import json
import posixpath
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from ....errors import AgentError, ErrorType
from ..._redaction import is_credential_name
from ...capture import json_lines, litellm
from .._cli import CliAgent
from .._provider_http import provider_failure
from ..sandbox import upload_model_capture


def _check_version(version):
    if not re.fullmatch(r"0\.2\.[0-9]+", version):
        raise AgentError(ErrorType.CONFIGURATION, f"Stirrup 0.2.x is required, got {version!r}")


def _check_client_options(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("client_kwargs must use string keys")
            if is_credential_name(key, request_options=True):
                raise ValueError("Pass model credentials through env")
            _check_client_options(item)
    elif isinstance(value, list):
        for item in value:
            _check_client_options(item)
    elif isinstance(value, str) and value.startswith(("http://", "https://")):
        url = urlsplit(value)
        if url.username or url.password:
            raise ValueError("Model service URLs must not contain credentials")
        _check_client_options(dict(parse_qsl(url.query, keep_blank_values=True)))


class StirrupAgent(CliAgent):
    """
    Args:
        model: 必填，带提供方前缀的模型名，例如 openai/Qwen3.5-9B。
        context_window_tokens: 必填，模型的上下文窗口大小，单位为 token。
        max_tokens: 单次回复的 token 上限，默认 64000，须小于或等于上下文窗口。
        max_turns: 最多执行的模型轮数，默认 30。
        version: 沙箱内的 Stirrup 0.2.x 版本。默认复用已安装版本，缺少时从 PyPI 安装。
        reasoning_effort: 模型的推理强度，默认 None，使用提供方默认值。
        client_kwargs: 传给 LiteLLM 的请求参数，如 api_base、temperature；默认 None。凭据通过 env 提供。
        system_prompt: 系统指令，默认 None，使用 Stirrup 自带提示。
        system_prompt_mode: append 将指令追加到原提示（默认），replace 使用完整的 system_prompt。
        view_image: 允许模型查看沙箱内的图片，默认 False。
        allow_abandon: 提供单独的放弃任务工具，默认 False。
        command_timeout: 每条 shell 命令的时限，单位秒，默认 300。
        submission_dir: 允许交付文件的沙箱绝对目录。填写时保存 finish 提交的原件；
            默认 None，仅在结果 metadata.submitted_paths 中记录提交路径。
        web: 启用 Stirrup 网页工具，默认 True。
        turns_remaining_warning_threshold: 剩余多少轮开始提醒，默认 20；0 关闭轮数提醒。
        context_summarization_cutoff: 触发上下文压缩的窗口占用比例，默认 0.7。
        env: 沙箱环境变量，例如 OPENAI_API_KEY、OPENAI_API_BASE，默认 None。
        cache: 是否使用 EnvWeaver 的安装缓存，默认 True。

    Raises:
        ValueError: 模型、执行限额、提示模式、交付目录或环境配置无效，或请求参数包含凭据或与客户端冲突。
        TypeError: 提示或推理强度不是字符串、开关不是布尔值，或请求参数不是 JSON 字典。
    """

    executable = "stirrup"
    dependency = "pip"
    prompt_on_stdin = True

    def __init__(
        self,
        *,
        model: str,
        context_window_tokens: int,
        max_tokens: int = 64000,
        max_turns: int = 30,
        reasoning_effort: str | None = None,
        client_kwargs: dict | None = None,
        system_prompt: str | None = None,
        web: bool = True,
        view_image: bool = False,
        allow_abandon: bool = False,
        command_timeout: int = 300,
        submission_dir: str | None = None,
        system_prompt_mode: str = "append",
        turns_remaining_warning_threshold: int = 20,
        context_summarization_cutoff: float = 0.7,
        version: str | None = None,
        env: dict[str, str] | None = None,
        cache: bool = True,
    ):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a nonempty string")
        if any(
            type(n) is not int or n < 1
            for n in (context_window_tokens, max_tokens, max_turns, command_timeout)
        ):
            raise ValueError(
                "context_window_tokens, max_tokens, max_turns and command_timeout must be positive integers"
            )
        if max_tokens > context_window_tokens:
            raise ValueError("max_tokens exceeds context_window_tokens")
        if any(v is not None and not isinstance(v, str) for v in (system_prompt, reasoning_effort)):
            raise TypeError("system_prompt and reasoning_effort must be strings or None")
        if not all(isinstance(flag, bool) for flag in (web, view_image, allow_abandon)):
            raise TypeError("web, view_image and allow_abandon must be bools")
        if system_prompt_mode not in ("append", "replace"):
            raise ValueError("system_prompt_mode must be append or replace")
        if system_prompt_mode == "replace" and system_prompt is None:
            raise ValueError("replace requires system_prompt")
        if submission_dir is not None and (
            not isinstance(submission_dir, str) or not posixpath.isabs(submission_dir)
        ):
            raise ValueError("submission_dir must be an absolute sandbox path")
        if (
            type(turns_remaining_warning_threshold) is not int
            or turns_remaining_warning_threshold < 0
        ):
            raise ValueError("turns_remaining_warning_threshold must be a nonnegative integer")
        if (
            type(context_summarization_cutoff) not in (float, int)
            or not 0 < context_summarization_cutoff <= 1
        ):
            raise ValueError("context_summarization_cutoff must be in (0, 1]")
        if client_kwargs is not None:
            if not isinstance(client_kwargs, dict):
                raise TypeError("client_kwargs must be a dict")
            _check_client_options(client_kwargs)
            if set(client_kwargs) & {
                "model",
                "messages",
                "tools",
                "tool_choice",
                "max_tokens",
                "reasoning_effort",
            } or client_kwargs.get("stream"):
                raise ValueError("client_kwargs conflict with Stirrup's non-streaming client")
        super().__init__(model=model, version=version, env=env, cache=cache)
        self.metadata["agent_version"] = None
        del self._config["extra_args"]
        self._config.update(
            context_window_tokens=context_window_tokens,
            max_tokens=max_tokens,
            max_turns=max_turns,
            reasoning_effort=reasoning_effort,
            client_kwargs=json.loads(json.dumps(client_kwargs, allow_nan=False)),
            system_prompt=system_prompt,
            web=web,
            view_image=view_image,
            allow_abandon=allow_abandon,
            command_timeout=command_timeout,
            submission_dir=submission_dir,
            system_prompt_mode=system_prompt_mode,
            turns_remaining_warning_threshold=turns_remaining_warning_threshold,
            context_summarization_cutoff=context_summarization_cutoff,
        )

    def _version_command(self):
        probe = (
            "from importlib.metadata import version; version('litellm'); print(version('stirrup'))"
        )
        return [self._python, "-c", probe]

    def _install_command(self, version):
        return self._pip_install_command(
            "stirrup[litellm]" + (f"=={version}" if version else ">=0.2,<0.3"), "litellm==1.111.0"
        )

    def prepare(self, *, runtime, instance, output, cwd: str):
        requested = self._config["version"]
        if requested is not None:
            _check_version(requested)
        code, python_version, stderr = runtime.exec(
            instance=instance,
            command=[
                instance.data.get("python", "python3"),
                "-c",
                "import platform; print(platform.python_version())",
            ],
            timeout=30,
        )
        if code or tuple(map(int, python_version.strip().split(".")[:2])) < (3, 12):
            raise AgentError(
                ErrorType.CONFIGURATION,
                f"Stirrup requires sandbox Python >=3.12; found {python_version.strip() or stderr.strip()}",
            )
        super().prepare(runtime=runtime, instance=instance, output=output, cwd=cwd)
        _check_version(output.result["metadata"]["agent_version"])
        upload_model_capture(runtime, instance, self._remote)
        directory = Path(__file__).parent
        for source, name in (
            (directory / "runner.py", "stirrup_runner.py"),
            (directory / "workspace.py", "workspace.py"),
            (directory.parents[2] / "runtime" / "_command.py", "task_command.py"),
        ):
            runtime.upload(instance=instance, source=source, target=f"{self._remote}/{name}")

    def _process_env(self):
        return {"LITELLM_LOCAL_MODEL_COST_MAP": "True", **super()._process_env()}

    def _command(self, prompt, images):
        cfg = self._config
        settings = {
            "client": {
                k: cfg[k]
                for k in ("model", "max_tokens", "context_window_tokens", "reasoning_effort")
            },
            "agent": {
                k: cfg[k]
                for k in (
                    "max_turns",
                    "system_prompt",
                    "turns_remaining_warning_threshold",
                    "context_summarization_cutoff",
                )
            },
            "web": cfg["web"],
            "view_image": cfg["view_image"],
            "allow_abandon": cfg["allow_abandon"],
            "command_timeout": cfg["command_timeout"],
            "submission_dir": cfg["submission_dir"],
        }
        if cfg["system_prompt_mode"] == "replace":
            settings["full_system_prompt"] = settings["agent"].pop("system_prompt")
        settings["client"]["kwargs"] = cfg["client_kwargs"]
        path = self._upload_text("settings.json", json.dumps(settings))
        return [self._python, self._remote + "/stirrup_runner.py", path, *images]

    def _poll_native(self):
        if self._native.poll({"stirrup": self._remote + "/model-calls.jsonl"}):
            events = json_lines(self._native.files["stirrup/model-calls.jsonl"])
            doc = litellm.convert(events, self._output, session_id=Path(self._remote).name)
            if doc is not None:
                self._output.save_trajectory(doc)

    def _finish(self):
        raw = self._read_remote(self._remote + "/outcome.json")
        if raw is None:
            return
        outcome = json.loads(raw)
        if "error" in outcome:
            error = outcome["error"]
            category = ErrorType(error["type"])
            if category == ErrorType.UNKNOWN:
                category = (
                    provider_failure({"status": error["status"], "body": error["message"]})
                    or category
                )
            self._run_error = AgentError(category, self._redact(error["message"]))
        else:
            paths = outcome["paths"]
            metadata = {"abandoned": outcome["abandoned"], "submitted_paths": paths}
            if self._config["submission_dir"] is not None:
                files = dict.fromkeys(paths)
                for i, path in enumerate(files):
                    data = self._read_remote(f"{self._remote}/submitted/{i}")
                    if data is None:
                        raise FileNotFoundError(f"Missing submitted file: {path}")
                    suffix = Path(path).suffix
                    files[path] = self._output.save_file(
                        data, suffix if suffix[1:].isalnum() else ""
                    )
                metadata["submitted_files"] = files
            self._output.update(
                output=[{"type": "text", "text": self._redact(outcome["answer"])}],
                metadata=metadata,
            )
            self._completed = True
