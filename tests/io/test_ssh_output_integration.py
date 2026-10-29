"""Real OpenSSH tests for Agent/Eval outputs and TaskPool publication."""

import io
import json
import os
import shutil
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from PIL import Image

from envweaver import EvalConfig, Runtime, RuntimeRef, Task, TaskPool, Verifier, eval
from envweaver._io import ssh_transport as _ssh_transport
from envweaver._io.output import OutputDirectory
from envweaver._io.output_lock import output_lock
from envweaver._io.paths import join_address
from envweaver._io.storage import local_source, local_target
from envweaver.agents.execution import AgentExecution
from envweaver.errors import RuntimeError as TransportError
from envweaver.errors import TaskExistsError
from tests.agents.helpers import CooperativeAgent

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ENVWEAVER_TEST_FILE_HOST"),
        reason="requires an SSH file server",
    ),
]


def read_json(address):
    with local_source(address) as source:
        return json.loads(source.read_text())


def remote(address, source):
    parsed = urlsplit(address)
    host = f"{parsed.scheme}://{parsed.netloc}"
    return _ssh_transport.ssh(
        host, ["python3", "-c", source], unquote(parsed.path),
    )


@pytest.fixture
def files():
    address = os.environ["ENVWEAVER_TEST_FILE_HOST"].rstrip("/") + "/tmp/ew-output-" + uuid.uuid4().hex
    remote(address, "import json,sys; from pathlib import Path; Path(json.load(sys.stdin)).mkdir()")
    try:
        yield address
    finally:
        remote(address, "import json,sys,shutil; shutil.rmtree(json.load(sys.stdin))")


@pytest.fixture
def sandbox(tmp_path):
    runtime = Runtime(root=tmp_path / "records")
    if image := os.environ.get("ENVWEAVER_TEST_DOCKER"):
        ref = RuntimeRef("docker", image, {"command": ["sleep", "infinity"]})
    elif filename := os.environ.get("ENVWEAVER_TEST_QEMU_REF"):
        ref = RuntimeRef.from_dict(json.loads(Path(filename).read_text()))
    else:
        pytest.skip("requires a Docker image or a prepared QEMU reference")
    instance = runtime.start(ref=ref)
    try:
        yield runtime, ref, instance
    finally:
        runtime.close(instance=instance)
        for record in (tmp_path / "records").glob("runtimes/*/metadata.json"):
            tag = json.loads(record.read_text()).get("tag")
            if tag:
                subprocess.run(["docker", "--context", "default", "image", "rm", tag], check=True, capture_output=True)


