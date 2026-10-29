import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from envweaver import Agent, Instance, Runtime
from envweaver.agents.execution import AgentExecution
from tests.agents.helpers import CooperativeAgent


class RecordingAgent(CooperativeAgent):
    def run(self, goal, *, runtime, instance, output, cwd: str):
        output.record("message", {"role": "assistant", "text": "done"})
        output.update(metadata={"usage": {"tokens": 12}})
        return output.finish([{"type": "text", "text": goal[0]["text"]}])


def test_public_adapter_needs_no_private_hooks_or_execution_state(tmp_path):
    received = []

    class CustomAgent(Agent):
        def prepare(self, *, runtime, instance, output, cwd: str):
            received.append((runtime, instance, output, cwd))

        def run(self, *, goal, runtime, instance, output, cwd: str):
            assert (runtime, instance, output, cwd) == received[0]
            assert not hasattr(self, "_runtime")
            return {
                "output": [{"type": "text", "text": self.config["prefix"] + goal[0]["text"]}],
                "artifacts": output.result["artifacts"],
                "metadata": self.metadata,
            }

    adapter = CustomAgent(prefix="answer: ")
    execution, kwargs = prepared(tmp_path, adapter)
    result = execution.run(goal=[{"type": "text", "text": "hello"}], **kwargs)
    assert received[0][3] == str(tmp_path)
    assert result["output"] == [{"type": "text", "text": "answer: hello"}]
    assert json.loads((tmp_path / "agent/result.json").read_text()) == result
    assert not hasattr(adapter, "_prepare") and not hasattr(adapter, "_run")


def prepared(tmp_path, agent=None):
    agent = agent or RecordingAgent(name="recording", model="test-model")
    runtime, instance = Mock(spec=Runtime), Instance("test", "test")
    runtime.exec.return_value = (0, "", "")
    directory = tmp_path / "agent"
    execution = AgentExecution(agent)
    execution.prepare(runtime=runtime, instance=instance, output_dir=directory, cwd=str(tmp_path))
    return execution, dict(runtime=runtime, instance=instance, output_dir=directory)


def test_prepare_requires_cwd_before_creating_artifacts(tmp_path):
    runtime = Mock(spec=Runtime)
    execution = AgentExecution(RecordingAgent())
    with pytest.raises(TypeError, match="cwd"):
        execution.prepare(runtime=runtime, instance=Instance("test", "test"),
                          output_dir=tmp_path / "agent")
    runtime.exec.assert_not_called()
    assert not (tmp_path / "agent").exists()


@pytest.mark.parametrize("cwd", [None, "", "relative/path", 123, "/bad\0path"])
def test_prepare_rejects_invalid_cwd_before_creating_artifacts(tmp_path, cwd):
    runtime = Mock(spec=Runtime)
    execution = AgentExecution(RecordingAgent())
    with pytest.raises(ValueError, match="absolute sandbox path"):
        execution.prepare(runtime=runtime, instance=Instance("test", "test"),
                          output_dir=tmp_path / "agent", cwd=cwd)
    runtime.exec.assert_not_called()
    assert not (tmp_path / "agent").exists()


def test_result_and_events_survive_roundtrip(tmp_path):
    agent, kwargs = prepared(tmp_path)
    result = agent.run(goal=[{"type": "text", "text": "hello"}], **kwargs)
    assert result == json.loads((kwargs["output_dir"] / "result.json").read_text())
    assert result["metadata"]["usage"]["tokens"] == 12
    events = [
        json.loads(line)
        for line in (kwargs["output_dir"] / "logs/events.jsonl").read_text().splitlines()
    ]
    assert [event["seq"] for event in events] == [1, 2]
    assert events[0]["type"] == "input"
    assert result["output"] == [{"type": "text", "text": "hello"}]
    agent.stop()
    agent.stop()
    with pytest.raises(ValueError, match="unused"):
        agent.run(goal=[{"type": "text", "text": "again"}], **kwargs)


