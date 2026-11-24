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
from envweaver.evaluation.panel import _Panel
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


def test_deadline_during_repeated_provider_failure_skips_verifier(tmp_path, task):
    class ThrottledAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd):
            self._cancelled.wait(5)
            raise AgentError(ErrorType.PROVIDER_TRANSIENT, "provider repeatedly returned 429")

    (result,) = eval(tasks=task, agent=ThrottledAgent(), config=config(agent_timeout=0.05),
                     output_dir=tmp_path / "result")
    assert result["agent_stop_reason"] == "error"
    assert [error["type"] for error in result["errors"]] == ["provider_transient"]
    assert not result["score_valid"] and not task.verifier.calls


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
    assert "1/1" in rendered and "平均 score 0" in rendered and "运行 0" in rendered


def test_progress_uses_final_valid_scores_and_keeps_elapsed_time_live(monkeypatch):
    from rich.console import Console

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    panel = _Panel(total=96)
    console = Console(width=160, color_system=None)

    def render():
        with console.capture() as capture:
            console.print(panel._render())
        return capture.get()

    for index, value in enumerate([0, 0.5, 1]):
        result = {"score_valid": True, "verification_result": {"score": value}, "errors": []}
        panel.update(index, "same-task", "done", result)
        panel.finish(index, result)
        panel.finish(index, result)  # Repeated/resumed notification must not double-count.
    invalid = {"score_valid": False, "verification_result": {"score": 0}, "errors": []}
    panel.update(3, "unscored", "failed", invalid)
    panel.finish(3, invalid)
    panel.update(4, "retrying", "failed", invalid)
    panel.update(4, "retrying", "retry")
    for index in range(5, 8):
        panel.update(index, "running", "agent")
    clock[0] = 1080.0
    text = render()
    assert "4/96" in text and "平均 score 0.5" in text
    assert "运行 4" in text and "已用时 18m" in text
    clock[0] += 60
    assert "已用时 19m" in render()  # No stage update is needed for the timer.


def test_progress_without_scores_and_cancelled_attempt():
    from rich.console import Console

    panel = _Panel()
    panel.finish(0, {"agent_stop_reason": "cancelled", "errors": []})
    console = Console(width=160, color_system=None)
    with console.capture() as capture:
        console.print(panel._render())
    text = capture.get()
    assert "0/?" in text and "平均 score —" in text


@pytest.mark.parametrize("streaming", [False, True])
def test_progress_total_and_resume(tmp_path, task, capsys, streaming):
    output = tmp_path / "batch"
    for resume in (False, True):
        tasks = iter([task, task]) if streaming else [task, task]
        eval(tasks=tasks, agent=GoodAgent(), config=config(), output_dir=output, resume=resume)
        rendered = capsys.readouterr().err
        assert "2/2" in rendered and "平均 score 0" in rendered and "运行 0" in rendered


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


def test_auxiliary_instance_survives_until_verify_and_is_fresh_on_retry(tmp_path, task):
    tools = []
    runtime = task.verifier.runtime

    class ToolAgent(GoodAgent):
        def prepare(self, *, runtime, instance, output, cwd):
            tool = runtime.start(ref=RuntimeRef("fake", "tools"))
            assert all(previous not in runtime.active for previous in tools)
            tools.append(tool.id)

        def run(self, **kwargs):
            if len(tools) == 1:
                raise AgentError(ErrorType.PROVIDER_TRANSIENT, "retry")
            raise AgentError(ErrorType.STEP_LIMIT)

    def verify(**kwargs):
        assert tools[-1] in runtime.active
        return {"score": 1}

    task.verifier.run = verify
    result, = eval(tasks=task, agent=ToolAgent(), config=config(max_retries=1, retry_delay=0.001),
                   output_dir=tmp_path / "result")
    assert result["score_valid"] and result["verification_result"]["score"] == 1
    assert len(set(tools)) == 2
    assert not runtime.active


def test_keyboard_interrupt_persists_result_and_cleans_resources(tmp_path, task):
    class InterruptedAgent(GoodAgent):
        def prepare(self, *, runtime, instance, output, cwd):
            runtime.start(ref=RuntimeRef("fake", "tools"))

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


