import json
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

import pytest

from envweaver import Instance, Runtime, RuntimeRef, Task, eval
from envweaver._io import ssh_transport as _ssh_transport
from envweaver._io.output import OutputDirectory
from envweaver.agents.execution import AgentExecution
from envweaver.errors import RuntimeError as TransportError
from tests.agents.helpers import CooperativeAgent


def address(path):
    return "ssh://files" + quote(str(path), safe="/")


@pytest.fixture
def ssh_files(monkeypatch):
    calls = []
    monkeypatch.setattr(Runtime, "exec", lambda *args, **kwargs: (0, "", ""))

    def ssh(host, command, payload, *, ssh_options=None):
        calls.append((host, payload, ssh_options))
        result = subprocess.run(
            [sys.executable, *command[1:]],
            input=json.dumps(payload).encode(),
            capture_output=True,
        )
        if result.returncode:
            raise TransportError(result.stderr.decode())
        return result.stdout

    monkeypatch.setattr(_ssh_transport, "ssh", ssh)
    monkeypatch.setattr(
        _ssh_transport,
        "command_args",
        lambda host, command, **kwargs: [sys.executable, *command[1:]],
    )
    return calls


class RecordingAgent(CooperativeAgent):
    def run(self, goal, *, runtime, instance, output, cwd: str):
        output.record("message", {"text": "done"})
        return output.finish([{"type": "text", "text": "done"}])


