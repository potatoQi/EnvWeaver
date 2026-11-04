import json
import os
import shutil
import signal
import subprocess
import uuid
from copy import deepcopy
from pathlib import Path

import pytest

from envweaver import Instance, Runtime, RuntimeRef


class FilesystemBackend:
    """A test double, deliberately not a shipped/local 'sandbox' backend."""

    def __init__(self, root, *, host=None, ssh=None):
        self.root = root

    def create(self, image, config=None, connection=None):
        return RuntimeRef(
            "test",
            str(Path(image).resolve(strict=True)),
            dict(config or {}),
            deepcopy(connection or {}),
        )

    def start(self, ref, connection=None):
        directory = self.root / uuid.uuid4().hex
        shutil.copytree(ref.image, directory)
        return Instance(
            directory.name,
            "test",
            str(self.root),
            {"path": str(directory), "connection": deepcopy(connection or {})},
        )

    def connect(self, record):
        return Instance(
            record["id"],
            "test",
            str(self.root) if self.root else None,
            record["data"],
            record.get("host"),
        )

    def describe(self, instance):
        return {"id": instance.id, "data": instance.data}

    def upload(self, instance, local_path, remote_path):
        target = Path(instance.data["path"]) / remote_path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        if Path(local_path).is_dir():
            shutil.copytree(local_path, target, dirs_exist_ok=True)
        else:
            shutil.copy2(local_path, target)

    def save(self, instance):
        destination = self.root / uuid.uuid4().hex
        shutil.copytree(instance.data["path"], destination)
        return RuntimeRef(
            "test", str(destination), connection=deepcopy(instance.data.get("connection", {}))
        )

    def close(self, instance):
        shutil.rmtree(instance.data["path"])


@pytest.fixture
def runtime(tmp_path):
    runtime = Runtime(root=tmp_path / "runtime")
    runtime.root.mkdir()
    runtime.register(name="test", backend=FilesystemBackend)
    return runtime


def stop_process(process):
    """Terminate our subprocess group, including shell children, on POSIX."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
    process.wait()


def run_process(command, *, timeout=None, **kwargs):
    process = subprocess.Popen(
        command,
        stdout=kwargs.pop("stdout", subprocess.PIPE),
        stderr=kwargs.pop("stderr", subprocess.PIPE),
        start_new_session=os.name == "posix",
        **kwargs,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        stop_process(process)
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


@pytest.fixture
def ssh_instance(tmp_path, monkeypatch):
    """Exercise the real SSH wire payload via a local subprocess transport."""
    import shlex
    import subprocess
    import sys
    from types import SimpleNamespace

    from envweaver.runtime import _ssh

    def run(args, **kwargs):
        executable, flag, source = shlex.split(args[-1])
        assert flag == "-c"
        return subprocess.run([sys.executable, flag, source], **kwargs)

    monkeypatch.setattr(
        _ssh, "subprocess", SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)
    )
    return Instance(
        "ssh-test",
        "qemu",
        data={"ssh": {"host": "test", "user": "worker"}, "python": sys.executable},
    )


@pytest.fixture
def run_native_verifier(tmp_path):
    """Test the independent run.py contract without a Verifier wrapper."""
    import subprocess
    import sys

    def run(entry, instance, timeout=300):
        descriptor = tmp_path / "native-instance.json"
        Runtime().describe(instance=instance, path=descriptor)
        result = subprocess.run(
            [sys.executable, str(entry), "--instance", str(descriptor)],
            cwd=Path(entry).parent,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    return run


def run_verifier(checker, ref, *, task_instance=None, connection=None, **kwargs):
    """Test caller owns one sandbox per evaluation."""
    instance = checker.runtime.start(ref=ref, connection=connection)
    try:
        return checker.run(verifier_instance=instance, task_instance=task_instance, **kwargs)
    finally:
        checker.runtime.close(instance=instance)
