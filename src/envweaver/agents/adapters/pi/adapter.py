import base64
import json
import re
from pathlib import Path

from ....errors import AgentError, ErrorType
from .._cli import CliAgent
from .._provider_errors import context_limit_message, provider_error_type


def _error(message):
    """Decode Pi assistant errorMessage; stopReason is handled by _on_event."""
    if not isinstance(message, str):
        return AgentError(ErrorType.UNKNOWN, "Invalid Pi error message")
    # Pi's OpenAI-compatible provider emits '<status>: <JSON error object>'.
    status = re.match(r"^(\d{3})(?::|\s)", message)
    number = int(status[1]) if status else None
    try:
        body = json.loads(message[status.end():].strip() if status else message)
    except ValueError:
        body = None
    code = body.get("code") if isinstance(body, dict) else None
    if code == "context_length_exceeded" and number in (None, 400):
        return AgentError(ErrorType.CONTEXT_LIMIT, message)
    if (kind := provider_error_type(code)) is not None:
        return AgentError(kind, message)
    if number in (401, 403):
        return AgentError(ErrorType.AUTHENTICATION, message)
    if number in (408, 429, 500, 502, 503, 504):
        return AgentError(ErrorType.PROVIDER_TRANSIENT, message)
    # Other Pi providers expose text only. Keep their interpretation in this adapter.
    if message.lower().startswith((
        "rate limit", "too many requests", "throttling error", "service unavailable",
    )):
        return AgentError(ErrorType.PROVIDER_TRANSIENT, message)
    explanation = body.get("message") if isinstance(body, dict) else message
    if number in (None, 400) and context_limit_message(explanation):
        return AgentError(ErrorType.CONTEXT_LIMIT, message)
    if number in (400, 404, 422):
        return AgentError(ErrorType.CONFIGURATION, message)
    if message.startswith(("Connection error", "Request timed out", "fetch failed")):
        return AgentError(ErrorType.PROVIDER_TRANSIENT, message)
    return AgentError(ErrorType.UNKNOWN, message or "Pi execution failed")


class PiAgent(CliAgent):
    """
    Args:
        model: Pi 模型名称，可用 provider/model 格式；不填使用 Pi 的配置。
        version: 安装的 Pi 版本；不填则使用已安装版本，未安装时安装最新版。
        env: 环境变量字典，传入值覆盖沙箱同名变量；默认 None，不自动读取当前机器的变量。
        extra_args: 原生 pi 参数列表，如 ["--thinking", "high"]；默认 None。
        cache: 是否使用 EnvWeaver 的安装缓存，默认 True。
    """

    executable = "pi"
    dependency = "node"
    stdout_events = True
    package = "@earendil-works/pi-coding-agent"
    managed_options = ("--mode", "--session", "--session-dir", "--no-session")

    def prepare(self, *, runtime, instance, output, cwd: str):
        super().prepare(runtime=runtime, instance=instance, output=output, cwd=cwd)
        runtime.upload(instance=instance, source=Path(__file__).with_name("capture.ts"), target=self._remote + "/capture.ts")

    def _process_env(self):
        return {**super()._process_env(), "ENVWEAVER_PI_TRACE": self._remote + "/trace/events.jsonl"}

    def _poll_native(self):
        from ...capture import json_lines
        from .trajectory import convert

        if self._native.poll({"pi": self._remote + "/trace"}):
            trajectory = convert(json_lines(self._native.files.get("pi/events.jsonl", "")), self._output)
            if trajectory is not None:
                self._output.save_trajectory(trajectory)

    def _command(self, prompt, images):
        # A prompt file preserves literal @file text in user instructions.
        prompt_path = self._upload_text("prompt.txt", prompt)
        command = ["pi", "--mode", "json", "--print", "--session", self._remote + "/trace/session.jsonl"]
        if model := self._output.result["metadata"]["model"]:
            command += ["--model", model]
        return command + self._extra_args + ["-e", self._remote + "/capture.ts", "@" + prompt_path] + ["@" + path for path in images]

    def _on_event(self, event):
        self._archive_media(event)
        if event.get("type") in ("agent_start", "message_start"):
            self._completed = False
        if event.get("type") in ("agent_end", "agent_settled"):
            self._completed = True
        if event.get("type") == "message_end":
            message = event.get("message", {})
            if message.get("role") != "assistant":
                return
            contents = message.get("content", [])
            if isinstance(contents, str):
                contents = [{"type": "text", "text": contents}]
            output = [
                {"type": "text", "text": item["text"]}
                for item in contents
                if item.get("type") == "text" and isinstance(item.get("text"), str)
            ]
            for item in contents:
                if item.get("type") == "image":
                    path = self._image_file(item)
                    if path:
                        output.append({"type": "image", "path": path})
            metadata = {}
            if message.get("model"):
                metadata["model"] = message["model"]
            if isinstance(message.get("usage"), dict):
                metadata["last_message_usage"] = message["usage"]
            self._output.update(output=output, metadata=metadata)
            self._run_error = None
            if message.get("stopReason") == "length":
                self._run_error = AgentError(
                    ErrorType.TOKEN_LIMIT, "Pi reached the token limit"
                )
            elif message.get("stopReason") == "error":
                self._run_error = _error(self._redact(message.get("errorMessage", "")))
            elif message.get("stopReason") == "aborted":
                self._run_error = InterruptedError("Pi session was aborted")
        if event.get("type") == "agent_settled" and event.get("aborted"):
            self._run_error = InterruptedError("Pi session was aborted")

    def _archive_media(self, value):
        if isinstance(value, list):
            for item in value:
                self._archive_media(item)
        elif isinstance(value, dict):
            path = self._image_file(value)
            if path:
                self._output.record(
                    "artifact",
                    {
                        "path": path,
                        "media_type": "image",
                    },
                )
            for child in value.values():
                if isinstance(child, (dict, list)):
                    self._archive_media(child)

    def _image_file(self, value):
        if value.get("type") != "image" or not isinstance(value.get("data"), str):
            return None
        mime = value.get("mimeType")
        if not isinstance(mime, str) or not mime.startswith("image/"):
            raise ValueError("Pi image requires an image mimeType")
        return self._output.save_file(
            base64.b64decode(value["data"], validate=True), "." + mime.split("/")[-1]
        )
