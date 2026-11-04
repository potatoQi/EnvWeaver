"""Exercise the public contract with real subprocesses and a filesystem test double.

This backend is not a sandbox and is not shipped with EnvWeaver.
"""

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from envweaver import Instance, Runtime, RuntimeRef, TaskPool, Verifier
from tests.conftest import run_process, run_verifier


class CompanyBackend:
    def __init__(self, root=None, *, host=None, ssh=None):
        self.root, self.host = Path(root), host
        self.started, self.closed = [], []

    def create(self, image, config=None, connection=None):
        return RuntimeRef(
            "company", str(Path(image).resolve(strict=True)), dict(config or {}),
            dict(connection or {}),
        )

    def start(self, ref, connection=None):
        directory = self.root / "instances" / uuid.uuid4().hex
        shutil.copytree(ref.image, directory)
        instance = Instance(
            directory.name,
            "company",
            host=self.host,
            data={"directory": str(directory), "python": sys.executable},
        )
        self.started.append(instance)
        return instance

    def describe(self, instance):
        return {"job": instance.id, "directory": instance.data["directory"]}

    def connect(self, record):
        return Instance(
            record["job"],
            "company",
            host=record["host"],
            data={"directory": record["directory"], "python": sys.executable},
        )

    def exec(self, instance, command, cwd=None, timeout=300):
        directory = Path(instance.data["directory"])
        temporary = directory / ".tmp"
        temporary.mkdir(exist_ok=True)
        try:
            result = run_process(
                command if isinstance(command, list) else ["/bin/sh", "-c", command],
                cwd=cwd or directory,
                env={**os.environ, "TMPDIR": str(temporary)},
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("Python execution timed out") from error
        return result.returncode, result.stdout.decode(), result.stderr.decode()

    def upload(self, instance, source, target):
        target = Path(target)
        if not target.is_absolute():
            target = Path(instance.data["directory"]) / target
        target.parent.mkdir(parents=True, exist_ok=True)
        if Path(source).is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)

    def download(self, instance, source, target):
        source = Path(source)
        if not source.is_absolute():
            source = Path(instance.data["directory"]) / source
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)

    def screenshot(self, instance):
        return b"PNG-from-company"

    def save(self, instance):
        target = self.root / "saved" / uuid.uuid4().hex
        shutil.copytree(instance.data["directory"], target)
        return RuntimeRef("company", str(target), {"company_setting": "preserved"})

    def close(self, instance):
        shutil.rmtree(instance.data["directory"])
        self.closed.append(instance)


@pytest.mark.parametrize("host", [None, "https://sandbox.company.example"])
def test_custom_backend_task_verifier_and_connect(tmp_path, host):
    runtime = Runtime(root=tmp_path / "storage", host=host)
    runtime.register(name="company", backend=CompanyBackend)
    backend = runtime._backends["company"]
    image = tmp_path / "base"
    image.mkdir()
    (image / "answer.txt").write_text("wrong")
    ref = runtime.create(backend="company", image=str(image))
    candidate = runtime.start(ref=ref)
    materials = tmp_path / "verifier"
    materials.mkdir()
    (materials / "run.py").write_text("""import argparse, json
from pathlib import Path
from envweaver import Runtime
p = argparse.ArgumentParser(); p.add_argument("--instance")
r = Runtime(); i = r.connect(path=p.parse_args().instance)
r.describe(instance=i, path="reconnected.json")
i = r.connect(path="reconnected.json")
assert r.screenshot(instance=i) == b"PNG-from-company"
r.upload(instance=i, source="test.txt", target="injected.txt")
code, out, err = r.exec(instance=i, command="cat injected.txt")
assert code == 0 and out == "test material"
r.download(instance=i, source="answer.txt", target="answer.txt")
code, out, err = r.exec(instance=i, command=["python3", "-c", "print('python works')"])
assert code == 0 and out.strip() == "python works"
print(json.dumps({"score": float(Path("answer.txt").read_text() == "correct")}))
""")
    (materials / "test.txt").write_text("test material")
    checker = Verifier(
        entry=materials / "run.py", timeout=10, runtime=runtime, requires_task_instance=True
    )
    pool = TaskPool(root=tmp_path / "pool", runtime=runtime)
    try:
        task_runtime_ref = runtime.save(instance=candidate)
        task_path = pool.save(
            cwd="/", verifier_runtime_ref=ref,
            task_runtime_ref=task_runtime_ref,
            name="company-task",
            goal=[{"type": "text", "text": "Write the answer."}],
            verifier=checker,
        )
        task = pool.load(task_ref=task_path)
        assert task.task_runtime_ref.backend == task.verifier_runtime_ref.backend == "company"
        assert task.task_runtime_ref.config == {"company_setting": "preserved"}
        checker = task.verifier
        assert run_verifier(checker, task.verifier_runtime_ref, task_instance=candidate) == {
            "score": 0
        }
        verifier_instance = runtime.start(ref=task.verifier_runtime_ref)
        try:
            record = tmp_path / "verifier-instance.json"
            runtime.describe(instance=verifier_instance, path=record)
            connected_verifier = runtime.connect(path=record)
            assert checker.run(verifier_instance=connected_verifier, task_instance=candidate) == {
                "score": 0
            }
            assert Path(verifier_instance.data["directory"]).is_dir()
            assert verifier_instance not in backend.closed
        finally:
            runtime.close(instance=verifier_instance)
        code, _, stderr = runtime.exec(
            instance=candidate,
            command=[sys.executable, "-c", "from pathlib import Path; Path('answer.txt').write_text('correct')"],
        )
        assert code == 0, stderr
        assert run_verifier(checker, task.verifier_runtime_ref, task_instance=candidate) == {
            "score": 1
        }
        record = tmp_path / "instance.json"
        runtime.describe(instance=candidate, path=record)
        assert json.loads(record.read_text())["job"] == candidate.id
        other = Runtime(root=tmp_path / "other")
        other.register(name="company", backend=CompanyBackend)
        connected = other.connect(path=record)
        assert connected.host == host
        assert (
            other.exec(instance=connected, command=[sys.executable, "-c", "print('connected')"])[1]
            == "connected\n"
        )
        restored = runtime.start(ref=task.task_runtime_ref)
        try:
            assert run_verifier(checker, task.verifier_runtime_ref, task_instance=restored) == {
                "score": 0
            }
        finally:
            runtime.close(instance=restored)
        assert all(i in backend.closed for i in backend.started if i != candidate)
    finally:
        runtime.close(instance=candidate)
    assert not list((runtime.root / "instances").iterdir())
