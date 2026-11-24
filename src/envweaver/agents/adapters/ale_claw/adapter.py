"""Host-side ALE Claw controller for an Eval-owned desktop."""

import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from ....errors import AgentError, ErrorType
from ..._redaction import redact_data
from ...base import Agent
from .._provider_http import provider_failure
from .desktop import desktop
from .trajectory import export, read_calls

_REPO = "https://github.com/potatoQi/agents-last-exam"
_BRANCH = "standalone-ale-claw"


def _failure(report, calls):
    if kind := report.get("error_type"):
        try:
            return ErrorType(kind)
        except ValueError:
            pass
    if calls and (kind := provider_failure(calls[-1])):
        return kind
    return provider_failure({"status": report.get("status_code")}) or ErrorType.UNKNOWN


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class AleClawAgent(Agent):
    """
    Args:
        model: 做题模型，格式为 `provider/model`，例如 `openai/Qwen3.8-27B`。
        options: AleClawConfig 的其余参数，如 base_url、api_key、max_turns。默认空表。
        port: 做题实例内的 CUA 服务端口，默认 8000。
        version: Git 提交或标签。省略时优先使用当前 Python 环境中的 ale-claw；
            未安装时使用 Git 和 uv 安装 potatoQi/agents-last-exam 的 standalone-ale-claw 分支最新提交。

    Raises:
        ValueError: 端口、版本或 options 无效。
        TypeError: 配置无法保存为 JSON。
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        options: dict | None = None,
        port: int = 8000,
        version: str | None = None,
    ):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if options is not None and (not isinstance(options, dict) or "model" in options):
            raise ValueError("options must be a mapping; set model separately")
        if version is not None and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/-]*", version):
            raise ValueError("version must be a Git revision")
        super().__init__(model=model, options=options or {}, port=port, version=version)
        self._lock = threading.Lock()
        self._stopped = False
        self._process = None
        self._controller = None
        cfg = self.config
        options = cfg["options"]
        if cfg["model"]:
            options["model"] = cfg["model"]
        self._options = options
        settings = options.get("extra_generation_kwargs")
        sources = [os.environ, options]
        if isinstance(settings, dict):
            sources.extend(
                settings[k]
                for k in ("headers", "extra_headers")
                if isinstance(settings.get(k), dict)
            )
        self._secrets = sorted(
            {
                value
                for source in sources
                for key, value in source.items()
                if isinstance(value, str)
                and value
                and any(
                    word in key.lower()
                    for word in ("key", "token", "secret", "password", "authorization")
                )
            },
            key=len,
            reverse=True,
        )

    def preflight(self, goal):
        if any(block["type"] != "text" for block in goal):
            raise ValueError("AleClawAgent accepts a text goal; put task files in Task.context")

    def _clean(self, value):
        def redact(text):
            for token in self._secrets:
                text = text.replace(token, "[REDACTED]")
            return text

        return redact_data(value, redact)

    def _execute(self, cmd, cwd, payload=None, *, preparing=False):
        data = json.dumps(payload, default=os.fspath).encode() if payload is not None else None
        with tempfile.TemporaryFile() as errors:
            with self._lock:
                if self._stopped:
                    raise InterruptedError("ALE Claw stopped")
                process = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=errors,
                    start_new_session=True,
                )
                self._process = process
            try:
                data, _ = process.communicate(data)
            except BaseException:
                _kill_group(process)
                process.wait()
                raise
            finally:
                with self._lock:
                    self._process = None
            if self._stopped:
                raise InterruptedError("ALE Claw stopped")
            if process.returncode:
                errors.seek(max(0, errors.tell() - 3000))
                raise AgentError(
                    ErrorType.CONFIGURATION if preparing else ErrorType.UNKNOWN,
                    self._clean(errors.read().decode(errors="replace")),
                )
            return data.decode().strip()

    def prepare(self, *, runtime, instance, output, cwd):
        if instance.backend not in ("docker", "qemu"):
            raise AgentError(
                ErrorType.CONFIGURATION, "AleClawAgent requires a Docker or QEMU instance"
            )
        python = instance.data.get("python", "python3")
        code, name, err = runtime.exec(
            instance=instance,
            command=[
                python,
                "-c",
                "import platform,socket,sys; "
                "socket.create_connection(('127.0.0.1',int(sys.argv[1])),5).close(); "
                "print(platform.system().lower())",
                str(self._config["port"]),
            ],
            cwd=cwd,
            timeout=15,
        )
        if code or name.strip() not in ("linux", "windows"):
            raise AgentError(
                ErrorType.SANDBOX,
                "Expected a reachable CUA service in a Linux or Windows task instance: "
                + self._clean(err),
            )
        self._os = name.strip()
        if self._controller is None:
            # Direct prepare/run callers own a single controller for the life of the Agent.
            env = tempfile.TemporaryDirectory(prefix="envweaver-ale-host-")
            try:
                self._controller = self._prepare_controller(env.name)
            except BaseException:
                env.cleanup()
                raise
            self._host_env = env
        _, version, model = self._controller
        output.update(metadata={"agent_version": version, "model": model})

    @contextmanager
    def _batch_agents(self):
        with tempfile.TemporaryDirectory(prefix="envweaver-ale-host-") as directory:
            # Concurrent batches may use the same original Agent.
            controller = self.new()._prepare_controller(directory)

            def create():
                agent = self.new()
                agent._controller = controller
                return agent

            yield create

    def _prepare_controller(self, directory):
        revision = self._config["version"]
        python = sys.executable
        if revision is not None or not importlib.util.find_spec("ale_claw"):
            if not shutil.which("uv"):
                raise AgentError(
                    ErrorType.CONFIGURATION, "Install uv or install ale-claw from GitHub"
                )
            if revision is None:
                head = self._execute(
                    ["git", "ls-remote", "--exit-code", _REPO, f"refs/heads/{_BRANCH}"],
                    directory,
                    preparing=True,
                )
                revision = head.split()[0]
            requirement = (
                f"ale-claw @ {_REPO}/archive/{revision}.tar.gz#subdirectory=ale_run/agents/ale_claw"
            )
            venv = Path(directory) / "venv"
            python = str(venv / "bin/python")
            self._execute(["uv", "venv", "--python", "3.12", str(venv)], directory, preparing=True)
            self._execute(
                ["uv", "pip", "install", "--python", python, requirement], directory, preparing=True
            )
        version, model = json.loads(
            self._execute(
                [python, str(Path(__file__).with_name("worker.py")), "prepare"],
                directory,
                self._options,
                preparing=True,
            )
        )
        return python, version, model

    def run(self, *, goal, runtime, instance, output, cwd):
        self.preflight(goal)
        prompt = f"Task working directory on the remote desktop: {cwd}\n\n" + "\n".join(
            b["text"] for b in goal
        )
        with tempfile.TemporaryDirectory(prefix="envweaver-ale-") as directory:
            root = Path(directory)
            with desktop(runtime, instance, self._config["port"]) as url:
                job = {"agent": self._options, "prompt": prompt, "url": url, "os": self._os}
                try:
                    report = json.loads(
                        self._execute(
                            [self._controller[0], str(Path(__file__).with_name("worker.py"))],
                            root,
                            job,
                        )
                    )
                finally:
                    calls = []
                    # Preserve partial evidence without masking a cancellation or native error.
                    try:
                        calls = read_calls(root, output, self._clean)
                        final = export(root, prompt, output, self._clean, calls)
                    except Exception as exc:
                        output.update(metadata={"trajectory_export_error": self._clean(str(exc))})
                        final = ""
            if report["status"] != "completed":
                error = self._clean(report.get("error") or report["status"])
                raise AgentError(_failure(report, calls), error)
            return output.finish([{"type": "text", "text": final}] if final else [])

    def stop(self, timeout: float = 30) -> None:
        with self._lock:
            self._stopped = True
            process = self._process
            if process is not None and process.poll() is None:
                _kill_group(process)
        if process is not None:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                raise TimeoutError("ALE Claw did not stop") from error
