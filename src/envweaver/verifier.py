import base64
import json
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import quote

from ._io.paths import _location, normalize_address
from ._io.storage import local_source
from ._util import positive, run_python, write_json
from .errors import VerifierError
from .models import Instance
from .runtime import Runtime
from .runtime._relay import decode_file, encode_file


def _result(code, stdout, stderr):
    if code:
        raise VerifierError(f"Verifier exited with {code}: {stderr}")
    try:
        output = json.loads(stdout)
        score = output["score"]
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not 0 <= score <= 1
        ):
            raise ValueError("score must be a finite number in [0, 1]")
        json.dumps(output, allow_nan=False)
    except (ValueError, TypeError, KeyError) as error:
        raise VerifierError(f"Invalid verifier result: {error}; stderr: {stderr}") from error
    return output


def _entry_directory(entry):
    if not isinstance(entry, (str, Path)):
        raise ValueError("Verifier entry must be named run.py")
    address = normalize_address(entry)
    if isinstance(address, Path):
        path, host = address, None
    else:
        host, filename = _location(address)
        path = PurePosixPath(filename)
    if path.name != "run.py":
        raise ValueError("Verifier entry must be named run.py")
    return path.parent if host is None else host + quote(str(path.parent), safe="/")


class Verifier:
    """
    Args:
        entry: run.py 的路径。run.py 所在目录是验证材料包，保存 Task 和执行 Verify 时会复制整个目录。
        runtime: 用于操作验证沙箱和任务沙箱的沙箱管理器。
        timeout: run.py 的最长运行秒数，须大于 0。
        required_env: 验证程序需要的环境变量名称。
        requires_task_instance: 是否需要任务沙箱，默认 True；不需要时显式设为 False。

    Raises:
        ValueError: entry 地址无效或文件名不是 run.py、timeout 不是正数、required_env 中的名称无效或重复，或 requires_task_instance 不是布尔值。
    """

    def __init__(
        self,
        entry: str | Path,
        *,
        runtime: Runtime,
        timeout: float,
        required_env: list[str] | None = None,
        requires_task_instance: bool = True,
    ):
        _entry_directory(entry)
        positive(timeout, "Verifier timeout")
        if required_env is None:
            required_env = []
        if not isinstance(required_env, list) or any(
            not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
            for name in required_env
        ):
            raise ValueError("required_env must be a list of environment variable names")
        if len(set(required_env)) != len(required_env):
            raise ValueError("required_env names must be unique")
        if not isinstance(requires_task_instance, bool):
            raise ValueError("requires_task_instance must be a boolean")
        self.entry = entry
        self.timeout = timeout
        self.required_env = list(required_env)
        self.runtime = runtime
        self.requires_task_instance = requires_task_instance

    @contextmanager
    def _local_entry(self):
        with local_source(_entry_directory(self.entry)) as directory:
            entry = (directory / "run.py").resolve(strict=True)
            if not entry.is_file():
                raise IsADirectoryError(f"Verifier entry is not a file: {entry}")
            yield entry

    def run(
        self,
        verifier_instance: Instance,
        *,
        task_instance: Instance | None = None,
        files: list[dict[str, str | Path]] | None = None,
        env: dict[str, str] | None = None,
    ) -> dict:
        """
        Args:
            verifier_instance: 验证沙箱。
            task_instance: 任务沙箱；requires_task_instance 为 True 时必填，为 False 时不能传入。
            files: 要复制的文件或目录列表。source 填来源路径；target 是验证沙箱内的路径，相对路径以 run.py 所在目录为基准。
            env: 传给验证程序的环境变量，须包含 required_env 中的全部变量。

        Returns:
            run.py 输出的 JSON 对象。

        Raises:
            ValueError: task_instance 与 requires_task_instance 不匹配、文件映射或环境变量无效，或缺少 required_env 中的变量。
            OSError: 验证材料或输入文件无法读取。
            envweaver.errors.VerifierError: 验证超时、执行失败或结果格式错误。
            envweaver.errors.RuntimeError: 材料传输或沙箱操作失败。
            TimeoutError: 材料传输超时。
            TypeError: verifier_instance 或 task_instance 不是 Instance。
        """
        if not isinstance(verifier_instance, Instance):
            raise TypeError("verifier_instance must be an Instance")
        if task_instance is not None and not isinstance(task_instance, Instance):
            raise TypeError("task_instance must be an Instance")
        if self.requires_task_instance and task_instance is None:
            raise ValueError("task_instance is required when requires_task_instance=True")
        if not self.requires_task_instance and task_instance is not None:
            raise ValueError("task_instance must be omitted when requires_task_instance=False")
        return _Evaluation(self, verifier_instance).run(task_instance, files, env)


