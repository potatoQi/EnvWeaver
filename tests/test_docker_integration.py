import json
import os
import uuid

import pytest

from envweaver import Runtime, RuntimeRef, TaskPool, Verifier, _ssh_transport
from envweaver.runtime.docker import DockerBackend

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ENVWEAVER_TEST_DOCKER"),
        reason="set ENVWEAVER_TEST_DOCKER to a prepared image",
    ),
]


def saved_records(root):
    return [
        {
            "metadata": json.loads(p.read_text()),
            "runtime": json.loads(p.with_name("runtime.json").read_text()),
        }
        for p in root.glob("runtimes/*/metadata.json")
    ]


@pytest.mark.parametrize("storage", ["local", "ssh"])
def test_real_docker_task_lifecycle(tmp_path, monkeypatch, run_native_verifier, request, storage):
    host = os.environ.get("ENVWEAVER_TEST_DOCKER_HOST")
    runtime = Runtime(tmp_path / "local-records", host=host)
    if host:
        monkeypatch.setenv("DOCKER_HOST", "ssh://wrong-envweaver-host")
        monkeypatch.setenv("DOCKER_CONTEXT", "wrong-envweaver-context")
    docker = DockerBackend(runtime.root)
    pool_root = tmp_path / "pool"
    if storage == "ssh":
        file_host = os.environ.get("ENVWEAVER_TEST_FILE_HOST")
        if not file_host:
            pytest.skip("set ENVWEAVER_TEST_FILE_HOST")
        directory = "/tmp/envweaver-task-test-" + uuid.uuid4().hex
        pool_root = file_host + directory
        request.addfinalizer(
            lambda: _ssh_transport.ssh(
                file_host,
                [
                    "python3",
                    "-c",
                    "import json,sys,shutil; shutil.rmtree(json.load(sys.stdin)['path'], ignore_errors=True)",
                ],
                {"path": directory},
            )
        )
    pool = TaskPool(root=pool_root, runtime=runtime)
    ref = runtime.create(
        backend="docker",
        image=os.environ["ENVWEAVER_TEST_DOCKER"],
        config={"command": ["sleep", "infinity"]},
    )
    assert isinstance(ref, RuntimeRef)
    assert ref.image.startswith("sha256:")
    original = runtime.start(ref)
    instances = [original]
    try:
        source = tmp_path / "state.txt"
        source.write_text("broken")
        runtime.upload(original, source, "/work/state.txt")
        assert runtime.exec(original, "cat state.txt", "/work") == (0, "broken", "")
        with pytest.raises(TimeoutError):
            runtime.exec(original, "sleep 30", timeout=0.1)
        package = tmp_path / "checker"
        package.mkdir()
        entry = package / "run.py"
        entry.write_text("""import argparse, json
from envweaver import Runtime
p = argparse.ArgumentParser()
p.add_argument('--instance')
runtime = Runtime()
i = runtime.connect(path=p.parse_args().instance)
code, out, err = runtime.exec(i, 'cat /work/state.txt')
if code: raise RuntimeError(err)
print(json.dumps({'score': float(out.strip() == 'fixed')}))
""")
        assert run_native_verifier(entry, original)["score"] == 0
        context = tmp_path / "inputs"
        context.mkdir()
        (context / "data.csv").write_text("original input")
        (context / "setup.py").write_text(
            "from pathlib import Path\n"
            "assert Path('/inputs/data.csv').read_text() == 'original input'\n"
            "Path('/work/ready.txt').write_text('ready')\n"
        )
        task_ref = pool.save(
            original,
            "bug",
            [{"type": "text", "text": "fix state.txt"}],
            Verifier(entry=entry, timeout=60, runtime=runtime, requires_task_instance=True),
            ref,
            [{"source": str(context), "target": "/inputs"}],
            setup={"command": ["python3", "/inputs/setup.py"], "timeout": 10},
            metadata={"kind": "repair"},
        )
        assert runtime.exec(original, "cat /work/state.txt")[1] == "broken"
        assert runtime.close(original) is None
        instances.remove(original)
        source.unlink()
        entry.unlink()
        assert pool.search(filters={"kind": "repair"}) == [task_ref]
        task = pool.load(task_ref)
        assert "host" not in task.task_runtime_ref.to_dict()
        # A fresh manager explicitly selects the server holding the saved image.
        runtime = Runtime(root=runtime.root, host=host)
        first = runtime.start(task.task_runtime_ref, task.context, setup=task.setup)
        instances.append(first)
        assert runtime.exec(first, "cat /work/ready.txt")[1] == "ready"
        assert (
            run_native_verifier(task.verifier.entry, first, timeout=task.verifier.timeout)["score"]
            == 0
        )
        assert runtime.exec(first, "printf fixed > /work/state.txt")[0] == 0
        assert (
            run_native_verifier(task.verifier.entry, first, timeout=task.verifier.timeout)["score"]
            == 1
        )
        runtime.download(first, "/inputs", tmp_path / "download")
        assert (tmp_path / "download/data.csv").read_text() == "original input"
        # Re-copy onto the same target must not add an extra nested directory.
        runtime.upload(first, context, "/inputs")
        assert runtime.exec(first, "cat /inputs/data.csv")[1] == "original input"
        second = runtime.start(task.task_runtime_ref, task.context, setup=task.setup)
        instances.append(second)
        assert runtime.exec(second, "cat /work/ready.txt")[1] == "ready"
        assert (
            run_native_verifier(task.verifier.entry, second, timeout=task.verifier.timeout)["score"]
            == 0
        )
        assert task.task_runtime_ref.image.startswith("sha256:")
        assert "archive" not in task.task_runtime_ref.config
        assert not list(runtime.root.rglob("*.tar"))
        if host:
            assert _ssh_transport.ssh(host, ["test", "!", "-e", str(runtime.root)], {}) == b""
        records = saved_records(runtime.root)
        assert "host" not in records[0]["runtime"]
        assert original.root == str(runtime.root)

        def layers(image):
            return json.loads(docker._docker("image", "inspect", image, host=host))[0]["RootFS"][
                "Layers"
            ]

        base_layers = layers(ref.image)
        assert layers(task.task_runtime_ref.image)[: len(base_layers)] == base_layers
    finally:
        for instance in instances:
            assert runtime.close(instance) is None
        # Explicitly remove only assets created by this test; close itself keeps them.
        for record in saved_records(runtime.root):
            saved = record["runtime"]
            try:
                docker._docker("image", "inspect", saved["image"], host=host)
            finally:
                docker._docker("image", "rm", record["metadata"]["tag"], host=host)


