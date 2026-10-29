import importlib
import json
import threading
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from envweaver import ErrorType, EvalConfig, Instance, RuntimeRef, Task, Verifier, eval
from envweaver.errors import AgentError, AgentStopReason
from envweaver.evaluation.failure import can_retry
from tests.agents.helpers import CooperativeAgent

evaluation = importlib.import_module("envweaver.evaluation.runner")


class FakeRuntime:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = set()
        self.starts = []
        self.closed = []
        self.delay = 0
        self.fail_close = False

    def start(self, **kwargs):
        time.sleep(self.delay)
        with self.lock:
            instance = Instance(str(len(self.starts)), "fake")
            self.starts.append(deepcopy(kwargs))
            self.active.add(instance.id)
            return instance

    def exec(self, *, instance, command, **kwargs):
        assert instance.id in self.active
        return 0, "", ""

    def close(self, instance):
        with self.lock:
            self.active.remove(instance.id)
            self.closed.append(instance.id)
        if self.fail_close:
            raise OSError("close failed")


class FakeVerifier(Verifier):
    def __init__(self, runtime, entry=Path(__file__).parents[1] / "fixtures/downstream/run.py"):
        super().__init__(entry=entry, runtime=runtime, timeout=3, required_env=["REQUIRED"])
        self.calls = []

    def run(self, **kwargs):
        self.calls.append({**kwargs, "timeout": self.timeout})
        return {"score": 0, "details": {"failed_tests": ["example"]}}


class GoodAgent(CooperativeAgent):
    def run(self, goal, *, runtime, instance, output, cwd: str):
        output.record("message", {"text": "done"})
        return output.finish([{"type": "text", "text": goal[0]["text"]}])