def test_parallel_attempts_clean_only_their_own_tools(tmp_path, task, monkeypatch):
    both_preparing = threading.Barrier(2)
    failed_tool_closed = threading.Event()
    raw = task.verifier.runtime
    close = raw.close
    tools = {}

    def record_close(instance):
        close(instance)
        if instance == tools.get("/fail"):
            failed_tool_closed.set()

    monkeypatch.setattr(raw, "close", record_close)

    class ToolAgent(GoodAgent):
        def prepare(self, *, runtime, instance, output, cwd):
            tools[cwd] = runtime.start(ref=RuntimeRef("fake", "tools"))
            both_preparing.wait(timeout=5)
            if cwd == "/fail":
                raise ValueError("prepare failed after starting tools")
            assert failed_tool_closed.wait(5)

    def verify(**kwargs):
        assert tools["/fail"].id not in raw.active
        assert tools["/ok"].id in raw.active
        return {"score": 1}

    task.verifier.run = verify
    failed, ok = eval(
        tasks=[replace(task, name=name, cwd="/" + name) for name in ("fail", "ok")],
        agent=ToolAgent(), config=config(), output_dir=tmp_path / "parallel", concurrency=2,
    )
    assert failed["errors"][0]["message"] == "prepare failed after starting tools"
    assert not failed["score_valid"]
    assert ok["errors"] == [] and ok["verification_result"]["score"] == 1
    assert len(raw.closed) == 6 and not raw.active


def test_attempt_close_and_connect(tmp_path, task, monkeypatch):
    raw = task.verifier.runtime
    external = raw.start(ref=RuntimeRef("fake", "external"))
    monkeypatch.setattr(raw, "connect", lambda path: external, raising=False)
    close = raw.close
    failed = []

    def fail_first_close(instance):
        if not failed:
            failed.append(instance)
            raise OSError("close failed")
        close(instance)

    monkeypatch.setattr(raw, "close", fail_first_close)

    class ToolAgent(GoodAgent):
        def run(self, *, runtime, **kwargs):
            assert isinstance(runtime, FakeRuntime)
            assert runtime.connect("external") == external
            with pytest.raises(OSError, match="close failed"):
                runtime.close(runtime.start(ref=RuntimeRef("fake", "retry-close")))
            runtime.close(runtime.start(ref=RuntimeRef("fake", "close-early")))
            return super().run(runtime=runtime, **kwargs)

    result, = eval(tasks=task, agent=ToolAgent(), config=config(), output_dir=tmp_path / "run")
    assert result["errors"] == []
    assert raw.active == {external.id}
    assert len(raw.closed) == len(set(raw.closed)) == 4
    close(external)


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


@pytest.mark.parametrize('limits', [
    {}, {'setup': 1}, {'setup': 1, 'agent': 2, 'verify': 1, 'cleanup': 1},
    {'setup': 0, 'agent': 2, 'verify': 1},
    {'setup': 1, 'agent': True, 'verify': 1},
    {'setup': 1, 'agent': 2, 'verify': 1.5}, None, [],
])
def test_invalid_stage_concurrency_allocates_nothing(tmp_path, task, limits):
    with pytest.raises(ValueError, match='concurrency'):
        eval(tasks=task, agent=GoodAgent(), concurrency=limits, output_dir=tmp_path / 'run')
    assert not (tmp_path / 'run').exists()
    assert not task.verifier.runtime.starts


def test_stage_pipeline_reaches_40_agents_with_four_setup_slots(tmp_path, task):
    from contextlib import contextmanager

    active = dict.fromkeys(('setup', 'agent', 'verify'), 0)
    peak = active.copy()
    lock = threading.Lock()
    agents_ready = threading.Barrier(40)
    verifiers_ready = threading.Barrier(4)

    @contextmanager
    def track(stage):
        with lock:
            active[stage] += 1
            peak[stage] = max(peak[stage], active[stage])
        try:
            yield
        finally:
            with lock:
                active[stage] -= 1

    runtime = task.verifier.runtime
    original_start = runtime.start

    def start(**kwargs):
        with track('setup'):
            time.sleep(0.02)
            return original_start(**kwargs)

    runtime.start = start

    class PipelineAgent(GoodAgent):
        def prepare(self, **kwargs):
            with track('setup'):
                time.sleep(0.02)

        def run(self, **kwargs):
            with track('agent'):
                agents_ready.wait(timeout=10)
                return super().run(**kwargs)

    def verify(**kwargs):
        with track('verify'):
            verifiers_ready.wait(timeout=10)
            time.sleep(0.01)
            return {'score': 1}

    task.verifier.run = verify
    tasks = [replace(task, name=f'task-{i}') for i in range(40)]
    results = eval(tasks=tasks, agent=PipelineAgent(), config=config(),
                   concurrency={'setup': 4, 'agent': 40, 'verify': 4},
                   output_dir=tmp_path / 'run')
    assert peak == {'setup': 4, 'agent': 40, 'verify': 4}
    assert all(row['score_valid'] and not row['errors'] for row in results)
    assert [row['name'] for row in results] == [t.name for t in tasks]
    assert not any(active.values()) and not runtime.active


