import json
import re

from ....errors import AgentError, ErrorType
from .._cli import CliAgent
from .._provider_errors import context_limit_message, provider_error_type


def _error(message):
    """Decode Codex exec's error/turn.failed message, including provider JSON."""
    if not isinstance(message, str):
        return AgentError(ErrorType.UNKNOWN, "Invalid Codex error message")
    status = None if message.startswith("{") else re.search(
        r"(?:unexpected status |last status: |^)(\d{3})\b", message
    )
    number = int(status[1]) if status else None
    detail = message[status.end():].strip() if status else message
    # Codex may prefix a provider JSON body with the HTTP status and append a URL.
    try:
        body, _ = json.JSONDecoder().raw_decode(detail[detail.index("{"):])
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    code = error.get("code")
    if number is None and isinstance(code, int):
        number = code
    if number in (401, 403):
        return AgentError(ErrorType.AUTHENTICATION, message)
    if number in (408, 429, 500, 502, 503, 504):
        return AgentError(ErrorType.PROVIDER_TRANSIENT, message)
    if (kind := provider_error_type(code)) is not None:
        return AgentError(kind, message)
    explanation = error.get("message") if body is not None else detail
    if number in (None, 400) and (
        code == "context_length_exceeded" or context_limit_message(explanation)
    ):
        return AgentError(ErrorType.CONTEXT_LIMIT, message)
    if number in (400, 404, 422) or error.get("type") == "BadRequestError":
        return AgentError(ErrorType.CONFIGURATION, message)
    if message.startswith("Codex ran out of room in the model's context window"):
        return AgentError(ErrorType.CONTEXT_LIMIT, message)
    if message.startswith("You've hit your usage limit"):
        return AgentError(ErrorType.CONFIGURATION, message)
    if message.startswith(("stream disconnected before completion", "error sending request for url")):
        return AgentError(ErrorType.PROVIDER_TRANSIENT, message)
    return AgentError(ErrorType.UNKNOWN, message or "Codex execution failed")


class CodexAgent(CliAgent):
    """
    Args:
        model: Codex 模型名称，不填使用 Codex 默认配置。
        version: Codex CLI 版本；不填时使用已安装版本，未安装则安装默认版本。
        env: 环境变量字典，传入值覆盖沙箱同名变量；默认 None，不自动读取当前机器的变量。
        extra_args: 原生 codex exec 参数列表，如 ["-c", 'model_reasoning_effort="high"']；默认 None。
        cache: 是否使用 EnvWeaver 的安装缓存，默认 True。
    """

    executable = "codex"
    dependency = "node"
    stdout_events = True
    package = "@openai/codex"
    managed_options = ("--output-last-message", "-o")

    def _process_env(self):
        return {**super()._process_env(), "CODEX_ROLLOUT_TRACE_ROOT": self._remote + "/rollout"}

    def _poll_native(self):
        from .payloads import unpack_shard
        from .trajectory import convert

        try:
            if not self._native.poll({"codex": self._remote + "/rollout"}, archives={".zst": unpack_shard}):
                return
            trajectory = convert(self._native.files, self._output, self._read_remote)
            if trajectory is None:
                return
            self._output.save_trajectory(trajectory)
            warning = None
        except Exception as exc:
            warning = self._redact(f"{type(exc).__name__}: {exc}")
        if self._output.result["metadata"].get("trajectory_export_error") != warning:
            self._output.update(metadata={"trajectory_export_error": warning}, sync=False)

    prompt_on_stdin = True

    def _command(self, prompt, images):
        command = [
            "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "danger-full-access",
            "-c",
            'approval_policy="never"',
            "--output-last-message",
            self._remote + "/final.txt",
        ]
        if model := self._output.result["metadata"]["model"]:
            command += ["--model", model]
        for path in images:
            command += ["--image", path]
        return command + self._extra_args + ["--", "-"]

    def _on_event(self, event):
        kind = event.get("type")
        if kind == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                self._output.update(output=[{"type": "text", "text": item["text"]}])
        if kind == "turn.started":
            self._completed = False
        if kind == "turn.completed":
            self._completed = True
            self._run_error = None
        if kind == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = dict(self._output.result["metadata"].get("usage", {}))
            for key, value in event["usage"].items():
                if isinstance(value, (int, float)):
                    usage[key] = usage.get(key, 0) + value
            self._output.update(metadata={"usage": usage})
        if kind in ("turn.failed", "error"):
            self._completed = False
            message = self._redact(
                event.get("error", {}).get("message", "")
                if kind == "turn.failed" else event.get("message", "")
            )
            self._run_error = _error(message)

    def _finish(self):
        data = self._read_remote(self._remote + "/final.txt")
        if data is not None:
            self._output.update(
                output=[{"type": "text", "text": data.decode("utf-8", errors="replace")}]
                if data else []
            )
