import json
from pathlib import Path

from ....errors import AgentError, ErrorType
from ....runtime._attempt import _AttemptRuntime
from ...capture import json_lines
from .._cli import CliAgent
from .sandbox import REVISION, start_tools
from .trajectory import convert

_SOURCE_HASH = "7f97ed412e556267b27868cba3bf38d33c5fcbbe6509e6ceb9455018e68d57f7"


class ClawEvalAgent(CliAgent):
    """
    Args:
        model: 必填，做题模型配置。填写模型名 `model` 和沙箱可访问的服务地址 `base_url`，需要鉴权时填写 `api_key`。
        cache: 是否使用 EnvWeaver 的安装缓存，默认 True。

    Raises:
        ValueError: 模型名类型无效，或配置包含非有限数字或循环引用。
        TypeError: 模型配置不是字典、配置值无法保存为 JSON，或 cache 不是布尔值。
    """

    executable = "claw-eval"
    dependency = "pip"

    def __init__(self, *, model: dict, cache: bool = True):
        if not isinstance(model, dict):
            raise TypeError("model must be a dictionary")
        super().__init__(model=model.get("model", model.get("model_id")), cache=cache)
        self._config = {"model": json.loads(json.dumps(model, allow_nan=False)), "cache": cache}

    def _redact(self, text):
        text = super()._redact(text)
        key = self._config["model"].get("api_key")
        if isinstance(key, str) and key:
            text = text.replace(key, "[REDACTED]")
            text = text.replace(json.dumps(key, ensure_ascii=False)[1:-1], "[REDACTED]")
        return text

    def _version_command(self):
        # The unrelated distribution on PyPI uses the same name. Verify the actual source.
        probe = (
            "import hashlib, sys; from pathlib import Path; from importlib.util import find_spec; "
            "spec=find_spec('claw_eval'); "
            "root=Path(spec.origin).parent if spec and spec.origin else sys.exit(1); "
            "digest=hashlib.sha256(b''.join(p.relative_to(root).as_posix().encode()+b'\\0'+"
            "p.read_bytes()+b'\\0' for p in sorted(root.rglob('*.py')))).hexdigest(); "
            f"digest == '{_SOURCE_HASH}' or sys.exit('unexpected claw-eval source'); "
            "from claw_eval.runner.loop import run_task; "
            f"print('1.0.0+git.{REVISION[:7]}')"
        )
        return [self._python, "-c", probe]

    def _install_command(self, version):
        url = (
            f"https://github.com/claw-eval/claw-eval/archive/{REVISION}.tar.gz"
            "#sha256=dcd009657da238a0b2b20b90606a61f2d1197a2810e190c9c7431f66c8caab97"
        )
        return self._pip_install_command(f"claw-eval @ {url}")

    def prepare(self, *, runtime, instance, output, cwd: str):
        if not isinstance(runtime, _AttemptRuntime):
            raise ValueError("ClawEvalAgent requires an Eval-managed tool container")
        tools_url = start_tools(runtime, instance)
        super().prepare(runtime=runtime, instance=instance, output=output, cwd=cwd)
        runtime.upload(
            instance=instance,
            source=Path(__file__).with_name("runner.py"),
            target=self._remote + "/runner.py",
        )

        code, _ = self._execute(
            [self._python, self._remote + "/runner.py", "--prepare", tools_url],
            preparing=True,
        )
        if code:
            raise AgentError(
                ErrorType.CONFIGURATION, "Cannot prepare Claw task files; see logs/prepare.log"
            )

    def _command(self, prompt, images):
        model = self._config["model"].copy()
        if "model" in model:
            model["model_id"] = model.pop("model")
        if "extra_body" not in model and "reasoning_effort" not in model:
            model["extra_body"] = {"reasoning": {"effort": "high"}}
        sampling = {key: model.pop(key) for key in ("top_p", "top_k") if key in model}
        if sampling:
            model["extra_body"] = {**(model.get("extra_body") or {}), **sampling}
        inputs = {
            "model": model,
            "prompt": prompt,
            "images": images,
        }
        path = self._upload_text("input.json", json.dumps(inputs))
        return [self._python, self._remote + "/runner.py", path]

    def _poll_native(self):
        if not self._native.poll(
            {
                "claw": self._remote + "/traces",
                "calls": self._remote + "/calls.jsonl",
                "run": self._remote + "/status.json",
            }
        ):
            return
        events = next(
            (
                json_lines(text)
                for name, text in self._native.files.items()
                if name.startswith("claw/")
            ),
            [],
        )
        if events:
            calls = json_lines(self._native.files.get("calls/calls.jsonl", ""))
            trajectory, reply = convert(events, calls, self._output)
            self._output.update(metadata={"model": events[0]["model"]}, output=reply, sync=False)
            if trajectory["steps"]:
                self._output.save_trajectory(trajectory)
        if status := self._native.files.get("run/status.json"):
            status = json.loads(status)
            if status["reason"] == "completed":
                self._completed = True
            else:
                self._run_error = AgentError(ErrorType(status["reason"]), status["detail"])
