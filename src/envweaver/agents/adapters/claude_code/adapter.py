import json
import re
from pathlib import Path

from ....errors import AgentError, ErrorType
from ...capture import json_lines
from .._cli import CliAgent
from .._provider_errors import context_limit_message
from .trajectory import convert

_STOP_TYPES = {
    "prompt_too_long": ErrorType.CONTEXT_LIMIT,
    "max_turns": ErrorType.STEP_LIMIT,
    "error_max_turns": ErrorType.STEP_LIMIT,
    "budget_exhausted": ErrorType.COST_LIMIT,
    "error_max_budget_usd": ErrorType.COST_LIMIT,
    "error_max_structured_output_retries": ErrorType.OTHER_LIMIT,
    "structured_output_retry_exhausted": ErrorType.OTHER_LIMIT,
}
_API_TYPES = {
    "authentication_failed": ErrorType.AUTHENTICATION,
    "oauth_org_not_allowed": ErrorType.AUTHENTICATION,
    "cloud_credential_error": ErrorType.AUTHENTICATION,
    "account_on_hold": ErrorType.CONFIGURATION,
    "billing_error": ErrorType.CONFIGURATION,
    "model_not_found": ErrorType.CONFIGURATION,
    "rate_limit": ErrorType.PROVIDER_TRANSIENT,
    "overloaded": ErrorType.PROVIDER_TRANSIENT,
    "overloaded_error": ErrorType.PROVIDER_TRANSIENT,
    "rate_limit_error": ErrorType.PROVIDER_TRANSIENT,
    "api_error": ErrorType.PROVIDER_TRANSIENT,
    "server_error": ErrorType.PROVIDER_TRANSIENT,
    "max_output_tokens": ErrorType.TOKEN_LIMIT,
}


def _failure(event, message):
    """Prefer CLI stop codes and HTTP metadata; older CLIs omit the HTTP status."""
    kind = event.get("error")
    reason = _STOP_TYPES.get(event.get("terminal_reason")) or _STOP_TYPES.get(event.get("subtype"))
    if reason:
        return AgentError(reason, message)
    code = event.get("api_error_status")
    if (
        code is None
        and kind not in _API_TYPES
        and (match := re.match(r"API Error: (\d{3})\b", message))
    ):
        code = int(match[1])
    if code in (401, 403):
        reason = ErrorType.AUTHENTICATION
    elif code in (408, 429) or isinstance(code, int) and 500 <= code < 600:
        reason = ErrorType.PROVIDER_TRANSIENT
    elif kind in _API_TYPES:
        reason = _API_TYPES[kind]
    elif code in (None, 400) and context_limit_message(message):
        reason = ErrorType.CONTEXT_LIMIT
    elif kind == "invalid_request" or code in (400, 404, 413, 422):
        reason = ErrorType.CONFIGURATION
    elif code is None and message.startswith("Invalid API key"):
        reason = ErrorType.AUTHENTICATION
    elif code is None and message.startswith(
        ("API Error: Connection refused", "API Error: Connection error")
    ):
        reason = ErrorType.PROVIDER_TRANSIENT
    return AgentError(reason or ErrorType.UNKNOWN, message or event.get("subtype") or kind)


