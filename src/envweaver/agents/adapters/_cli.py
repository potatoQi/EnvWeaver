import base64
import codecs
import json
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from ..._io.retry import retry
from ...errors import AgentError, ErrorType
from .._redaction import redact_data
from ..base import Agent
from ..trajectory.capture import NativeFiles
from ._downloads import PreparationDownloads

_POLL_INTERVAL = 2.0


class CliAgent(Agent):
    """Shared sandbox process transport; provider-specific commands stay in adapters."""

    executable = ""
    package = ""
    dependency = ""
    stdout_events = False
    supports_images = True
    managed_options = ()

    def __init__(
        self,
        *,
        model: str | None = None,
        version: str | None = None,
        env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
        cache: bool = True,
    ):
        if not isinstance(cache, bool):
            raise TypeError("cache must be a bool")
        if env is not None and (
            not isinstance(env, dict)
            or any(
                not isinstance(k, str)
                or not k
                or "=" in k
                or "\0" in k
                or not isinstance(v, str)
                or "\0" in v
                for k, v in env.items()
            )
        ):
            raise ValueError("env must map environment variable names to strings without NUL")
        if extra_args is not None and (
            not isinstance(extra_args, list)
            or any(not isinstance(v, str) or "\0" in v for v in extra_args)
        ):
            raise ValueError("extra_args must be a list of strings")
        super().__init__(model=model, version=version, env=dict(env or {}),
                         extra_args=list(extra_args or []), cache=cache)
        self.metadata["agent"] = self.executable
        self._output = None
        self._cancelled = threading.Event()
        config = self.config
        self._env = config["env"]
        self._extra_args = config["extra_args"]
        self._remote = None
        self._process_dir = None
        self._process_lock = threading.Lock()
        self._event_buffer = ""
        self._run_error = None
        self._completed = False
        self._dependency_env = {}
        self._downloads = None
        self._native = None

    def preflight(self, goal):
        if not self.supports_images and any(block["type"] == "image" for block in goal):
            raise ValueError(f"{self.executable} adapter does not support image goals")
        reserved = {"--help", "-h", "--version", *self.managed_options}
        if any(arg.split("=", 1)[0] in reserved for arg in self._extra_args):
            raise ValueError("extra_args cannot override adapter output or execution options")

    def _rpc(self, action, directory, payload=None):
        try:
            code, stdout, stderr = retry(lambda: self._runtime.exec(
                instance=self._instance,
                command=[
                    self._python,
                    self._remote + "/process.py",
                    action,
                    directory,
                    json.dumps(payload or {}),
                ],
                timeout=30,
            ), replay=action in {"poll", "read", "stop"},
                cancelled=None if action == "stop" or self._cancelled.is_set() else self._cancelled)
        except (ConnectionError, TimeoutError, subprocess.TimeoutExpired) as error:
            raise AgentError(
                ErrorType.TRANSPORT, "Agent sandbox transport failed",
            ) from error
        if code:
            raise AgentError(
                ErrorType.SANDBOX, f"Agent process transport failed: {self._redact(stderr)}",
            )
        return json.loads(stdout)

    def _redact(self, text):
        for key, value in self._env.items():
            if value and any(
                word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")
            ):
                text = text.replace(value, "[REDACTED]")
                text = text.replace(json.dumps(value, ensure_ascii=False)[1:-1], "[REDACTED]")
        return text

    def prepare(self, *, runtime, instance, output, cwd: str):
        self._runtime, self._instance = runtime, instance
        self._output = output
        self._cwd = cwd
        self._check_stopped()
        self._python = self._instance.data.get("python", "python3")
        self._native = NativeFiles(
            runtime=runtime, instance=instance, python=self._python, output=output, redact=self._redact,
        )
        code, stdout, stderr = self._runtime.exec(
            instance=self._instance,
            command=[
                self._python,
                "-c",
                "import sys, tempfile, json, os; "
                "assert sys.platform == 'linux', 'Built-in agents require a Linux sandbox'; "
                "print(json.dumps({'directory': tempfile.mkdtemp(prefix='envweaver-agent-'), "
                "'path': os.environ.get('PATH', ''), 'pythonpath': os.environ.get('PYTHONPATH', '')}))",
            ],
            timeout=30,
        )
        if code:
            raise RuntimeError(f"Cannot create Agent directory: {stderr}")
        environment = json.loads(stdout)
        self._remote = environment["directory"]
        self._sandbox_path = self._env.get("PATH", environment["path"])
        self._sandbox_pythonpath = self._env.get("PYTHONPATH", environment["pythonpath"])
        self._check_stopped()
        self._runtime.upload(
            instance=self._instance,
            source=Path(__file__).with_name("_process.py"),
            target=self._remote + "/process.py",
        )
        code, output = self._execute(
            self._version_command(), preparing=True, capture_stdout=True
        )
        requested = self._output.result["metadata"]["agent_version"]
        if code or requested is not None:
            try:
                if self.config["cache"]:
                    self._downloads = PreparationDownloads(
                        runtime=runtime, instance=instance, directory=self._remote + "/downloads",
                        env=self._env, cancelled=self._cancelled, redact=self._redact,
                        helpers=self._download_helpers(),
                    )
                    self._downloads.setup()
                self._ensure_dependencies()
                command = self._install_command(requested)
                code, _ = self._execute(command, preparing=True)
                if code:
                    raise RuntimeError(
                        f"{self.executable} installation failed; see logs/prepare.log"
                    )
            finally:
                if self._downloads is not None:
                    original_error = sys.exception()
                    try:
                        self._downloads.close()
                    except Exception as error:
                        if original_error is None:
                            raise
                        original_error.add_note(f"Preparation download cleanup failed: {error}")
                    finally:
                        self._downloads = None
            code, output = self._execute(
                self._version_command(), preparing=True, capture_stdout=True
            )
            if code:
                raise RuntimeError(f"{self.executable} version check failed")
        self._output.update(metadata={"agent_version": output.strip() or None})

    def _version_command(self):
        return [self.executable, "--version"]

    def _download_helpers(self):
        return ("_npm_download.cjs",) if self.dependency == "node" else ("_pip_install.py",)

    def _install_command(self, version):
        return [
            "npm",
            "install",
            "--prefix",
            self._remote + "/npm",
            self.package + (f"@{version}" if version else ""),
        ]

    def _ensure_dependencies(self):
        root = self._remote + "/dependencies"
        kind = self.dependency
        if kind == "pip":
            paths = [self._remote + "/packages", root + "/pip", self._sandbox_pythonpath]
            self._dependency_env["PYTHONPATH"] = ":".join(path for path in paths if path)
        elif kind == "node":
            self._dependency_env["PATH"] = ":".join(
                [root + "/node/bin", self._remote + "/npm/node_modules/.bin", self._sandbox_path]
            )
            self._dependency_env["npm_config_cache"] = self._env.get(
                "npm_config_cache", self._remote + "/npm-cache"
            )
        self._runtime.upload(
            instance=self._instance,
            source=Path(__file__).with_name("_dependencies.py"),
            target=self._remote + "/dependencies.py",
        )
        code, _ = self._execute(
            [self._python, self._remote + "/dependencies.py", kind, root], preparing=True
        )
        if code:
            raise RuntimeError(f"Cannot prepare {kind} dependencies; see logs/prepare.log")

    def _execute(self, command, *, preparing=False, capture_stdout=False):
        self._check_stopped()
        directory = self._remote + "/process-" + uuid.uuid4().hex
        with self._process_lock:
            self._process_dir = directory
        offsets = {"stdout": 0, "stderr": 0}
        decoders = {name: codecs.getincrementaldecoder("utf-8")("replace") for name in offsets}
        captured = []
        stopped = False
        finished = False
        try:
            environment = self._process_env()
            if preparing and self._downloads is not None:
                environment["ENVWEAVER_DOWNLOAD_REQUESTS"] = self._downloads.directory
                if self.dependency == "node":
                    environment["NODE_OPTIONS"] = (
                        environment.get("NODE_OPTIONS", "") + " --require "
                        + self._remote + "/_npm_download.cjs"
                    ).strip()
            response = self._rpc(
                "start",
                directory,
                {"command": command, "env": environment, "cwd": self._cwd},
            )
            if not response["started"]:
                raise InterruptedError("Agent stopped before process launch")
            while True:
                served = False
                if preparing and self._downloads is not None:
                    served = self._downloads.serve()
                if self._cancelled.is_set() and not stopped:
                    self._rpc("stop", directory)
                    stopped = True
                wait = (
                    _POLL_INTERVAL
                    if preparing and (self._downloads is None or self._downloads.streaming)
                    else 0
                )
                response = self._rpc("poll", directory, {**offsets, "wait": wait})
                finished = response["status"] is not None
                for name, chunk in response["streams"].items():
                    offsets[name] = chunk["offset"]
                    raw = base64.b64decode(chunk["data"])
                    text = decoders[name].decode(raw)
                    if text:
                        self._consume(name, text, preparing)
                        if capture_stdout and name == "stdout":
                            captured.append(text)
                if not preparing:
                    self._poll_native()
                if finished and all(
                    offsets[name] >= end for name, end in response["status"]["offsets"].items()
                ):
                    for name, decoder in decoders.items():
                        text = decoder.decode(b"", final=True)
                        if text:
                            self._consume(name, text, preparing)
                    if not preparing and self._event_buffer.strip():
                        self._event_line(self._event_buffer)
                        self._event_buffer = ""
                    if response["status"].get("error"):
                        self._output.write_log(
                            "prepare.log" if preparing else "stderr.log",
                            self._redact(response["status"]["error"]) + "\n",
                        )
                    self._check_stopped()
                    return response["status"]["code"], "".join(captured)
                if not wait:
                    self._cancelled.wait(
                        0 if served else _POLL_INTERVAL
                    ) if not stopped else threading.Event().wait(_POLL_INTERVAL)
        finally:
            # Only interrupt a command which has not reported its exit.
            original_error = sys.exception()
            try:
                if not finished:
                    self._terminate(directory)
            except Exception as error:
                if original_error is None:
                    raise
                original_error.add_note(f"Agent process cleanup failed: {error}")
            else:
                with self._process_lock:
                    self._process_dir = None

    def _terminate(self, directory, timeout=30):
        self._rpc("stop", directory)
        deadline = time.monotonic() + timeout
        while self._rpc("poll", directory)["status"] is None:
            if time.monotonic() >= deadline:
                raise TimeoutError("Agent process did not stop")
            time.sleep(0.05)

    def _process_env(self):
        return {**self._env, **self._dependency_env}

    def _consume(self, name, text, preparing):
        self._output.write_log("prepare.log" if preparing else name + ".log", self._redact(text))
        if name == "stdout" and not preparing and self.stdout_events:
            self._event_buffer += text
            while "\n" in self._event_buffer:
                line, self._event_buffer = self._event_buffer.split("\n", 1)
                if line.strip():
                    self._event_line(line)

    def _event_line(self, line):
        try:
            event = redact_data(json.loads(line), self._redact)
        except ValueError:
            self._output.record(self.executable + ".stdout", {"text": self._redact(line)})
            return
        if not isinstance(event, dict):
            self._output.record(self.executable + ".stdout", event)
            return
        self._output.record(self.executable + "." + str(event.get("type", "event")), event)
        self._on_event(event)

    def _on_event(self, event):
        pass

    def _poll_native(self):
        pass

    def _read_remote(self, path):
        data = self._rpc("read", self._remote, {"path": path})["data"]
        return None if data is None else base64.b64decode(data)

    def run(self, *, goal, runtime, instance, output, cwd: str):
        prompt, images = [], []
        for index, block in enumerate(goal):
            self._check_stopped()
            if block["type"] == "text":
                prompt.append(block["text"])
            else:
                source = Path(block["path"])
                target = self._remote + f"/input-{index}" + source.suffix
                self._runtime.upload(instance=self._instance, source=source, target=target)
                images.append(target)
                prompt.append(f"[Image {len(images)}: {target}]")
        from ..trajectory import add_step, document

        initial = document(output.result["metadata"], Path(self._remote).name)
        parts = []
        for block in goal:
            if block["type"] == "text":
                parts.append(dict(block))
            else:
                import mimetypes

                from ..trajectory.capture import Media

                path = Path(block["path"])
                saved = output.save_file(path.read_bytes(), path.suffix)
                parts.append(Media(output).image(saved, mimetypes.guess_type(str(path))[0]))
        add_step(initial, "user", parts)
        initial["extra"] = {"capture": {"complete": False, "missing": ["native_model_calls"]}}
        output.save_trajectory(initial)
        try:
            code, _ = self._execute(self._command("\n".join(prompt), images))
            self._finish()
        finally:
            original_error = sys.exception()
            try:
                self._poll_native()
            except Exception as error:
                if original_error is None:
                    raise
                original_error.add_note(f"Final trajectory capture failed: {error}")
        if self._run_error is not None and code >= 0:
            raise self._run_error
        if code:
            raise RuntimeError(f"{self.executable} exited with code {code}; see Agent logs")
        if not self._completed:
            raise AgentError(
                ErrorType.PROVIDER_TRANSIENT,
                f"{self.executable} exited without a completed response; see Agent logs",
            )
        return self._output.result

    def _finish(self):
        pass

    def stop(self, timeout=30):
        self._cancelled.set()
        with self._process_lock:
            directory = self._process_dir
        if directory is not None:
            self._terminate(directory, timeout=timeout)

    def _check_stopped(self):
        if self._cancelled.is_set():
            raise InterruptedError("Agent stopped")

    def _upload_text(self, filename, text):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / filename
            path.write_text(text, encoding="utf-8")
            target = self._remote + "/" + filename
            self._runtime.upload(instance=self._instance, source=path, target=target)
        return target
