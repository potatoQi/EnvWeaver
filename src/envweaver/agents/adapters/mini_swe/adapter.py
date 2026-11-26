import json
from pathlib import Path

from ...._util import write_json
from ....errors import AgentError, ErrorType
from ..._redaction import redact_data
from .._cli import CliAgent
from ..sandbox import upload_model_capture

_ERROR_TYPES = {
    "LimitsExceeded": ErrorType.OTHER_LIMIT,
    "TimeExceeded": ErrorType.TIMEOUT,
    "ContextWindowExceededError": ErrorType.CONTEXT_LIMIT,

    "AuthenticationError": ErrorType.AUTHENTICATION,
    "PermissionDeniedError": ErrorType.AUTHENTICATION,
    "BadRequestError": ErrorType.CONFIGURATION,
    "NotFoundError": ErrorType.CONFIGURATION,
    "UnsupportedParamsError": ErrorType.CONFIGURATION,
    "RateLimitError": ErrorType.PROVIDER_TRANSIENT,
    "APIConnectionError": ErrorType.PROVIDER_TRANSIENT,
    "APITimeoutError": ErrorType.PROVIDER_TRANSIENT,
    "Timeout": ErrorType.PROVIDER_TRANSIENT,
    "ServiceUnavailableError": ErrorType.PROVIDER_TRANSIENT,
    "InternalServerError": ErrorType.PROVIDER_TRANSIENT,
}


def _error(status, detail):
    """Interpret mini's exception class name, not arbitrary trajectory text."""
    message = f"mini-swe-agent ended with {status}: {detail}"
    if status == "Submitted":
        return None
    # LiteLLM can flatten provider invalid_request_error into BadRequestError.
    # Only refine that ambiguous class; a typed error always keeps its meaning.
    if status == "BadRequestError" and isinstance(detail, str):
        prefix = "litellm.BadRequestError: OpenAIException - "
        if detail.startswith(prefix):
            explanation = detail.removeprefix(prefix).lower()
            if explanation.startswith((
                "context length exceeded", "context_length_exceeded", "maximum context length",
            )):
                return AgentError(ErrorType.CONTEXT_LIMIT, message)
            if explanation.startswith("invalid api key"):
                return AgentError(ErrorType.AUTHENTICATION, message)
    return AgentError(_ERROR_TYPES.get(status, ErrorType.UNKNOWN), message)


class MiniSweAgent(CliAgent):
    """
    Args:
        model: mini-swe-agent 模型名称，例如 provider/model；不填使用其配置。
        version: 安装的 mini-swe-agent 版本，仅支持 2.x；不填则使用已安装版本，未安装时安装最新的 2.x 版本。
        env: 环境变量字典，传入值覆盖沙箱同名变量；默认 None，不自动读取当前机器的变量。
        extra_args: 原生 mini 参数列表，如 ["-c", "/workspace/agent.yaml"]；配置文件须已在沙箱内。默认 None。
        cache: 是否使用 EnvWeaver 的安装缓存，默认 True。
    """

    executable = "mini"
    dependency = "pip"
    supports_images = False
    managed_options = ("--output", "-o", "--task", "-t", "--environment-class")

    def preflight(self, goal):
        super().preflight(goal=goal)
        version = self.config.get("version")
        if version is not None and not version.startswith("2."):
            raise ValueError("MiniSweAgent requires mini-swe-agent 2.x")

    def _version_command(self):
        return [
            self._python,
            "-c",
            "from mcp.types import CacheableResult; "
            "from importlib.metadata import version; print(version('mini-swe-agent'))",
        ]

    def prepare(self, *, runtime, instance, output, cwd: str):
        self._native_count = 0
        self._native_snapshot = None
        self._snapshot = None
        super().prepare(runtime=runtime, instance=instance, output=output, cwd=cwd)
        upload_model_capture(runtime, instance, self._remote)
        runtime.upload(
            instance=instance, source=Path(__file__).with_name("runner.py"),
            target=self._remote + "/mini-runner.py",
        )
        if not self._output.result["metadata"]["agent_version"].startswith("2."):
            raise RuntimeError("MiniSweAgent requires mini-swe-agent 2.x")

    def _install_command(self, version):
        package = "mini-swe-agent" + (f"=={version}" if version else ">=2,<3")
        return self._pip_install_command("--no-warn-script-location", package, "mcp>=2.3,<3")

    def _process_env(self):
        return {"MSWEA_CONFIGURED": "true", **super()._process_env()}

    def _command(self, prompt, images):
        command = [
            self._python,
            self._remote + "/mini-runner.py",
            "--yolo",
            "--exit-immediately",
            "--environment-class",
            "local",
            "--cost-limit",
            "0",
            "--output",
            self._remote + "/mini.traj.json",
        ]
        if model := self._output.result["metadata"]["model"]:
            command += ["--model", model]
        return command + self._extra_args + ["--task", prompt]

    def _poll_native(self):
        from ...capture import json_lines
        from .trajectory import convert

        self._native.poll({"mini": self._remote + "/model-calls.jsonl"})
        calls = self._native.files.get("mini/model-calls.jsonl", "")

        def save_trajectory():
            trajectory = convert(
                json_lines(calls), self._output, self._read_remote, self._native_snapshot,
                session_id=Path(self._remote).name,
            )
            if trajectory is not None:
                self._output.save_trajectory(trajectory)
            return trajectory is None or not trajectory["extra"]["media_missing"]

        body = self._read_remote(self._remote + "/mini.traj.json")
        snapshot = (body, calls)
        if snapshot == self._snapshot:
            return
        if body is not None:
            try:
                native = redact_data(json.loads(body), self._redact)
            except ValueError:
                body = None  # A poll can catch a snapshot while mini is writing it.
        if body is None:
            self._snapshot = snapshot if save_trajectory() else None
            return
        messages = native.get("messages", [])
        if len(messages) < self._native_count:
            raise RuntimeError("mini-swe-agent trajectory was reset during a run")
        for message in messages[self._native_count :]:
            self._output.record("mini.message", message)
            self._native_count += 1
        info = native.get("info", {})
        # Do not archive native config: it may contain API keys.
        filtered_info = {key: value for key, value in info.items() if key != "config"}
        metadata = {"native_info": filtered_info}
        submission = info.get("submission")
        output = None
        if isinstance(submission, str):
            output = [{"type": "text", "text": submission}] if submission else []
        if info.get("exit_status"):
            status = info["exit_status"]
            self._completed = status == "Submitted"
            exit_message = next((item for item in reversed(messages) if item.get("role") == "exit"), {})
            detail = self._redact(exit_message.get("extra", {}).get("exception_str", ""))
            self._run_error = _error(status, detail)
        filtered = {**native, "info": filtered_info}
        self._native_snapshot = filtered
        media_complete = save_trajectory()
        write_json(self._output.directory / "logs" / "mini.traj.json", filtered, private=True)
        paths = list(self._output.result["artifacts"]["logs"])
        if "logs/mini.traj.json" not in paths:
            paths.append("logs/mini.traj.json")
        self._output.update(output=output, metadata=metadata, artifacts={"logs": paths})
        self._snapshot = snapshot if media_complete else None