class ClaudeCodeAgent(CliAgent):
    """
    Args:
        model: Claude Code 的模型名或别名，默认使用 CLI 配置。
        version: npm 包版本。默认使用沙箱中的 claude，缺失时安装 npm 默认版本。
        env: 传给沙箱内 Claude Code 的环境变量字典，默认 None。
        extra_args: 传给 Claude Code 的额外非交互式命令行参数列表，默认 None。
        cache: 是否使用 EnvWeaver 的安装缓存，默认 True。
    """

    executable = "claude"
    dependency = "node"
    package = "@anthropic-ai/claude-code"
    prompt_on_stdin = True
    stdout_events = True
    supports_images = False
    managed_options = (
        "-p",
        "--print",
        "--input-format",
        "--output-format",
        "--verbose",
        "--include-partial-messages",
        "--replay-user-messages",
    )

    def prepare(self, *, runtime, instance, output, cwd: str):
        super().prepare(runtime=runtime, instance=instance, output=output, cwd=cwd)
        runtime.upload(
            instance=instance,
            source=Path(__file__).with_name("capture.py"),
            target=self._remote + "/capture.py",
        )

    def _command(self, prompt, images):
        self._prompt = prompt
        self._events = []
        self._saved_events = 0
        self._last_api_error = {}
        self._terminal_reason = None
        args = ["claude", "-p", "--verbose", "--output-format", "stream-json"]
        permission_options = {
            "--permission-mode",
            "--dangerously-skip-permissions",
            "--allow-dangerously-skip-permissions",
            "--permission-prompt-tool",
            "--allowedTools",
            "--allowed-tools",
            "--disallowedTools",
            "--disallowed-tools",
            "--settings",
        }
        if not any(arg.partition("=")[0] in permission_options for arg in self._extra_args):
            args.append("--permission-mode=bypassPermissions")
        if model := self.config["model"]:
            args += ["--model", model]
        return [self._python, self._remote + "/capture.py", *args, *self._extra_args]

    def _consume(self, name, text, preparing):
        super()._consume(name, text, preparing)
        if (
            name == "stderr"
            and not preparing
            and "--dangerously-skip-permissions cannot be used with root/sudo" in text
        ):
            self._run_error = AgentError(
                ErrorType.CONFIGURATION,
                "Claude Code rejected permission bypass while running as root",
            )

    def _on_event(self, event):
        kind = event.get("type")
        if kind in ("assistant", "user") or (
            kind == "system" and event.get("subtype") in ("init", "compact_boundary")
        ):
            self._events.append(event)
        if event.get("parent_tool_use_id"):
            return
        if kind == "assistant":
            self._last_api_error = {}
            self._run_error = None
            if event.get("error") or event.get("is_api_error_message"):
                self._last_api_error = {
                    k: event[k] for k in ("error", "api_error_status") if k in event
                }
                blocks = event["message"]["content"]
                text = (
                    blocks
                    if isinstance(blocks, str)
                    else " ".join(b["text"] for b in blocks if b["type"] == "text")
                )
                self._run_error = _failure(event, text)
        if kind == "result":
            subtype = event.get("subtype")
            self._terminal_reason = event.get("terminal_reason") or subtype
            self._completed = subtype == "success" and not event.get("is_error")
            text = event.get("result") or ""
            if self._completed:
                self._run_error = None
                if event.get("structured_output") is not None:
                    text = json.dumps(event["structured_output"], ensure_ascii=False)
                self._output.update(output=[{"type": "text", "text": text}] if text else [])
            else:
                message = text or "; ".join(event.get("errors", [])) or str(self._run_error or "")
                evidence = {
                    **self._last_api_error,
                    **{k: v for k, v in event.items() if v is not None},
                }
                self._run_error = _failure(evidence, message)
            metadata = {
                k: event[k]
                for k in ("usage", "total_cost_usd", "num_turns", "permission_denials")
                if k in event
            }
            if metadata:
                self._output.update(metadata=metadata)

    def _classify_provider_error(self, records, calls):
        if (
            self._terminal_reason in _STOP_TYPES
            or self._run_error is None
            or self._run_error.type
            not in (
                ErrorType.UNKNOWN,
                ErrorType.CONFIGURATION,
                ErrorType.PROVIDER_TRANSIENT,
                ErrorType.AUTHENTICATION,
                ErrorType.CONTEXT_LIMIT,
            )
        ):
            return
        primary_ids = {
            record["id"]
            for record in records
            if "request" in record and _has_user_prompt(record["request"], self._prompt)
        }
        primary = [c for c in calls if c["call_id"] in primary_ids]
        relevant = primary[-1:] or calls
        reasons = set()
        for call in relevant:
            if call["response_status"] != "failed":
                return
            detail = call.get("error") or {}
            kind = detail.get("type")
            if kind == "connection_error":
                reason = ErrorType.PROVIDER_TRANSIENT
            else:
                reason = _failure(
                    {"api_error_status": call.get("http_status"), "error": kind},
                    detail.get("message", ""),
                ).type
            reasons.add(reason)
        if len(reasons) == 1 and (reason := reasons.pop()) != ErrorType.UNKNOWN:
            self._run_error = AgentError(reason, str(self._run_error))

    def _finalize_native(self):
        self._poll_native(final=True)

    def _poll_native(self, *, final=False):
        warning = None
        try:
            changed = self._native.poll({"claude": self._remote + "/calls.jsonl"})
        except Exception as exc:
            warning = self._redact(str(exc))
            changed = False
        if not final and not changed and warning is None and len(self._events) == self._saved_events:
            return
        try:
            records = json_lines(self._native.files.get("claude/calls.jsonl", ""))
            trace = convert(
                self._events, records, self._output.result["metadata"], self._prompt, self._output
            )
            if final:
                self._classify_provider_error(records, trace["extra"]["model_calls"])
            self._output.save_trajectory(trace)
        except Exception as exc:
            warning = self._redact(str(exc))
        self._saved_events = len(self._events) if warning is None else None
        self._set_trajectory_error(warning)


def _has_user_prompt(request, prompt):
    for msg in request.get("messages", []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", [])
        if isinstance(content, str):
            if prompt in content:
                return True
        elif any(prompt in block["text"] for block in content if block.get("type") == "text"):
            return True
    return False