def test_cancel_waits_for_run_and_final_snapshot(tmp_path):
    entered = threading.Event()

    class WaitingAgent(RecordingAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            output.record("message", {"text": "partial"})
            entered.set()
            self._cancelled.wait(5)
            output.update(metadata={"partial": True})
            self._check_stopped()

    agent, kwargs = prepared(tmp_path, WaitingAgent())
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(agent.run, goal=[{"type": "text", "text": "test"}], **kwargs)
        assert entered.wait(2)
        agent.stop(timeout=1)
        with pytest.raises(InterruptedError):
            future.result()
    result = json.loads((kwargs["output_dir"] / "result.json").read_text())
    assert result["metadata"]["partial"] is True


def test_failure_keeps_partial_data(tmp_path):
    class FailingAgent(RecordingAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            output.record("message", {"text": "before failure"})
            raise RuntimeError("provider unavailable")

    agent, kwargs = prepared(tmp_path, FailingAgent())
    with pytest.raises(RuntimeError, match="provider unavailable"):
        agent.run(goal=[{"type": "text", "text": "test"}], **kwargs)
    agent.stop()
    assert json.loads((kwargs["output_dir"] / "result.json").read_text())["artifacts"]["logs"]


def test_uncooperative_stop_is_bounded(tmp_path):
    release = threading.Event()

    class StuckAgent(RecordingAgent):
        def stop(self, timeout=30):
            self._cancelled.set()
            release.wait(5)

    agent, _ = prepared(tmp_path, StuckAgent())
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            agent.stop(timeout=0.03)
        assert time.monotonic() - started < 1
    finally:
        release.set()


def test_snapshot_updates_are_atomic_and_reject_nonfinite_values(tmp_path):
    agent, kwargs = prepared(tmp_path)
    with pytest.raises(ValueError):
        agent.output.update(metadata={"cost": float("nan")})
    assert "cost" not in json.loads((kwargs["output_dir"] / "result.json").read_text())["metadata"]
    with pytest.raises(ValueError, match="escapes"):
        agent.output.update(artifacts={"files": ["../outside"]})


def test_prepare_failure_has_log_and_snapshot(tmp_path):
    class BrokenPrepare(RecordingAgent):
        def prepare(self, *, runtime, instance, output, cwd: str):
            output.write_log("prepare.log", "installation failed")
            raise OSError("installation failed")

    with pytest.raises(OSError):
        prepared(tmp_path, BrokenPrepare())
    assert (
        json.loads((tmp_path / "agent/result.json").read_text())["metadata"]["agent"]
        == "BrokenPrepare"
    )
    assert "installation failed" in (tmp_path / "agent/logs/prepare.log").read_text()


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan")])
def test_invalid_stop_timeout(timeout):
    with pytest.raises(ValueError):
        AgentExecution(RecordingAgent()).stop(timeout=timeout)


def test_new_uses_configuration_without_copying_execution_state(tmp_path):
    agent, kwargs = prepared(tmp_path)
    agent.run(goal=[{"type": "text", "text": "first"}], **kwargs)
    fresh = AgentExecution(agent.agent.new())
    assert fresh.agent is not agent.agent
    assert fresh.agent.config == agent.agent.config
    assert fresh.output is None and fresh._state == "new"
    assert fresh._lock is not agent._lock
    assert fresh._cancelled is not agent._cancelled
    assert fresh._instance is None
    fresh.prepare(runtime=kwargs["runtime"], instance=kwargs["instance"],
                  output_dir=tmp_path / "fresh", cwd=str(tmp_path))
    result = fresh.run(goal=[{"type": "text", "text": "second"}], runtime=kwargs["runtime"],
                       instance=kwargs["instance"], output_dir=tmp_path / "fresh")
    assert result["output"][0]["text"] == "second"
    assert agent.output.result["output"][0]["text"] == "first"


def test_custom_configuration_is_copied_without_custom_creation_hooks(tmp_path):
    class CustomAgent(RecordingAgent):
        def prepare(self, *, runtime, instance, output, cwd: str):
            self.messages = []
            self.client_lock = threading.Lock()

        def run(self, goal, *, runtime, instance, output, cwd: str):
            self.messages.append(goal[0]["text"])
            return super().run(goal=goal, runtime=runtime, instance=instance, output=output, cwd=cwd)

    options = {"tools": ["read"], "nested": {"temperature": 0.7}}
    agent = CustomAgent(model="custom", endpoint="https://model.invalid", options=options,
                        cache=tmp_path / "cache")
    options["tools"].append("write")
    first, second = agent.new(), agent.new()
    assert first.config == second.config == agent.config
    assert first.config["options"]["tools"] == ["read"]
    assert first._identity()["config"]["endpoint"] == "https://model.invalid"
    exposed = first.config
    exposed["options"]["tools"].append("execute")
    assert first.config["options"]["tools"] == ["read"]
    for index, execution in enumerate((first, second)):
        prepared(tmp_path / str(index), execution)
    first.messages.append("first")
    assert second.messages == []
    assert first.client_lock is not second.client_lock
    assert not hasattr(agent, "client_lock")
    assert first.new().config == agent.config
    assert not hasattr(first.new(), "client_lock")


def test_explicit_custom_constructor_uses_base_creation_and_identity():
    class CustomAgent(RecordingAgent):
        def __init__(self, *, endpoint, temperature=0.7):
            super().__init__(endpoint=endpoint, temperature=temperature)

    agent = CustomAgent(endpoint="https://model.invalid", temperature=None)
    fresh = agent.new()
    assert fresh.config == {"endpoint": "https://model.invalid", "temperature": None}
    assert fresh._identity() == agent._identity()


@pytest.mark.parametrize("options", [{"name": ""}, {"name": 1}, {"model": 1}, {"version": []}])
def test_common_configuration_validation(options):
    with pytest.raises(ValueError):
        RecordingAgent(**options)


@pytest.mark.parametrize("value", [object(), threading.Lock(), {"client": object()}])
def test_runtime_objects_are_rejected_at_construction(value):
    with pytest.raises(TypeError, match="create runtime objects in prepare"):
        RecordingAgent(options=value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_configuration_is_rejected_at_construction(value):
    with pytest.raises(ValueError):
        RecordingAgent(options={"temperature": value})


def test_circular_configuration_is_rejected_at_construction():
    options = {}
    options["self"] = options
    with pytest.raises(ValueError, match="Circular reference"):
        RecordingAgent(options=options)