def test_remote_goal_and_live_output(tmp_path, ssh_files):
    image = tmp_path / "input 中文 #.png"
    image.write_bytes(b"image")
    destination = tmp_path / "output 中文 #"
    runtime = Runtime(ssh={"config_file": str(tmp_path / "ssh_config")})
    instance = Instance("test", "test")

    class InspectAgent(RecordingAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            assert Path(goal[0]["path"]).read_bytes() == b"image"
            output.record("checkpoint", {"text": "partial"})
            snapshot = json.loads((destination / "result.json").read_text())
            attachment = snapshot["artifacts"]["files"][0]
            assert (destination / attachment).read_bytes() == b"image"
            assert len((destination / "logs/events.jsonl").read_text().splitlines()) == 2
            return output.finish([{"type": "image", "path": attachment}])

    agent = InspectAgent()
    agent_execution = AgentExecution(agent)
    kwargs = dict(runtime=runtime, instance=instance, output_dir=address(destination))
    agent_execution.prepare(**kwargs, cwd=str(tmp_path))
    assert (destination / "logs/prepare.log").exists()
    goal = [{"type": "image", "path": address(image)}]
    result = agent_execution.run(goal=goal, **kwargs)
    assert goal[0]["path"] == address(image)
    assert json.loads((destination / "result.json").read_text()) == result
    assert (destination / "result.json").stat().st_mode & 0o777 == 0o600
    assert all(options is None for _, _, options in ssh_files)
    attachment_writes = [
        payload
        for _, payload, _ in ssh_files
        if payload["action"] == "publish" and "/files/" in payload["root"]
    ]
    assert len(attachment_writes) == 1


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_or_stopped_agent_keeps_remote_artifacts(tmp_path, ssh_files, cancel):
    entered = threading.Event()

    class FailingAgent(RecordingAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            output.record("checkpoint", {"text": "partial"})
            entered.set()
            if cancel:
                self._cancelled.wait(5)
                self._check_stopped()
            raise RuntimeError("provider failed")

    output = tmp_path / "failed"
    agent = FailingAgent()
    agent_execution = AgentExecution(agent)
    kwargs = dict(runtime=Runtime(), instance=Instance("test", "test"), output_dir=address(output))
    agent_execution.prepare(**kwargs, cwd=str(tmp_path))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            agent_execution.run, goal=[{"type": "text", "text": "solve"}], **kwargs
        )
        assert entered.wait(5)
        if cancel:
            agent_execution.stop(timeout=5)
        with pytest.raises(InterruptedError if cancel else RuntimeError):
            future.result()
    snapshot = json.loads((output / "result.json").read_text())
    assert "duration_seconds" in snapshot["metadata"]
    assert len((output / "logs/events.jsonl").read_text().splitlines()) == 2


def test_remote_collision_and_protocol_validation(tmp_path, ssh_files, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="scheme"):
        OutputDirectory("https://files/output")
    assert not (tmp_path / "https:").exists()
    output = tmp_path / "existing"
    output.mkdir()
    (output / "result.json").write_text("keep")
    agent = RecordingAgent()
    agent_execution = AgentExecution(agent)
    with pytest.raises(FileExistsError):
        agent_execution.prepare(
            runtime=Runtime(),
            instance=Instance("test", "test"),
            output_dir=address(output),
            cwd=str(tmp_path),
        )
    assert (output / "result.json").read_text() == "keep"


def test_failed_publish_retains_recoverable_local_output(tmp_path, ssh_files, monkeypatch):
    output = OutputDirectory(address(tmp_path / "remote"))
    (output.path / "result.json").write_text('{"score": 1}')
    original = _ssh_transport.ssh

    def fail(host, command, payload, **kwargs):
        if payload["action"] == "publish":
            raise TransportError("connection lost")
        return original(host, command, payload, **kwargs)

    monkeypatch.setattr(_ssh_transport, "ssh", fail)
    try:
        with pytest.raises(TransportError) as caught:
            output.sync()
        assert str(output.path) in " ".join(caught.value.__notes__)
        assert (output.path / "result.json").read_text() == '{"score": 1}'
        assert not (tmp_path / "remote/result.json").exists()
    finally:
        shutil.rmtree(output.path)


def test_eval_remote_batch_and_failure_snapshot(tmp_path, ssh_files):
    from tests.evaluation.test_eval import FakeRuntime, FakeVerifier

    runtime = FakeRuntime()
    runtime.ssh = {"config_file": str(tmp_path / "ssh_config")}
    verifier = FakeVerifier(runtime)
    verifier.required_env = []
    ref = RuntimeRef("fake", "image")
    task = Task(
        cwd="/",
        name="task",
        task_ref="task.json",
        goal=[{"type": "text", "text": "solve"}],
        metadata={},
        context=[],
        task_runtime_ref=ref,
        verifier_runtime_ref=ref,
        verifier=verifier,
    )

    class FailingAgent(RecordingAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            output.record("message", {"text": "partial"})
            if goal[0]["text"] == "fail":
                raise RuntimeError("provider failed")
            return super().run(
                goal=goal, runtime=runtime, instance=instance, output=output, cwd=cwd
            )

    other_runtime = FakeRuntime()
    other_runtime.ssh = {"identity_file": str(tmp_path / "other_key")}
    other_verifier = FakeVerifier(other_runtime)
    other_verifier.required_env = []
    other_task = replace(task, verifier=other_verifier, goal=[{"type": "text", "text": "fail"}])
    output = tmp_path / "batch"
    results = eval(
        tasks=[task, other_task], agent=FailingAgent(), output_dir=address(output), concurrency=2
    )
    assert results == json.loads((output / "results.json").read_text())
    assert results[0]["errors"] == []
    assert results[1]["errors"][-1]["stage"] == "agent"
    assert "logs/events.jsonl" in results[1]["agent_result"]["artifacts"]["logs"]
    for index, result in enumerate(results, 1):
        directory = output / f"{index:06d}"
        assert json.loads((directory / "result.json").read_text()) == result
        assert (directory / "agent/logs/events.jsonl").is_file()
    assert not runtime.active
    assert not other_runtime.active
    assert all(call[2] is None for call in ssh_files)
    starts = len(runtime.starts)
    with pytest.raises(FileExistsError):
        eval(tasks=task, agent=RecordingAgent(), output_dir=address(output))
    assert len(runtime.starts) == starts


def test_empty_remote_eval(tmp_path, ssh_files):
    output = tmp_path / "empty"
    assert eval(tasks=[], agent=RecordingAgent(), output_dir=address(output)) == []
    assert json.loads((output / "results.json").read_text()) == []


def test_slow_ssh_does_not_block_stop_deadline(tmp_path, ssh_files, monkeypatch):
    agent = RecordingAgent()
    agent_execution = AgentExecution(agent)
    kwargs = dict(
        runtime=Runtime(), instance=Instance("test", "test"), output_dir=address(tmp_path / "slow")
    )
    agent_execution.prepare(**kwargs, cwd=str(tmp_path))
    entered, release = threading.Event(), threading.Event()
    original = _ssh_transport.ssh

    def slow(host, command, payload, **options):
        if payload["action"] == "publish":
            entered.set()
            assert release.wait(5)
        return original(host, command, payload, **options)

    monkeypatch.setattr(_ssh_transport, "ssh", slow)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            agent_execution.run, goal=[{"type": "text", "text": "solve"}], **kwargs
        )
        try:
            assert entered.wait(5)
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                agent_execution.stop(timeout=0.05)
            assert time.monotonic() - started < 1
        finally:
            release.set()
        with pytest.raises(InterruptedError):
            future.result(timeout=5)


def test_remote_prepare_failure_syncs_directly_written_files(tmp_path, ssh_files):
    class FailedPrepare(RecordingAgent):
        def prepare(self, *, runtime, instance, output, cwd: str):
            (output.directory / "diagnostic.txt").write_text("installation failed")
            raise RuntimeError("installation failed")

    output = tmp_path / "prepare-failed"
    with pytest.raises(RuntimeError, match="installation failed"):
        AgentExecution(FailedPrepare()).prepare(
            runtime=Runtime(),
            instance=Instance("test", "test"),
            output_dir=address(output),
            cwd=str(tmp_path),
        )
    assert (output / "diagnostic.txt").read_text() == "installation failed"


@pytest.mark.parametrize("failure", ["interrupt", "upload"])
def test_remote_eval_preserves_interrupt_and_upload_failure(
    tmp_path, ssh_files, monkeypatch, failure
):
    from tests.evaluation.test_eval import FakeRuntime, FakeVerifier

    runtime = FakeRuntime()
    verifier = FakeVerifier(runtime)
    verifier.required_env = []
    ref = RuntimeRef("fake", "image")
    task = Task(
        cwd="/",
        name="task",
        task_ref="task.json",
        goal=[{"type": "text", "text": "solve"}],
        metadata={},
        context=[],
        task_runtime_ref=ref,
        verifier_runtime_ref=ref,
        verifier=verifier,
    )

    executions = []

    class TrackedAgent(RecordingAgent):
        def prepare(self, *, runtime, instance, output, cwd: str):
            self.directory = output.directory

        def new(self):
            execution = super().new()
            executions.append(execution)
            return execution

    class InterruptAgent(TrackedAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            raise KeyboardInterrupt()

    agent = InterruptAgent() if failure == "interrupt" else TrackedAgent()
    original = _ssh_transport.ssh

    def fail(host, command, payload, **options):
        if (
            failure == "upload"
            and payload["action"] == "publish"
            and payload["root"].endswith("logs/events.jsonl")
        ):
            raise TransportError("connection lost")
        return original(host, command, payload, **options)

    monkeypatch.setattr(_ssh_transport, "ssh", fail)
    output = tmp_path / failure
    try:
        if failure == "interrupt":
            with pytest.raises(KeyboardInterrupt):
                eval(tasks=task, agent=agent, output_dir=address(output))
        else:
            (result,) = eval(tasks=task, agent=agent, output_dir=address(output))
            assert str(executions[0].directory) in result["errors"][-1]["message"]
            assert "connection lost" in result["errors"][-1]["message"]
        (result,) = json.loads((output / "results.json").read_text())
        assert result["verification_result"] is None
        assert result["errors"][-1]["stage"] == "agent"
        assert not runtime.active
    finally:
        for execution in executions:
            if failure == "upload" and execution.directory is not None:
                shutil.rmtree(execution.directory)


def test_atif_children_are_published_before_main_trajectory(tmp_path, ssh_files, monkeypatch):
    import envweaver._io.output as publication
    from envweaver import AgentOutput
    from envweaver.agents.trajectory import add_step, document

    destination = tmp_path / "remote-atif"
    output = AgentOutput(
        address(destination), {"agent": "test", "agent_version": "1", "model": "test"}
    )
    child = document(output.result["metadata"], "child")
    add_step(child, "user", "child input")
    child_name = "trajectory.summarization-1.json"
    root = document(output.result["metadata"], "root")
    add_step(
        root,
        "system",
        "",
        observation={"results": [{"subagent_trajectory_ref": [{"trajectory_path": child_name}]}]},
    )
    dispatch = publication._dispatch
    observed = []

    def inspect_publish(target, payload):
        result = dispatch(target, payload)
        if payload["action"] == "publish" and str(target).endswith("/trajectory.json"):
            observed.append(True)
            assert (destination / child_name).is_file()
        return result

    monkeypatch.setattr(publication, "_dispatch", inspect_publish)
    output.save_trajectory(root, related={child_name: child})
    assert observed
