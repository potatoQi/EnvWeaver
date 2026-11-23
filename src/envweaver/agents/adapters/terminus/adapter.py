"""Terminus 2 adapter; Harbor and its model client run inside the task sandbox."""

import json
from pathlib import Path

from ...._util import write_json
from ....errors import AgentError, ErrorType
from ..._redaction import redact_data
from .._cli import CliAgent
from ..sandbox import SANDBOX_SCRIPTS

_ERROR_TYPES = {
    "StepLimit": ErrorType.STEP_LIMIT,
    "ContextLengthExceededError": ErrorType.CONTEXT_LIMIT,
    "ContextWindowExceededError": ErrorType.CONTEXT_LIMIT,
    "OutputLengthExceededError": ErrorType.TOKEN_LIMIT,
    "AuthenticationError": ErrorType.AUTHENTICATION,
    "PermissionDeniedError": ErrorType.AUTHENTICATION,
    "RateLimitError": ErrorType.PROVIDER_TRANSIENT,
    "APIConnectionError": ErrorType.PROVIDER_TRANSIENT,
    "Timeout": ErrorType.PROVIDER_TRANSIENT,
    "InternalServerError": ErrorType.PROVIDER_TRANSIENT,
    "ServiceUnavailableError": ErrorType.PROVIDER_TRANSIENT,
    "BadRequestError": ErrorType.CONFIGURATION,
    "NotFoundError": ErrorType.CONFIGURATION,
    "ValueError": ErrorType.CONFIGURATION,
    "ValidationError": ErrorType.CONFIGURATION,
    "TerminalError": ErrorType.SANDBOX,
    "OSError": ErrorType.SANDBOX,
}