def test_uploaded_files_are_writable_by_runtime_user(tmp_path):
    runtime = Runtime(tmp_path / "local-records", host=os.environ.get("ENVWEAVER_TEST_DOCKER_HOST"))
    instance = runtime.start(
        RuntimeRef(
            "docker",
            os.environ["ENVWEAVER_TEST_DOCKER"],
            {
                "command": ["sleep", "infinity"],
                "user": "1000:1000",
            },
        )
    )
    try:
        source = tmp_path / "input.txt"
        source.write_text("input")
        runtime.upload(instance, source, "/tmp/input.txt")
        assert runtime.exec(instance, "printf edited > /tmp/input.txt")[0] == 0
        assert runtime.exec(instance, "cat /tmp/input.txt")[1] == "edited"
    finally:
        assert runtime.close(instance) is None


def test_connect_external_container_rejects_save(tmp_path):
    host = os.environ.get("ENVWEAVER_TEST_DOCKER_HOST")
    docker = DockerBackend(host=host)
    cid = docker._docker(
        "run",
        "-d",
        "--memory",
        "256m",
        "--cpus",
        "0.5",
        os.environ["ENVWEAVER_TEST_DOCKER"],
        "sleep",
        "infinity",
        host=host,
    )
    runtime = Runtime(root=tmp_path / "records")
    try:
        descriptor = tmp_path / "instance.json"
        descriptor.write_text(
            json.dumps({"version": 1, "backend": "docker", "host": host, "container_id": cid})
        )
        instance = runtime.connect(path=descriptor)
        assert runtime.exec(instance, "printf retained > /tmp/state")[0] == 0
        with pytest.raises(ValueError, match="returned by connect"):
            runtime.save(instance)
        assert runtime.exec(instance, "cat /tmp/state")[1] == "retained"
        info = json.loads(docker._docker("inspect", instance.id, host=host))[0]
        assert info["HostConfig"]["Memory"] == 256 * 1024 * 1024
        assert info["HostConfig"]["NanoCpus"] == 500_000_000
        assert (
            runtime.exec(
                instance,
                "python3 -c 'import importlib.util; assert importlib.util.find_spec(\"envweaver\") is None'",
            )[0]
            == 0
        )
    finally:
        if cid:
            docker._docker("rm", "-f", cid, host=host)