@pytest.fixture
def task(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("input")
    entry = tmp_path / "task-verifier/run.py"
    entry.parent.mkdir()
    entry.write_text('print(\'{"score": 0}\')')
    runtime = FakeRuntime()
    ref = RuntimeRef(
        "fake",
        "image",
        {"env": {"A": "base", "B": "keep"}, "args": ["old"]},
        {"ssh": {"user": "worker"}},
    )
    return Task(
        cwd="/", name="test",
        task_ref="task.json",
        goal=[{"type": "text", "text": "solve"}],
        metadata={"tag": "test"},
        task_runtime_ref=ref,
        context=[{"source": str(source), "target": "/data"}],
        setup={"command": "ready", "timeout": 1},
        verifier_runtime_ref=ref,
        verifier=FakeVerifier(runtime, entry=entry),
    )


def config(**kwargs):
    return EvalConfig(verifier_env={"REQUIRED": "yes", "EXTRA": "also"}, **kwargs)


def test_eval_success_preserves_task_and_overrides(tmp_path, task):
    original = deepcopy(task.task_runtime_ref)
    with pytest.warns(UserWarning, match="verify_timeout"):
        (result,) = eval(
            tasks=task,
            agent=GoodAgent(),
            config=config(
                verify_timeout=0.5, task_runtime_config={"env": {"A": "new"}, "args": ["new"]}
            ),
            output_dir=tmp_path / "result",
        )
    assert result["errors"] == []
    assert "evaluation_errors" not in result and "cleanup_errors" not in result
    assert result["agent_stop_reason"] == "completed"
    assert result["verification_result"]["score"] == 0
    assert task.task_runtime_ref == original
    assert task.verifier.timeout == 3
    assert task.verifier.calls[0]["timeout"] == 0.5
    assert task.verifier.calls[0]["env"]["EXTRA"] == "also"
    runtime = task.verifier.runtime
    assert runtime.starts[0]["ref"].config == {"env": {"A": "new", "B": "keep"}, "args": ["new"]}
    assert runtime.closed == ["1", "0"]
    assert not runtime.active
    assert json.loads((tmp_path / "result/000001/result.json").read_text()) == result


def test_failure_recovers_snapshot_without_verifying(tmp_path, task):
    class BrokenAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            output.update(metadata={"usage": {"tokens": 10}})
            raise OSError("connection lost")

    (result,) = eval(
        tasks=task,
        agent=BrokenAgent(),
        config=config(),
        output_dir=tmp_path / "result",
    )
    assert result["errors"][-1]["stage"] == "agent"
    assert result["agent_stop_reason"] == "error"
    assert result["agent_result"]["metadata"]["usage"]["tokens"] == 10
    assert result["verification_result"] is None
    assert not task.verifier.calls
    assert not task.verifier.runtime.active


def test_timeout_stops_agent_before_verifying_and_sandbox_close(tmp_path, task):
    stopped = threading.Event()
    verify = task.verifier.run

    def verify_after_stop(**kwargs):
        assert stopped.is_set()
        assert task.verifier.runtime.active == {"0", "1"}
        return verify(**kwargs)

    task.verifier.run = verify_after_stop

    class WaitingAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            self._cancelled.wait(5)
            assert runtime.active
            stopped.set()
            self._check_stopped()

    (result,) = eval(
        tasks=task,
        agent=WaitingAgent(),
        config=config(agent_timeout=0.05),
        output_dir=tmp_path / "result",
    )
    assert stopped.is_set()
    assert result["errors"][-1]["type"] == "timeout"
    assert result["agent_stop_reason"] == "timeout"
    assert result["errors"][-1]["stage"] == "agent"
    assert result["verification_result"]["score"] == 0
    assert len(task.verifier.calls) == 1
    assert not any(error["stage"] == "cleanup" for error in result["errors"])
    assert result["agent_result"] is not None
    assert not task.verifier.runtime.active


@pytest.mark.parametrize("reason", [
    AgentStopReason.TIMEOUT, AgentStopReason.CONTEXT_LIMIT, AgentStopReason.STEP_LIMIT,
    AgentStopReason.COST_LIMIT, AgentStopReason.TOKEN_LIMIT, AgentStopReason.OTHER_LIMIT,
])
def test_agent_limit_preserves_partial_result_and_verifies(tmp_path, task, reason):
    class LimitedAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            output.update(output=[{"type": "text", "text": "partial"}])
            raise AgentError(type=ErrorType(reason.value), message="budget exhausted")

    (result,) = eval(tasks=task, agent=LimitedAgent(), config=config(), output_dir=tmp_path / "run")
    assert result["errors"][-1]["type"] == reason
    assert result["agent_stop_reason"] == reason.value
    assert type(result["agent_stop_reason"]) is str
    assert result["verification_result"]["score"] == 0
    assert result["agent_result"]["output"][0]["text"] == "partial"
    assert not any(error["stage"] == "cleanup" for error in result["errors"])
    assert not task.verifier.runtime.active


def test_limit_and_verifier_failure_are_both_recorded(tmp_path, task):
    class LimitedAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            raise AgentError(ErrorType.STEP_LIMIT)

    def broken_verifier(**kwargs):
        raise OSError("verifier failed")

    task.verifier.run = broken_verifier
    (result,) = eval(tasks=task, agent=LimitedAgent(), config=config(), output_dir=tmp_path / "run")
    assert [error["stage"] for error in result["errors"]] == ["agent", "verify"]
    assert "error" not in result
    assert all("previous" not in error for error in result["errors"])
    assert result["agent_stop_reason"] == "step_limit"
    assert result["errors"][0]["type"] == "step_limit"
    assert result["verification_result"] is None
    assert not task.verifier.runtime.active


@pytest.mark.parametrize("score", [0.5, 1])
def test_limit_keeps_verifier_score(tmp_path, task, score):
    class LimitedAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            raise AgentError(ErrorType.STEP_LIMIT)

    task.verifier.run = lambda **kwargs: {"score": score, "details": {"partial": True}}
    (result,) = eval(tasks=task, agent=LimitedAgent(), config=config(), output_dir=tmp_path / "run")
    assert result["verification_result"] == {"score": score, "details": {"partial": True}}
    assert result["errors"][-1]["type"] == "step_limit"
    assert json.loads((tmp_path / "run/000001/result.json").read_text()) == result


def test_limit_does_not_verify_when_stop_fails(tmp_path, task):
    class UnstoppableAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            raise AgentError(ErrorType.CONTEXT_LIMIT)

        def stop(self, timeout=30):
            self._cancelled.set()
            raise OSError("cannot stop tools")

    (result,) = eval(tasks=task, agent=UnstoppableAgent(), config=config(), output_dir=tmp_path / "run")
    assert result["errors"][0]["type"] == "context_limit"
    assert any("cannot stop tools" in error["message"] for error in result["errors"] if error["stage"] == "cleanup")
    assert result["verification_result"] is None
    assert not result["score_valid"]
    assert not task.verifier.calls
    assert not task.verifier.runtime.active


@pytest.mark.parametrize("error,reason", [
    (TimeoutError("SSH timed out"), "error"), (InterruptedError("cancelled"), "cancelled"),
])
def test_internal_timeout_and_cancellation_do_not_verify(tmp_path, task, error, reason):
    class BrokenAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            raise error

    (result,) = eval(tasks=task, agent=BrokenAgent(), config=config(), output_dir=tmp_path / "run")
    assert result["errors"][-1]["type"] == ("transport" if isinstance(error, TimeoutError) else "cancelled")
    assert result["agent_stop_reason"] == reason
    assert not task.verifier.calls
    assert not task.verifier.runtime.active


def test_setup_timeout_reclaims_late_instance(tmp_path, task):
    task.verifier.runtime.delay = 0.12
    with pytest.warns(UserWarning, match="setup_timeout"):
        (result,) = eval(
            tasks=task,
            agent=GoodAgent(),
            config=config(setup_timeout=0.02, max_retries=2),
            output_dir=tmp_path / "result",
        )
    assert result["errors"][-1]["stage"] == "setup"
    assert result["agent_stop_reason"] is None
    assert task.verifier.runtime.closed == ["0"]
    assert not task.verifier.runtime.active
    assert len(task.verifier.runtime.starts) == 1
    assert not can_retry(result["errors"][-1])
    manifest = json.loads((tmp_path / "result/manifest.json").read_text())
    assert len(manifest["tasks"]["0"]["attempts"]) == 1


def test_setup_cleanup_reports_startup_still_pending(tmp_path, task, monkeypatch):
    release = threading.Event()
    closed = threading.Event()
    runtime = task.verifier.runtime
    start = runtime.start
    close = runtime.close

    def blocked_start(**kwargs):
        release.wait(5)
        return start(**kwargs)

    def record_close(instance):
        close(instance)
        closed.set()

    monkeypatch.setattr(runtime, "start", blocked_start)
    monkeypatch.setattr(runtime, "close", record_close)
    monkeypatch.setattr(evaluation, "_CLEANUP_TIMEOUT", 0.05)
    try:
        with pytest.warns(UserWarning, match="setup_timeout"):
            (result,) = eval(
                tasks=task, agent=GoodAgent(), config=config(setup_timeout=0.02, max_retries=0),
                output_dir=tmp_path / "result",
            )
        assert result["errors"][0]["stage"] == "setup"
        assert any("cleanup is pending" in error["message"] for error in result["errors"] if error["stage"] == "cleanup")
    finally:
        release.set()
        assert closed.wait(2)
    assert not runtime.active


@pytest.mark.parametrize("operation", ["upload", "setup"])
def test_setup_timeout_closes_instance_before_runtime_start_returns(tmp_path, task, operation):
    from envweaver import Runtime

    closed = threading.Event()
    entered = threading.Event()

    class Backend:
        def start(self, ref, connection=None):
            return Instance("owned", "managed")

        def upload(self, *args):
            entered.set()
            assert closed.wait(3), "Eval must close the instance while start is blocked"
            raise OSError("instance closed")

        def exec(self, *args):
            entered.set()
            assert closed.wait(3), "Eval must close the instance while setup is blocked"
            raise OSError("instance closed")

        def close(self, instance):
            assert not closed.is_set(), "instance should be closed once"
            closed.set()

    runtime = Runtime()
    runtime._backends["managed"] = Backend()
    source = tmp_path / "input"
    source.write_text("input")
    ref = RuntimeRef("managed", "image")
    task = replace(
        task,
        task_runtime_ref=ref,
        verifier_runtime_ref=ref,
        context=[{"source": str(source), "target": "/input"}] if operation == "upload" else [],
        setup={"command": "setup", "timeout": 1},
        verifier=FakeVerifier(runtime, entry=task.verifier.entry),
    )
    with pytest.warns(UserWarning, match="setup_timeout"):
        (result,) = eval(
            tasks=task,
            agent=GoodAgent(),
            config=config(setup_timeout=0.05, max_retries=0),
            output_dir=tmp_path / "result",
        )
    assert entered.is_set() and closed.is_set()
    assert result["errors"][-1]["type"] == "timeout"
    assert not any(error["stage"] == "cleanup" for error in result["errors"])


def test_prepare_and_cleanup_failures_share_ordered_errors(tmp_path, task):
    class BrokenAgent(GoodAgent):
        def prepare(self, *, runtime, instance, output, cwd: str):
            raise RuntimeError("prepare failed")

    task.verifier.runtime.fail_close = True
    (result,) = eval(
        tasks=task,
        agent=BrokenAgent(),
        config=config(),
        output_dir=tmp_path / "result",
    )
    assert result["errors"][0]["message"] == "prepare failed"
    assert [error["stage"] for error in result["errors"]] == ["setup", "cleanup", "cleanup"]
    assert sum(error["stage"] == "cleanup" for error in result["errors"]) == 2
    assert result["agent_result"] is not None


def test_missing_env_fails_before_start(tmp_path, task):
    (result,) = eval(tasks=task, agent=GoodAgent(), output_dir=tmp_path / "result")
    assert result["errors"][-1]["stage"] == "preflight"
    assert not task.verifier.runtime.starts


def test_rich_panel_shows_zero_score_by_default(tmp_path, task, capsys):
    (result,) = eval(
        tasks=task, agent=GoodAgent(), config=config(), output_dir=tmp_path / "result"
    )
    assert result["verification_result"]["score"] == 0
    rendered = capsys.readouterr().err
    assert "Eval" in rendered and "done" in rendered and "0" in rendered


def test_output_directory_collision_is_not_overwritten(tmp_path, task):
    with pytest.raises(FileExistsError):
        eval(tasks=task, agent=GoodAgent(), config=config(), output_dir=tmp_path)
    assert not task.verifier.runtime.starts


def test_verify_without_task_instance(tmp_path, task):
    task.verifier.requires_task_instance = False
    (result,) = eval(
        tasks=task,
        agent=GoodAgent(),
        config=config(),
        output_dir=tmp_path / "result",
    )
    assert result["errors"] == []
    assert task.verifier.calls[0]["task_instance"] is None


def test_batch_concurrency_is_bounded_and_results_ordered(tmp_path, task):
    state = SimpleNamespace(active=0, peak=0)
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    class ParallelAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            with lock:
                state.active += 1
                state.peak = max(state.peak, state.active)
            try:
                barrier.wait(timeout=3)
                time.sleep(0.01)
                return super().run(goal=goal, runtime=runtime, instance=instance, output=output, cwd=cwd)
            finally:
                with lock:
                    state.active -= 1

    tasks = [replace(task, name=str(index)) for index in range(4)]
    results = eval(
        tasks=tasks,
        agent=ParallelAgent(),
        config=config(),
        output_dir=tmp_path / "batch",
        concurrency=2,
    )
    assert state.peak == 2
    assert [result["name"] for result in results] == ["0", "1", "2", "3"]
    assert all(result["errors"] == [] for result in results)
    assert len(list((tmp_path / "batch").glob("*/agent/result.json"))) == 4
    assert not task.verifier.runtime.active


def test_batch_rejects_reused_agent_without_stopping_first_trial(tmp_path, task):
    class ReusedAgent(GoodAgent):
        def new(self):
            return shared

    shared = ReusedAgent()
    results = eval(
        tasks=[task, task],
        agent=ReusedAgent(),
        config=config(),
        output_dir=tmp_path / "batch",
        concurrency=2,
    )
    assert sum(result["errors"] == [] for result in results) == 1
    assert not task.verifier.runtime.active


def test_eval_runs_real_verifier_subprocess(tmp_path, task):
    from envweaver import Verifier
    from tests.verifier.test_verifier_instance import LocalTransport

    class RuntimeTransport(LocalTransport):
        def start(self, ref, context=None, setup=None, connection=None):
            assert not context and setup is None
            return super().start(ref=ref, connection=connection)

    root = tmp_path / "sandboxes"
    root.mkdir()
    runtime = RuntimeTransport(root)
    materials = tmp_path / "verifier"
    materials.mkdir()
    entry = materials / "run.py"
    entry.write_text(
        'import json; print(json.dumps({"score": 0.75, "details": {"checked": True}}))'
    )
    verifier = Verifier(entry=entry, runtime=runtime, timeout=5, requires_task_instance=False)
    task = replace(task, verifier=verifier, context=[], setup=None)
    (result,) = eval(tasks=task, agent=GoodAgent(), output_dir=tmp_path / "result")
    assert result["verification_result"] == {"score": 0.75, "details": {"checked": True}}
    assert result["errors"] == []
    assert len(runtime.closed) == 2
    assert not list(root.iterdir())


def test_keyboard_interrupt_persists_result_and_cleans_resources(tmp_path, task):
    class InterruptedAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        eval(
            tasks=task,
            agent=InterruptedAgent(),
            config=config(),
            output_dir=tmp_path / "result",
        )
    result = json.loads((tmp_path / "result/000001/result.json").read_text())
    assert result["errors"][-1]["type"] == "cancelled"
    assert json.loads((tmp_path / "result/results.json").read_text()) == [result]
    assert not task.verifier.runtime.active


def test_second_sandbox_failure_closes_first(tmp_path, task):
    original = task.verifier.runtime.start

    def start(**kwargs):
        if task.verifier.runtime.starts:
            raise RuntimeError("verifier image unavailable")
        return original(**kwargs)

    task.verifier.runtime.start = start
    (result,) = eval(
        tasks=task,
        agent=GoodAgent(),
        config=config(),
        output_dir=tmp_path / "result",
    )
    assert result["errors"][-1]["stage"] == "setup"
    assert task.verifier.runtime.closed == ["0"]


def test_broken_snapshot_does_not_hide_original_error(tmp_path, task):
    class BrokenSnapshotAgent(GoodAgent):
        def prepare(self, *, runtime, instance, output, cwd: str):
            (output.directory / "result.json").write_text("{")
            raise RuntimeError("original failure")

    (result,) = eval(
        tasks=task,
        agent=BrokenSnapshotAgent(),
        config=config(),
        output_dir=tmp_path / "result",
    )
    assert result["errors"][0]["message"] == "original failure"
    assert any(message["message"].startswith("snapshot:") for message in result["errors"] if message["stage"] == "cleanup")
    assert not task.verifier.runtime.active


@pytest.mark.parametrize(
    "field,value",
    [
        ("setup_timeout", 0),
        ("agent_timeout", float("inf")),
        ("verify_timeout", True),
        ("task_connection", []),
        ("verifier_env", {"KEY": 1}),
    ],
)
def test_config_validation(field, value):
    with pytest.raises(ValueError):
        EvalConfig(**{field: value})


@pytest.mark.parametrize("form", ["task", "list", "iterator"])
def test_single_task_has_same_results_and_artifact_layout(tmp_path, task, form):
    tasks = task if form == "task" else [task] if form == "list" else iter([task])
    results = eval(
        tasks=tasks, agent=GoodAgent(), config=config(), output_dir=tmp_path / "run"
    )
    assert isinstance(results, list) and len(results) == 1
    assert results[0]["errors"] == []
    assert json.loads((tmp_path / "run/results.json").read_text()) == results
    assert json.loads((tmp_path / "run/000001/result.json").read_text()) == results[0]
    assert (tmp_path / "run/000001/agent/logs/events.jsonl").is_file()


def test_agent_object_is_reusable_across_batches(tmp_path, task):
    agent = GoodAgent(model="test-model")
    for batch in range(2):
        results = eval(
            tasks=iter([task, task]), agent=agent, config=config(),
            output_dir=tmp_path / str(batch), concurrency=2,
        )
        assert all(result["score_valid"] for result in results)
    assert not hasattr(agent, "_state")
    assert not hasattr(agent, "_claimed") and not hasattr(agent, "_output_dir")
    assert not agent._cancelled.is_set()
    assert len(task.verifier.runtime.starts) == 8


def test_empty_tasks_returns_and_saves_empty_list(tmp_path):
    class UnusedAgent(GoodAgent):
        def new(self):
            pytest.fail("No Agent should be created for empty tasks")

    assert eval(tasks=[], agent=UnusedAgent(), output_dir=tmp_path / "run") == []
    assert json.loads((tmp_path / "run/results.json").read_text()) == []


def test_agent_creation_failure_does_not_stop_remaining_tasks(tmp_path, task):
    created = 0

    class CreationAgent(GoodAgent):
        def new(self):
            nonlocal created
            created += 1
            if created == 1:
                raise RuntimeError("initialization failed")
            return super().new()

    results = eval(
        tasks=[task, task],
        agent=CreationAgent(),
        config=config(),
        output_dir=tmp_path / "run",
    )
    assert results[0]["errors"][-1]["message"] == "initialization failed"
    assert results[1]["errors"] == []
    assert json.loads((tmp_path / "run/results.json").read_text()) == results


@pytest.mark.parametrize("concurrency", [0, -1, True, 1.5])
def test_eval_rejects_invalid_concurrency(tmp_path, task, concurrency):
    with pytest.raises(ValueError, match="concurrency"):
        eval(tasks=task, agent=GoodAgent(), concurrency=concurrency, output_dir=tmp_path / "run")
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("accessible", [True, False])
def test_task_cwd_checked_after_setup(tmp_path, task, monkeypatch, accessible):
    runtime = task.verifier.runtime
    task = replace(task, cwd="/data/project")
    checks = []

    def execute(*, instance, command, timeout):
        assert len(runtime.starts) == 2
        assert runtime.starts[0]["context"] == task.context
        assert runtime.starts[0]["setup"] == task.setup
        assert instance.id == "0"
        checks.append(command)
        return (0, "", "") if accessible else (1, "", "directory missing")

    monkeypatch.setattr(runtime, "exec", execute, raising=False)

    class WorkingAgent(GoodAgent):
        def prepare(self, *, runtime, instance, output, cwd: str):
            assert checks
            assert cwd == task.cwd

        def run(self, goal, *, runtime, instance, output, cwd: str):
            assert cwd == task.cwd
            return super().run(goal=goal, runtime=runtime, instance=instance, output=output, cwd=cwd)

    (result,) = eval(
        tasks=task, agent=WorkingAgent(), config=config(),
        output_dir=tmp_path / "evaluation",
    )
    assert len(checks) == 1
    assert "/data/project" in checks[0][-1]
    if accessible:
        assert result["errors"] == []
    else:
        assert result["errors"][-1]["stage"] == "setup"
        assert not task.verifier.calls
    assert not runtime.active


def test_concurrent_tasks_use_their_own_cwd(tmp_path, task, monkeypatch):
    runtime = task.verifier.runtime
    tasks = [replace(task, name=name, cwd=f"/work/{name}") for name in ("first", "second")]
    monkeypatch.setattr(runtime, "exec", lambda **kwargs: (0, "", ""), raising=False)
    prepared = threading.Barrier(2)

    class DirectoryAgent(CooperativeAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            prepared.wait(timeout=5)
            return output.finish([{"type": "text", "text": cwd}])

    results = eval(
        tasks=tasks, agent=DirectoryAgent(), config=config(), concurrency=2,
        output_dir=tmp_path / "evaluation",
    )
    for item, result in zip(tasks, results, strict=True):
        assert result["errors"] == []
        assert result["agent_result"]["output"] == [{"type": "text", "text": item.cwd}]
    assert [item.cwd for item in tasks] == ["/work/first", "/work/second"]
    assert not runtime.active


@pytest.mark.parametrize("invalid", [GoodAgent, lambda: GoodAgent(), None])
def test_eval_rejects_factory_and_non_agent_before_creating_output(tmp_path, task, invalid):
    with pytest.raises(TypeError, match="agent must be an Agent"):
        eval(tasks=task, agent=invalid, config=config(), output_dir=tmp_path / "run")
    assert not (tmp_path / "run").exists()
    assert not task.verifier.runtime.starts


@pytest.mark.parametrize("returned", ["self", "object", "changed_config"])
def test_invalid_new_does_not_start_or_modify_template(tmp_path, task, returned):
    class InvalidAgent(GoodAgent):
        def new(self):
            if returned == "changed_config":
                return type(self)(model="different")
            return self if returned == "self" else object()

    agent = InvalidAgent()
    result, = eval(tasks=task, agent=agent, config=config(), output_dir=tmp_path / "run")
    assert result["errors"][-1]["stage"] == "preflight"
    assert "Agent.new()" in result["errors"][-1]["message"]
    assert not hasattr(agent, "_state") and not hasattr(agent, "_claimed")
    assert not agent._cancelled.is_set()
    assert not task.verifier.runtime.starts