class _Evaluation:
    """State belonging to one verification call."""

    def __init__(self, verifier, verifier_instance):
        self.verifier = verifier
        self.runtime = verifier.runtime
        self._instance = verifier_instance

    def run(self, instance, files, env):
        environment = self._environment(env)
        with self.verifier._local_entry() as entry:
            inputs = self._files(files, entry.parent)
            executor = ThreadPoolExecutor(max_workers=1) if instance is not None else None
            try:
                return self._run(instance, entry, inputs, environment, executor)
            finally:
                # On failure, return control so the caller can close its sandbox.
                if executor is not None:
                    executor.shutdown(wait=False, cancel_futures=True)

    def _environment(self, env):
        environment = {}
        for name, value in (env or {}).items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("Invalid environment variable name")
            if not isinstance(value, str) or "\0" in value:
                raise ValueError("Environment variable values must be strings without NUL")
            environment[name] = value
        missing = [name for name in self.verifier.required_env if name not in environment]
        if missing:
            raise ValueError("Missing verifier environment variables: " + ", ".join(missing))
        return environment

    @staticmethod
    def _files(files, materials):
        inputs = {}
        if files is not None and not isinstance(files, list):
            raise ValueError("files must be a list of source/target mappings")
        for item in files or []:
            if not isinstance(item, dict) or set(item) != {"source", "target"}:
                raise ValueError("Each file mapping must contain source and target")
            name, source = item["target"], item["source"]
            if not isinstance(name, str) or not name or "\0" in name:
                raise ValueError("File destinations must be nonempty paths without NUL")
            path = (
                PureWindowsPath(name)
                if PureWindowsPath(name).is_absolute()
                else PurePosixPath(name)
            )
            if (
                not path.name
                or ".." in path.parts
                or (
                    not path.is_absolute()
                    and (
                        "\\" in name
                        or ":" in name
                        or any(part in ("", ".", "..") for part in name.split("/"))
                    )
                )
            ):
                raise ValueError(f"Invalid file destination: {name}")
            name = path.as_posix()
            if not path.is_absolute():
                target = materials / name
                if target.exists() or target.is_symlink():
                    raise ValueError(f"Input would overwrite verification materials: {name}")
                for parent in target.parents:
                    if parent == materials:
                        break
                    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                        raise ValueError(f"Invalid input directory: {name}")
            if any(
                name == other or name.startswith(other + "/") or other.startswith(name + "/")
                for other in inputs
            ):
                raise ValueError(f"Overlapping input destinations: {name}")
            address = normalize_address(source)
            inputs[name] = address.resolve(strict=True) if isinstance(address, Path) else address
        return inputs

    def _execute(self, source, timeout=60):
        return run_python(self.runtime, self._instance, source, timeout)

    def _run(self, instance, entry, inputs, environment, executor):
        code, directory, stderr = self._execute(
            "import tempfile; from pathlib import Path; print(Path(tempfile.mkdtemp(prefix='envweaver-verifier-')).as_posix())"
        )
        if code:
            raise VerifierError(f"Cannot prepare verifier directory: {stderr}")
        remote = PurePosixPath(directory.strip())
        with tempfile.TemporaryDirectory(prefix="envweaver-relay-") as directory:
            local = Path(directory)
            arguments = []
            if instance is not None:
                write_json(
                    local / "instance.json",
                    {
                        "version": 1,
                        "backend": "_verifier",
                        "id": instance.id,
                        "directory": str(remote / "mailbox"),
                        "timeout": self.verifier.timeout,
                    },
                    private=True,
                )
                arguments = ["--instance", str(remote / "control/instance.json")]
            write_json(local / "environment.json", environment, private=True)
            self.runtime.upload(
                instance=self._instance, source=entry.parent, target=str(remote / "verifier")
            )
            self.runtime.upload(
                instance=self._instance, source=local, target=str(remote / "control")
            )
            for name, path in inputs.items():
                target = (
                    name
                    if (PurePosixPath(name).is_absolute() or PureWindowsPath(name).is_absolute())
                    else str(remote / "verifier" / name)
                )
                self.runtime.upload(instance=self._instance, source=path, target=target)
            source = (
                "import json, os, subprocess, sys; from pathlib import Path; "
                f"os.environ.update(json.loads(Path({str(remote / 'control/environment.json')!r}).read_text())); "
                f"os.chdir({str(remote / 'verifier')!r}); "
            )
            if instance is not None:
                source += f"Path({str(remote / 'mailbox')!r}).mkdir(); "
            source += f"code = subprocess.call([sys.executable, 'run.py'] + {arguments!r}); "
            if instance is not None:
                source += f"Path({str(remote / 'done')!r}).touch(); "
            source += "raise SystemExit(code)"
            try:
                if instance is None:
                    return _result(*self._execute(source, self.verifier.timeout))
                deadline = time.monotonic() + self.verifier.timeout
                pending = executor.submit(self._execute, source, self.verifier.timeout)
                while not pending.done():
                    if time.monotonic() < deadline:
                        self._forward(remote / "mailbox", local, instance, deadline)
                    time.sleep(0.05)
                return _result(*pending.result())
            except (TimeoutError, subprocess.TimeoutExpired) as error:
                raise VerifierError(f"Verifier timed out after {self.verifier.timeout}s") from error

    def _forward(self, mailbox, local, instance, deadline):
        poll_time = min(0.5, max(0, deadline - time.monotonic() - 0.1))
        code, output, stderr = self._execute(
            "import json, time; from pathlib import Path; "
            f"directory = Path({str(mailbox)!r}); end = time.monotonic() + {poll_time!r}\n"
            "while True:\n"
            " files = list(directory.glob('*.request'))\n"
            " if files or (directory.parent / 'done').exists() or time.monotonic() >= end: break\n"
            " time.sleep(0.05)\n"
            "requests = []\n"
            "for file in files:\n requests.append(json.loads(file.read_text())); file.unlink()\n"
            "print(json.dumps(requests))",
            timeout=max(1, deadline - time.monotonic()),
        )
        if code:
            raise VerifierError(f"Cannot read verifier requests: {stderr}")
        for request in json.loads(output):
            request_id = request["id"]
            if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
                raise VerifierError("Invalid verifier request ID")
            try:
                result = self._dispatch(instance, request, local, deadline)
                response = {"result": result}
            except Exception as error:
                response = {
                    "error": str(error),
                    "timeout": isinstance(error, (TimeoutError, subprocess.TimeoutExpired)),
                }
            path = local / (request_id + ".response")
            write_json(path, response, private=True)
            temporary = str(mailbox / (request_id + ".tmp"))
            payload = path.read_bytes()
            if len(payload) <= 4096:
                prepare = f"from pathlib import Path; Path({temporary!r}).write_bytes(bytes.fromhex({payload.hex()!r})); "
            else:
                self.runtime.upload(instance=self._instance, source=path, target=temporary)
                prepare = ""
            code, _, stderr = self._execute(
                prepare + f"import os; os.replace({temporary!r}, {str(mailbox / path.name)!r})"
            )
            if code:
                raise VerifierError(f"Cannot deliver verifier response: {stderr}")

    def _dispatch(self, instance, request, local, deadline):
        operation, arguments = request["operation"], request["arguments"]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Verification session timed out")
        if operation == "exec":
            timeout = arguments.get("timeout")
            return self.runtime.exec(
                instance=instance,
                command=arguments["command"],
                cwd=arguments.get("cwd"),
                timeout=min(remaining, timeout) if timeout is not None else remaining,
            )
        if operation == "upload":
            with tempfile.TemporaryDirectory(dir=local) as directory:
                source = Path(directory) / "data"
                decode_file(arguments["file"], source)
                self.runtime.upload(instance=instance, source=source, target=arguments["target"])
            return None
        if operation == "download":
            with tempfile.TemporaryDirectory(dir=local) as directory:
                target = Path(directory) / "data"
                self.runtime.download(instance=instance, source=arguments["source"], target=target)
                return encode_file(target)
        if operation == "screenshot":
            return base64.b64encode(self.runtime.screenshot(instance=instance)).decode()
        raise ValueError(f"Unsupported verifier operation: {operation}")