def test_stage_queue_wait_is_not_execution_timeout_and_late_worker_holds_slot():
    limits = evaluation._Concurrency({'setup': 1, 'agent': 1, 'verify': 1})
    release = threading.Event()
    cancelled = threading.Event()
    entered = threading.Event()
    first = limits.start('setup', lambda: release.wait(5), cancelled)
    try:
        with pytest.raises(evaluation.StageTimeout):
            first.wait(timeout=0.01)
        second = evaluation._Worker(
            lambda: limits.start('setup', lambda: entered.set(), cancelled).wait(timeout=0.1)
        )
        assert not entered.wait(0.2)  # Even after the caller's timeout, the first operation owns the slot.
        release.set()
        first.wait(timeout=1)
        second.wait(timeout=1)
        assert entered.is_set()
    finally:
        release.set()


def test_cancellation_interrupts_stage_queue_without_starting_work():
    limits = evaluation._Concurrency(1)
    release = threading.Event()
    cancelled = threading.Event()
    entered = threading.Event()
    first = limits.start('verify', lambda: release.wait(5), cancelled)
    try:
        waiting = evaluation._Worker(lambda: limits.start('verify', entered.set, cancelled))
        cancelled.set()
        with pytest.raises(InterruptedError):
            waiting.wait(timeout=1)
        assert not entered.is_set()
    finally:
        release.set()
        first.wait(timeout=1)
    # Cancellation must not consume a slot, and an operation error must release it.
    cancelled.clear()
    def fail():
        raise OSError('verification failed')
    with pytest.raises(OSError):
        limits.start('verify', fail, cancelled).wait(timeout=1)
    limits.start('verify', entered.set, cancelled).wait(timeout=1)
    assert entered.is_set()


def test_ale_batch_install_failure_and_resume(tmp_path, task, monkeypatch):
    from envweaver import AleClawAgent

    directory = tmp_path / "batch"
    agent = AleClawAgent(version="test-version")
    attempts = []
    workers = []
    fail_install = True
    host_dirs = []

    def install(self, cmd, cwd, payload=None, *, preparing=False):
        assert preparing
        attempts.append(cmd)
        if cmd[:3] == ["uv", "pip", "install"]:
            host_dirs.append(Path(cwd))
            if fail_install:
                raise AgentError(ErrorType.CONFIGURATION, "install failed")
            time.sleep(0.6)  # Longer than the per-task setup timeout.
        return '["0.1.0", "test-model"]'

    def prepare(self, **kwargs):
        assert host_dirs[-1].is_dir()
        workers.append(self)

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(AleClawAgent, "_execute", install)
    monkeypatch.setattr(AleClawAgent, "prepare", prepare)
    monkeypatch.setattr(AleClawAgent, "run", GoodAgent.run)
    task = replace(task, setup=None)
    arguments = dict(tasks=[task] * 3, agent=agent, config=config(setup_timeout=0.5),
                     output_dir=directory, concurrency=3)
    with pytest.raises(AgentError, match="install failed"):
        eval(**arguments)
    assert [cmd[:2] for cmd in attempts] == [["uv", "venv"], ["uv", "pip"]]
    assert not task.verifier.runtime.starts
    assert not list(directory.glob("000*"))

    fail_install = False
    attempts.clear()
    assert all(row["score_valid"] for row in eval(**arguments, resume=True))
    assert len(attempts) == 3  # create venv, install package, validate controller
    assert len(set(workers)) == 3
    assert len({worker._controller for worker in workers}) == 1
    assert all(not path.exists() for path in host_dirs)

    attempts.clear()
    eval(**arguments, resume=True)
    assert not attempts