class Terminus2Agent(CliAgent):
    """
    Args:
        model: 模型名称，必填，例如 openai/Qwen3.8-27B。
        env: 传给沙箱内 Agent 的环境变量，默认 None；不自动读取当前机器的变量。
            TMUX_TMPDIR 由适配器设置为本次独立终端目录。
        options: Harbor 0.25.0 的 Terminus 2 参数字典，默认 None；例如 api_base、
            temperature、max_turns、model_info、llm_call_kwargs。
        cache: 是否使用 EnvWeaver 的安装缓存，默认 True。

    Raises:
        ValueError: model、env 或 options 无效，或覆盖了适配器管理的参数。
        TypeError: cache 不是布尔值，或配置不能保存为 JSON。
    """

    executable = "terminus-2"
    dependency = "pip"
    supports_images = False

    def __init__(
        self,
        *,
        model: str,
        env: dict[str, str] | None = None,
        options: dict | None = None,
        cache: bool = True,
    ):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a nonempty string")
        if options is not None and not isinstance(options, dict):
            raise ValueError("options must be a dictionary")
        reserved = {
            "model_name",
            "logs_dir",
            "logger",
            "extra_env",
            "session_id",
            "record_terminal_session",
            "load_trajectory",
            "environment_logs_dir",
            "episodes",
            "max_episodes",
        }
        if reserved.intersection(options or {}):
            raise ValueError("options cannot override adapter lifecycle or legacy parameters")
        super().__init__(model=model, env=env, cache=cache)
        self._config = json.loads(
            json.dumps(
                {"model": model, "env": self._env, "options": options or {}, "cache": cache},
                allow_nan=False,
            )
        )
        self._agent_python = None
        self._counts = {}
        self._snapshot = None

    def _redact(self, text):
        def secrets(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                        if isinstance(item, str) and item:
                            yield item
                    yield from secrets(item)
            elif isinstance(value, list):
                for item in value:
                    yield from secrets(item)

        for value in sorted(set(secrets(self._config)), key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
        return text

    def _version_command(self):
        return [
            self._agent_python or self._python,
            "-c",
            "import sys; from importlib.metadata import version; "
            "assert sys.version_info >= (3,12); "
            "assert version('harbor') == '0.25.0'; print('2.0.0')",
        ]

    def _install_command(self, version):
        self._agent_python = self._remote + "/venv/bin/python"
        # uv supplies an isolated Python if the task's Python is too old for Harbor.
        self._dependency_env.update(
            {
                "UV_CACHE_DIR": self._remote + "/uv-cache",
                "UV_PYTHON_INSTALL_DIR": self._remote + "/python",
            }
        )
        self._runtime.upload(
            instance=self._instance,
            source=Path(__file__).with_name("install.py"),
            target=self._remote + "/terminus-install.py",
        )
        return [self._python, self._remote + "/terminus-install.py", self._remote]

    def _download_helpers(self):
        return (*super()._download_helpers(), Path(__file__).with_name("_uv_install.py"))

    def prepare(self, *, runtime, instance, output, cwd: str):
        super().prepare(runtime=runtime, instance=instance, output=output, cwd=cwd)
        self._agent_python = self._agent_python or self._python
        runtime.upload(
            instance=instance,
            source=Path(__file__).with_name("runner.py"),
            target=self._remote + "/terminus.py",
        )
        runtime.upload(
            instance=instance,
            source=Path(__file__).with_name("monkey_patches"),
            target=self._remote + "/monkey_patches",
        )
        runtime.upload(instance=instance, source=SANDBOX_SCRIPTS / "_model_capture.py", target=self._remote + "/model_capture.py")
        runtime.upload(instance=instance, source=Path(__file__).parents[2] / "_redaction.py", target=self._remote + "/_redaction.py")
        settings = {key: self._config[key] for key in ("model", "env", "options")}
        self._upload_text("config.json", json.dumps(settings))
        code, _ = self._execute(self._runner("prepare"), preparing=True)
        self._poll_native()
        if self._run_error:
            raise self._run_error
        if code:
            raise AgentError(
                ErrorType.SANDBOX, "Terminus 2 preparation failed; see logs/prepare.log"
            )
        output.update(metadata={"agent_version": "2.0.0", "harbor_version": "0.25.0"})

    def _runner(self, action):
        return [self._agent_python, self._remote + "/terminus.py", action, self._remote]

    def _command(self, prompt, images):
        self._upload_text("goal.txt", prompt)
        return self._runner("run")

    def _poll_native(self):
        from ...capture import json_lines
        from .trajectory import convert

        changed = self._native.poll({"terminus": self._remote + "/model-calls.jsonl"})
        body = self._read_remote(self._remote + "/state.json")
        if body is None or (body == self._snapshot and not changed):
            return
        native = redact_data(json.loads(body), self._redact)
        paths = []
        for name, trajectory in native["trajectories"].items():
            if Path(name).name != name:
                raise ValueError("Invalid Terminus trajectory filename")
            steps = trajectory.get("steps", [])
            count = self._counts.get(name, 0)
            if len(steps) < count:
                raise RuntimeError("Terminus trajectory was reset during a run")
            for step in steps[count:]:
                self._output.record("terminus.step", step)
            self._counts[name] = len(steps)
            relative = "logs/terminus/" + name
            write_json(self._output.directory / relative, trajectory, private=True)
            paths.append(relative)
        trajectories = convert(native["trajectories"], json_lines(self._native.files.get("terminus/model-calls.jsonl", "")), self._output)
        if trajectories is not None:
            self._output.save_trajectory(
                trajectories.pop("trajectory.json"), related=trajectories,
            )
        self._completed = native["completed"]
        if error := native["error"]:
            kind = next(
                (_ERROR_TYPES[name] for name in error["classes"] if name in _ERROR_TYPES),
                ErrorType.UNKNOWN,
            )
            self._run_error = AgentError(kind, self._redact(error["message"]))
        self._output.update(metadata={"usage": native["usage"]}, artifacts={"logs": list(dict.fromkeys([*self._output.result["artifacts"]["logs"], *paths]))})
        self._snapshot = body
        if native.get("stop_failed"):
            raise AgentError(ErrorType.SANDBOX, native["stop_failed"])