def test_agent_live_ssh_image_and_outputs(files, sandbox):
    runtime, _, instance = sandbox
    image = io.BytesIO()
    Image.new("RGB", (2, 3), (12, 34, 56)).save(image, format="PNG")
    image_bytes = image.getvalue()
    image_address = join_address(files, "图片 #.png")
    with local_target(image_address) as source:
        source.write_bytes(image_bytes)
    output = join_address(files, "产物 #")

    class LiveAgent(CooperativeAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            assert Path(goal[0]["path"]).read_bytes() == image_bytes
            output.record("checkpoint", {"text": "实时保存"})
            snapshot = read_json(join_address(output, "result.json"))
            attachment = snapshot["artifacts"]["files"][0]
            with local_source(join_address(output, attachment)) as source:
                assert source.read_bytes() == image_bytes
            return output.finish([{"type": "image", "path": attachment}])

    agent = LiveAgent()
    agent_execution = AgentExecution(agent)
    kwargs = dict(runtime=runtime, instance=instance, output_dir=output)
    try:
        agent_execution.prepare(**kwargs, cwd="/")
        goal = [{"type": "image", "path": image_address}]
        result = agent_execution.run(goal=goal, **kwargs)
        assert read_json(join_address(output, "result.json")) == result
        assert goal[0]["path"] == image_address
        mode = remote(join_address(output, "result.json"), "import json,sys,os; print(os.stat(json.load(sys.stdin)).st_mode & 0o777)")
        assert int(mode) == 0o600
        with pytest.raises(FileExistsError):
            AgentExecution(LiveAgent()).prepare(**kwargs, cwd="/")
    finally:
        agent_execution.stop(timeout=10)


@pytest.mark.parametrize("cancel", [False, True])
def test_agent_ssh_failure_and_stop_keep_snapshot(files, sandbox, cancel):
    runtime, _, instance = sandbox
    entered = threading.Event()

    class FailingAgent(CooperativeAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            output.record("checkpoint", {"partial": True})
            entered.set()
            if cancel:
                self._cancelled.wait(15)
                self._check_stopped()
            raise OSError("injected failure")

    agent = FailingAgent()
    agent_execution = AgentExecution(agent)
    output = join_address(files, "interrupted")
    kwargs = dict(runtime=runtime, instance=instance, output_dir=output)
    try:
        agent_execution.prepare(**kwargs, cwd="/")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(agent_execution.run, goal=[{"type": "text", "text": "test"}], **kwargs)
            assert entered.wait(10)
            if cancel:
                agent_execution.stop(timeout=10)
            with pytest.raises(InterruptedError if cancel else OSError):
                future.result(timeout=15)
        result = read_json(join_address(output, "result.json"))
        assert result["artifacts"]["logs"] is not None
        assert "duration_seconds" in result["metadata"]
    finally:
        agent_execution.stop(timeout=10)


def test_ssh_publication_failure_retains_local_cache(files):
    output = OutputDirectory(join_address(files, "denied"))
    (output.path / "result.json").write_text('{"partial":true}')
    try:
        remote(output.address, "import json,sys,os; os.chmod(json.load(sys.stdin),0o500)")
        with pytest.raises(TransportError) as caught:
            output.sync()
        assert str(output.path) in " ".join(caught.value.__notes__)
        assert (output.path / "result.json").read_text() == '{"partial":true}'
    finally:
        remote(output.address, "import json,sys,os; os.chmod(json.load(sys.stdin),0o700)")
        shutil.rmtree(output.path)


def test_ssh_output_lock_excludes_other_writers_and_releases(files):
    address = join_address(files, ".eval.lock")
    with output_lock(address):
        with pytest.raises(FileExistsError, match="Another eval"):
            with output_lock(address):
                pytest.fail("Concurrent writer acquired the lock")
    with output_lock(address):
        pass


def test_eval_ssh_batch_and_collision(files, sandbox, tmp_path):
    runtime, ref, _ = sandbox
    entry = tmp_path / "verify/run.py"
    entry.parent.mkdir()
    entry.write_text('print(\'{"score":1,"details":{"ok":true}}\')')
    verifier = Verifier(entry=entry, timeout=10, runtime=runtime, requires_task_instance=False)
    tasks = [Task(cwd="/", name=str(n), task_ref="test", metadata={}, context=[], goal=[{"type": "text", "text": "test"}], task_runtime_ref=ref, verifier_runtime_ref=ref, verifier=verifier) for n in range(4)]

    class TestAgent(CooperativeAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            output.record("message", {"text": "done"})
            return output.finish([])

    output = join_address(files, "batch")
    results = eval(tasks=tasks, agent=TestAgent(), config=EvalConfig(), output_dir=output, concurrency=2)
    assert [item["name"] for item in results] == ["0", "1", "2", "3"]
    assert all(item["errors"] == [] for item in results), results
    assert read_json(join_address(output, "results.json")) == results
    for n, result in enumerate(results, 1):
        assert read_json(join_address(output, f"{n:06d}/result.json")) == result
    with pytest.raises(FileExistsError):
        eval(tasks=[], agent=TestAgent(), output_dir=output)


def test_ssh_pool_delivery_and_concurrent_name(files, sandbox, tmp_path):
    runtime, ref, instance = sandbox
    assert_pool_delivery(files, runtime, ref, instance, tmp_path)


def test_ssh_pool_publication(files, runtime, tmp_path):
    """Real SSH storage, with snapshot creation isolated by a filesystem backend."""
    source = tmp_path / "sandbox"
    source.mkdir()
    ref = runtime.create(backend="test", image=str(source))
    instance = runtime.start(ref=ref)
    try:
        assert_pool_delivery(files, runtime, ref, instance, tmp_path)
    finally:
        runtime.close(instance=instance)


def assert_pool_delivery(files, runtime, ref, instance, tmp_path):
    if ref.backend == "qemu":
        # Deployment SSH credentials are not portable Task fields.
        ssh = ref.connection.get("ssh", {})
        ref = replace(ref, connection={"ssh": {
            key: value for key, value in ssh.items() if key in {"user", "python"}
        }})
    pool = TaskPool(root=join_address(files, "pool"), runtime=runtime)
    entry = tmp_path / "checker/run.py"
    entry.parent.mkdir()
    entry.write_text('print(\'{"score":1}\')')
    args = dict(cwd="/", instance=instance, name="任务 #", goal=[{"type": "text", "text": "test"}], metadata={"tag": "acceptance"}, verifier=Verifier(entry=entry, timeout=10, runtime=runtime), verifier_runtime_ref=ref)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(pool.save, **args) for _ in range(2)]
        saved, failures = [], []
        for future in futures:
            try:
                saved.append(future.result())
            except TaskExistsError as error:
                failures.append(error)
    assert len(saved) == len(failures) == 1
    assert pool.search(filters={"tag": "acceptance"}) == saved
    assert pool.search(filters={"missing": None}) == []
    entry.unlink()
    task = pool.load(task_ref=saved[0])
    assert Path(task.verifier.entry).is_file()
    assert task.metadata == {"tag": "acceptance"}
