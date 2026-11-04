"""Ship a Task to a stdlib-only consumer and a separate verifier container."""

import base64
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from envweaver import Runtime, TaskPool, Verifier
from envweaver._io import ssh_transport as _ssh_transport
from tests.conftest import run_verifier

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ENVWEAVER_TEST_DOCKER"), reason="requires a prepared Docker image"
    ),
]


class RecordingRuntime(Runtime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instances = []
        self.closed = []

    def start(self, *args, **kwargs):
        instance = super().start(*args, **kwargs)
        self.instances.append(instance)
        return instance

    def close(self, instance):
        super().close(instance=instance)
        self.closed.append(instance)


def test_independent_downstream_complete_task(tmp_path, request, monkeypatch):
    monkeypatch.setenv("HOST_ONLY_SECRET", "must-not-enter-checker")
    project = Path(__file__).parents[2]
    fixtures = Path(__file__).parents[1] / "fixtures/downstream"
    wheels = tmp_path / "wheels"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheels)],
        cwd=project,
        check=True,
        capture_output=True,
    )
    host = os.environ.get("ENVWEAVER_TEST_DOCKER_HOST")
    connection = ["--host", host] if host else ["--context", "default"]
    verifier_image = "envweaver/downstream-verifier:" + uuid.uuid4().hex
    python_version = subprocess.run(
        [
            "docker", *connection, "run", "--rm", "--pull=never", "--network", "none",
            "--entrypoint", "python3", os.environ["ENVWEAVER_TEST_DOCKER"],
            "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()
    # Prepare all software before building; the verification image is usable offline.
    download = subprocess.run(
        [
            "uv",
            "tool",
            "run",
            "--from",
            "pip",
            "pip",
            "download",
            "--only-binary=:all:",
            "--python-version",
            python_version,
            "--dest",
            str(wheels),
            str(next(wheels.glob("*.whl"))),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert download.returncode == 0, download.stdout + download.stderr
    if not host and sys.platform != "linux":
        pytest.skip("requires a Linux Docker CLI host; set ENVWEAVER_TEST_DOCKER_HOST")

    def docker(*args):
        result = subprocess.run(
            ["docker", *connection, *args],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    # Native startup/commit avoids registry access, including build-time metadata lookup.
    builder = docker(
        "run",
        "-d",
        "--pull=never",
        "--network",
        "none",
        "--name",
        "ew-downstream-build-" + uuid.uuid4().hex,
        os.environ["ENVWEAVER_TEST_DOCKER"],
        "sleep",
        "infinity",
    )
    try:
        docker("cp", str(wheels), builder + ":/tmp/wheels")
        docker(
            "exec",
            builder,
            "python",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links=/tmp/wheels",
            "envweaver",
        )
        if host:
            cli_path = _ssh_transport.ssh(host, ["sh", "-c", "command -v docker"], {}).decode().strip()
            _ssh_transport.ssh(
                host,
                [
                    "docker",
                    "--context",
                    "default",
                    "cp",
                    cli_path,
                    builder + ":/usr/local/bin/docker",
                ],
                {},
                timeout=120,
            )
        else:
            docker("cp", shutil.which("docker"), builder + ":/usr/local/bin/docker")
        docker("exec", builder, "rm", "-rf", "/tmp/wheels")
        docker("commit", builder, verifier_image)
        request.addfinalizer(lambda: docker("image", "rm", verifier_image))
    finally:
        docker("rm", "--force", builder)
    producer = tmp_path / "producer"
    producer.mkdir()
    checker = producer / "checker"
    checker.mkdir()
    shutil.copy2(fixtures / "run.py", checker / "run.py")
    cases = [
        {
            "input": "/tmp/csv task/input files/quoted.csv",
            "expected": [["name", "value"], ["a,b", "é"], ["last", ""]],
        },
        {
            "input": "/tmp/csv task/input files/BOM 数据.csv",
            "expected": [["name", "value"], ["多行\n文本", "42"]],
        },
    ]
    (checker / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    inputs = producer / "input files"
    inputs.mkdir()
    (inputs / "quoted.csv").write_text('name,value\n"a,b",é\nlast,\n', encoding="utf-8")
    (inputs / "BOM 数据.csv").write_text('name,value\n"多行\n文本",42\n', encoding="utf-8-sig")
    screenshot = producer / "goal.png"
    screenshot.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="
        )
    )
    program = producer / "parser.py"
    program.write_text(
        "import json,sys\nprint(json.dumps([line.rstrip('\\n').split(',') for line in open(sys.argv[1])]))\n"
    )
    runtime = RecordingRuntime(root=producer / "records", host=host)
    ref = runtime.create(
        backend="docker",
        image=os.environ["ENVWEAVER_TEST_DOCKER"],
        config={
            "command": ["sleep", "infinity"],
            "user": "1000:1000",
            "memory": "256m",
            "cpus": 0.5,
            "shm_size": "64m",
            "network": "none",
            "env": {"TASK_SETTING": "value with spaces"},
            "python": "python3",
            "startup_timeout": 120,
        },
    )
    source_instance = runtime.start(ref=ref)
    saved_tags = []
    try:
        runtime.upload(instance=source_instance, source=program, target="/tmp/csv task/parser.py")
        pool = TaskPool(root=producer / "pool", runtime=runtime)
        task_runtime_ref = runtime.save(instance=source_instance)
        task = pool.save(
            cwd="/", verifier_runtime_ref=runtime.create(
                backend="docker",
                image=verifier_image,
                config={
                    "command": ["sleep", "infinity"],
                    "memory": "256m",
                    "cpus": 0.5,
                    "env": {"CHECKER_MODE": "csv"},
                    "network": "none",
                },
            ),
            task_runtime_ref=task_runtime_ref,
            name="CSV 引号与 BOM",
            goal=[
                {"type": "text", "text": "修复 parser.py，支持引号、换行、BOM 和 Unicode。"},
                {"type": "image", "path": str(screenshot)},
            ],
            verifier=Verifier(
                entry=checker / "run.py",
                timeout=120,
                required_env=["CHECKER_TOKEN"],
                requires_task_instance=True,
                runtime=runtime,
            ),
            context=[{"source": str(inputs), "target": "/tmp/csv task/input files"}],
        )
        saved_tags += [
            json.loads(path.read_text())["tag"]
            for path in (producer / "records").glob("runtimes/*/metadata.json")
        ]
        runtime.close(instance=source_instance)
        source_instance = None
        _check_verifier_object(runtime, docker, Path(task).parent, tmp_path / "production")
        print("production: saved Task, checked incorrect and oracle solutions")
        delivered = tmp_path / "交付 task with spaces"
        shutil.copytree(Path(task).parent, delivered)
        shutil.rmtree(producer)  # No producer files or pool are available to the consumer.
        _check_verifier_object(runtime, docker, delivered, tmp_path / "consumer")
        work = tmp_path / "downstream work"
        work.mkdir()
        runner = work / "consumer.py"
        shutil.copy2(fixtures / "consumer.py", runner)
        # -I -S excludes installed packages and producer PYTHONPATH from the consumer.
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(runner), str(delivered), host or ""],
            cwd=work,
            capture_output=True,
            text=True,
            timeout=900,
        )
        print(result.stdout)
        assert result.returncode == 0, result.stderr
    finally:
        if source_instance is not None:
            runtime.close(instance=source_instance)
        for tag in saved_tags:
            subprocess.run(
                ["docker", *connection, "image", "rm", tag],
                check=True,
                capture_output=True,
                timeout=120,
            )


def _check_verifier_object(runtime, docker, delivered, work):
    """Same Task checked during production and after delivery, without socket mounts."""
    work.mkdir()
    received = work / "pool/received"
    shutil.copytree(delivered, received)
    pool = TaskPool(root=work / "pool", runtime=runtime)
    task = pool.load(task_ref=received / "task.json")
    checker = task.verifier
    assert checker.runtime is runtime
    candidate = runtime.start(ref=task.task_runtime_ref, context=task.context)
    try:
        before = len(runtime.instances)
        assert (
            run_verifier(
                checker,
                task.verifier_runtime_ref,
                task_instance=candidate,
                env={"CHECKER_TOKEN": "test-value"},
            )["score"]
            == 0
        )
        assert (
            runtime.exec(
                candidate,
                "python3 -c 'import importlib.util; assert importlib.util.find_spec(\"envweaver\") is None'",
            )[0]
            == 0
        )
        fixed = work / "parser.py"
        for encoding, score in [("utf-8", 0.5), ("utf-8-sig", 1)]:
            fixed.write_text(
                "import csv,json,sys\nwith open(sys.argv[1],encoding="
                + repr(encoding)
                + ",newline='') as f:\n print(json.dumps(list(csv.reader(f))))\n"
            )
            runtime.upload(
                instance=candidate, source=fixed, target="/tmp/csv task/parser.py"
            )
            assert (
                run_verifier(
                    checker,
                    task.verifier_runtime_ref,
                    task_instance=candidate,
                    env={"CHECKER_TOKEN": "test-value"},
                )["score"]
                == score
            )
        sandboxes = runtime.instances[before:]
        assert len({i.id for i in sandboxes}) == 3
        assert all(i in runtime.closed for i in sandboxes)
        remaining = docker("ps", "-aq", "--no-trunc").splitlines()
        assert all(i.id not in remaining for i in sandboxes)
        print("Verifier: fresh sandboxes for 0/0.5/1; all destroyed after each run")
    finally:
        runtime.close(instance=candidate)


@pytest.mark.skipif(
    not os.environ.get("ENVWEAVER_TEST_PATCH_IMAGE"),
    reason="requires a Docker image with Python 3 and git",
)
def test_patch_verification_without_task_connection(tmp_path, monkeypatch):
    from envweaver.errors import VerifierError

    monkeypatch.delenv("CHECKER_TOKEN", raising=False)
    host = os.environ.get("ENVWEAVER_TEST_DOCKER_HOST")
    connection = ["--host", host] if host else ["--context", "default"]
    runtime = RecordingRuntime(root=tmp_path / "records", host=host)
    ref = runtime.create(
        backend="docker",
        image=os.environ["ENVWEAVER_TEST_PATCH_IMAGE"],
        config={
            "args": ["--entrypoint", "sh"],
            "command": ["-c", "sleep infinity"],
            "network": "none",
            "user": "0:0",
        },
    )
    materials = tmp_path / "materials"
    repo = materials / "repo"
    repo.mkdir(parents=True)
    fixtures = Path(__file__).parents[1] / "fixtures/patch_verifier"
    shutil.copy2(fixtures / "run.py", materials / "run.py")
    (repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "test_app.py").write_text(
        "import unittest\nfrom app import add\nclass TestAdd(unittest.TestCase):\n"
        " def test_add(self): self.assertEqual(add(2, 3), 5)\n"
    )
    checker = Verifier(
        entry=materials / "run.py",
        timeout=60,
        required_env=["CHECKER_TOKEN"],
        runtime=runtime,
        requires_task_instance=False,
    )
    candidate = runtime.start(ref=ref)
    try:
        runtime.upload(instance=candidate, source=repo, target="/tmp/repo")
        assert (
            runtime.exec(instance=candidate, command="git init -q && git add .", cwd="/tmp/repo")[0]
            == 0
        )
        pool = TaskPool(root=tmp_path / "pool", runtime=runtime)
        task_runtime_ref = runtime.save(instance=candidate)
        task_path = pool.save(
            cwd="/", verifier_runtime_ref=ref,
            task_runtime_ref=task_runtime_ref,
            name="patch-evaluation",
            goal=[{"type": "text", "text": "Fix app.py so add returns the sum."}],
            verifier=checker,
        )
        before = len(runtime.instances)
        # Every patch is evaluated in a new sandbox.
        for expression, score in [
            ("a - b + 0", 0),
            ("a + b", 1),
            ("a + b", 1),
            ("a - b + 0", 0),
        ]:
            submitted = tmp_path / "app.py"
            submitted.write_text(f"def add(a, b):\n    return {expression}\n")
            runtime.upload(instance=candidate, source=submitted, target="/tmp/repo/app.py")
            code, patch, stderr = runtime.exec(
                instance=candidate, command="git diff", cwd="/tmp/repo"
            )
            assert code == 0, stderr
            patch_file = tmp_path / "submission.patch"
            patch_file.write_text(patch)
            assert run_verifier(
                checker,
                ref,
                files=[{"source": patch_file, "target": "submission.patch"}],
                env={"CHECKER_TOKEN": "per-run-value"},
            ) == {"score": score}
            assert runtime.instances[-1] in runtime.closed
        patch_file.write_text("invalid patch")
        with pytest.raises(VerifierError):
            run_verifier(
                checker,
                ref,
                files=[{"source": patch_file, "target": "submission.patch"}],
                env={"CHECKER_TOKEN": "per-run-value"},
            )
        with pytest.raises(VerifierError):
            run_verifier(checker, ref, env={"CHECKER_TOKEN": "per-run-value"})
        sandboxes = runtime.instances[before:]
        assert len({i.id for i in sandboxes}) == 6
        assert all(i in runtime.closed for i in sandboxes)
        print(
            "Patch Verifier: scores 0/1/1/0, invalid/missing patch errors; six sandboxes destroyed"
        )
        delivered = tmp_path / "delivered"
        shutil.copytree(Path(task_path).parent, delivered)
        shutil.rmtree(materials)
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(fixtures / "consumer.py"), str(delivered), host or ""],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        print(result.stdout)
    finally:
        runtime.close(instance=candidate)
        for record in (tmp_path / "records").glob("runtimes/*/metadata.json"):
            tag = json.loads(record.read_text())["tag"]
            subprocess.run(
                ["docker", *connection, "image", "rm", tag], check=True, capture_output=True
            )
