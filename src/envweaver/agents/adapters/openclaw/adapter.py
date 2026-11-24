import json
from pathlib import Path

from ....errors import AgentError, ErrorType
from ...capture import json_lines
from .._cli import CliAgent
from .._provider_errors import context_limit_message
from .._provider_http import model_calls, provider_failure
from .trajectory import convert


def repeated_outage(calls):
    """Only attribute a deadline to the provider when every observed call confirms it."""
    return len(calls) >= 2 and all(
        (c.get("status") == 429 or 500 <= (c.get("status") or 0) < 600)
        and provider_failure(c) == ErrorType.PROVIDER_TRANSIENT
        for c in calls
    )


def failure(envelope, calls, terminal=None):
    detail = envelope.get("error")
    error = detail if isinstance(detail, dict) else {}
    message = (
        detail if isinstance(detail, str) else error.get("message")
    ) or "OpenClaw agent exec failed"
    if error.get("kind") == "context_overflow":
        return AgentError(ErrorType.CONTEXT_LIMIT, message)
    if error.get("type") == "cli_error":
        return AgentError(ErrorType.CONFIGURATION, message)
    if error.get("kind") == "aborted":
        return InterruptedError(message)
    kind = provider_failure(calls[-1]) if calls else None
    if kind is None and terminal and terminal.get("stopReason") == "error":
        if any(
            isinstance(d, dict) and d.get("type") == "provider_transport_failure"
            for d in terminal.get("diagnostics") or []
        ):
            kind = ErrorType.PROVIDER_TRANSIENT
    if kind is None:
        if context_limit_message(message):
            kind = ErrorType.CONTEXT_LIMIT
        elif envelope.get("status") == "timeout" or error.get("kind") == "timeout":
            kind = ErrorType.PROVIDER_TRANSIENT
        else:
            kind = ErrorType.UNKNOWN
    return AgentError(kind, message)


def read_envelope(text):
    # Native --json emits one (possibly pretty-printed) object. Ignore other stdout lines.
    decoder = json.JSONDecoder()
    last = {}
    offset = 0
    while (start := text.find("{", offset)) >= 0:
        try:
            value, offset = decoder.raw_decode(text, start)
        except ValueError:
            offset = start + 1
            continue
        if isinstance(value.get("ok"), bool):
            last = value
    return last


class OpenClawAgent(CliAgent):
    """
    Args:
        model: OpenClaw 模型名，格式为 provider/model；默认使用沙箱中的 OpenClaw 配置。
        version: openclaw npm 版本，须支持 agent exec。默认使用已安装版本，缺失时安装最新版。
        env: 传给沙箱内 CLI 的环境变量，如模型凭据。默认 None。
        extra_args: 原生 agent exec 参数列表，如 ["--config", "/etc/openclaw.json"]。
            路径位于做题沙箱中。默认 None。
        cache: 是否使用 EnvWeaver 的安装缓存，默认 True。
    """

    executable = "openclaw"
    package = "openclaw"
    dependency = "node"
    node_major = 24
    supports_images = False
    prompt_on_stdin = True
    managed_options = ("--cwd", "--state-dir", "--message-file", "--json", "--model", "--timeout")

    _outage = None

    def prepare(self, *, runtime, instance, output, cwd: str):
        super().prepare(runtime=runtime, instance=instance, output=output, cwd=cwd)
        for name in ("capture.cjs", "snapshot.cjs"):
            runtime.upload(
                instance=instance,
                source=Path(__file__).with_name(name),
                target=f"{self._remote}/{name}",
            )

    def _command(self, prompt, images):
        args = [
            "agent",
            "exec",
            "--message-file",
            "-",
            "--cwd",
            self._cwd,
            "--state-dir",
            self._remote,
            "--json",
            "--timeout",
            "0",
        ]
        if self.config["model"]:
            args.extend(["--model", self.config["model"]])
        # Scope capture to execution. Preserve NODE_OPTIONS from the sandbox or caller.
        return [
            "sh",
            "-c",
            'mkdir -p "$1/trace" || exit 1; export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--require=$1/capture.cjs"; '
            'export ENVWEAVER_OPENCLAW_ENTRY="$(command -v openclaw)"; '
            'shift; exec openclaw "$@"',
            "openclaw",
            self._remote,
            *args,
            *self._extra_args,
        ]

    def run(self, *, goal, runtime, instance, output, cwd: str):
        try:
            return super().run(
                goal=goal, runtime=runtime, instance=instance, output=output, cwd=cwd
            )
        except InterruptedError as exc:
            if self._outage is not None:
                raise self._outage from exc
            raise

    def _finalize_native(self):
        log = self._output.directory / "logs/stdout.log"
        envelope = read_envelope(log.read_text()) if log.exists() else {}
        if not envelope and not self._cancelled.is_set():
            self._output.update(
                metadata={"envelope_error": "No complete OpenClaw JSON result on stdout"}
            )
        if envelope:
            self._completed = envelope.get("ok") is True and envelope.get("status") == "ok"
            if not self._completed:
                self._run_error = failure(envelope, [])
            self._output.update(
                output=[{"type": "text", "text": envelope["final"]}]
                if envelope.get("final")
                else [],
                metadata={
                    k: envelope[k]
                    for k in (
                        "model",
                        "provider",
                        "sessionId",
                        "usage",
                        "costUsd",
                        "assistantTurns",
                        "toolSummary",
                    )
                    if envelope.get(k) is not None
                },
            )
        try:
            self._export(envelope)
        except Exception as exc:
            self._output.update(metadata={"trajectory_export_error": self._redact(str(exc))})

    def _export(self, envelope):
        code, _, stderr = self._runtime.exec(
            instance=self._instance,
            command=[
                "env",
                "PATH=" + self._dependency_env.get("PATH", self._sandbox_path),
                "OPENCLAW_STATE_DIR=" + self._remote,
                "sh",
                "-c",
                'exec node "$1/snapshot.cjs" "$(command -v openclaw)"',
                "openclaw",
                self._remote,
            ],
            timeout=30,
        )
        if code:
            self._output.update(metadata={"trajectory_export_error": self._redact(stderr.strip())})
        self._native.poll({"openclaw": self._remote + "/trace"})
        files = self._native.files
        calls = model_calls(json_lines(files.get("openclaw/http.jsonl", "")), self._output, source="openclaw.http")
        if self._cancelled.is_set() and repeated_outage(calls):
            self._outage = AgentError(
                ErrorType.PROVIDER_TRANSIENT,
                f"All {len(calls)} observed OpenClaw requests failed with HTTP {sorted({c['status'] for c in calls})}",
            )
        sessions = json.loads(files.get("openclaw/messages.json", "{}"))
        session = envelope.get("sessionId") or (
            next(iter(sessions)) if len(sessions) == 1 else None
        )
        messages = sessions.get(session, [])
        if not code and self._completed and not messages:
            self._output.update(
                metadata={
                    "trajectory_export_error": "No transcript for the completed OpenClaw session"
                }
            )

        terminal = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
        if envelope and not self._completed:
            self._run_error = failure(envelope, calls, terminal)
        if self._completed and terminal and terminal.get("stopReason") == "length":
            self._run_error = AgentError(
                ErrorType.TOKEN_LIMIT, "OpenClaw reached the output token limit"
            )
        doc = convert(
            session or Path(self._remote).name,
            messages,
            calls,
            envelope,
            self._output,
            multiple_sessions=len(sessions) > 1,
        )
        if doc:
            self._output.save_trajectory(doc)
