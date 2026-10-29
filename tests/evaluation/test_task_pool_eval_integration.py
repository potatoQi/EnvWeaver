"""Opt-in Eval checks on existing producer Tasks, including their real verifiers."""

import importlib
import json
import os
from pathlib import Path

import pytest

from envweaver import AgentStopReason, ErrorType, EvalConfig, Runtime, TaskPool, eval
from envweaver.errors import AgentError
from envweaver.evaluation.failure import can_retry
from tests.agents.helpers import CooperativeAgent
from tests.agents.test_model_integration import make_agent

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not all(os.environ.get(key) for key in ("ENVWEAVER_TEST_TASK_REFS", "ENVWEAVER_TEST_TASK_POOL")),
        reason="requires a Task Pool, JSON Task references, and their original runtime images",
    ),
]

requires_model = pytest.mark.skipif(
    not all(os.environ.get(key) for key in (
        "ENVWEAVER_TEST_MODEL_BASE_URL", "ENVWEAVER_TEST_MODEL_KEY", "ENVWEAVER_TEST_MODEL_NAME",
    )),
    reason="requires a model endpoint, key, and model name",
)


class NoopAgent(CooperativeAgent):
    def run(self, goal, *, runtime, instance, output, cwd: str):
        return output.finish([])


@pytest.fixture
def production_tasks(tmp_path):
    records = tmp_path / "records"
    records.mkdir()
    if archive := os.environ.get("ENVWEAVER_TEST_IMAGE_STORE"):
        (records / "image-store").symlink_to(archive, target_is_directory=True)
    runtime = Runtime(root=records)
    if adapter := os.environ.get("ENVWEAVER_TEST_TASK_BACKEND"):
        module, name = adapter.split(":")
        runtime.register(name="docker", backend=getattr(importlib.import_module(module), name))
    refs = json.loads(os.environ["ENVWEAVER_TEST_TASK_REFS"])
    originals = {path: Path(path).read_bytes() for path in refs}
    pool = TaskPool(root=os.environ["ENVWEAVER_TEST_TASK_POOL"], runtime=runtime)
    tasks = [pool.load(task_ref=path) for path in refs]
    yield tasks
    assert not list((tmp_path / "records/instances").glob("*/instance.json"))
    assert all(Path(path).read_bytes() == body for path, body in originals.items())


def configuration(task, **overrides):
    label = os.environ.get("ENVWEAVER_TEST_RUN_ID", "producer-tasks")
    configs = {}
    for prefix, ref in (("task", task.task_runtime_ref), ("verifier", task.verifier_runtime_ref)):
        configs[prefix + "_runtime_config"] = {
            "args": [*ref.config.get("args", []), "--label", f"envweaver.task-test={label}"],
        }
    configs["task_runtime_config"]["network"] = "bridge"
    overrides.setdefault("max_retries", 2)
    return EvalConfig(agent_timeout=600, retry_delay=0.01, **configs, **overrides)


def attempts(output):
    return json.loads((output / "manifest.json").read_text())["tasks"]["0"]["attempts"]


@requires_model
def test_producer_tasks_real_model_and_resume(production_tasks, tmp_path):
    output = tmp_path / "batch"
    kwargs = dict(
        tasks=production_tasks, agent=make_agent("codex"),
        config=configuration(production_tasks[0]), output_dir=output, concurrency=3,
    )
    results = eval(**kwargs)
    assert [r["name"] for r in results] == [t.name for t in production_tasks]
    for result in results:
        assert not any(error["stage"] == "cleanup" for error in result["errors"]), result
        assert result["score_valid"] and 0 <= result["verification_result"]["score"] <= 1, result
        if result["errors"] != []:
            # A difficult production task can exhaust its budget and still be graded.
            assert ErrorType(result["errors"][-1]["type"]).is_limit, result
            assert AgentStopReason(result["agent_stop_reason"]).should_verify
        assert result["agent_result"]["artifacts"]["logs"]
    before = (output / "manifest.json").read_bytes()
    assert eval(**kwargs, resume=True) == results
    assert (output / "manifest.json").read_bytes() == before


def test_producer_task_transient_failure_restarts_in_new_sandbox(production_tasks, tmp_path):
    task = production_tasks[0]
    output = tmp_path / "retry"
    instances = []

    class FlakyAgent(CooperativeAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            instances.append(instance.id)
            marker = "/tmp/envweaver-retry-marker"
            if len(instances) == 1:
                runtime.exec(instance=instance, command=["touch", marker])
                output.record("checkpoint", {"injected_failure": True})
                raise AgentError(ErrorType.PROVIDER_TRANSIENT, "injected stream interruption")
            code, _, _ = runtime.exec(
                instance=instance, command=["test", "!", "-e", marker],
            )
            assert code == 0, "Previous sandbox state leaked into the retry"
            return output.finish([])

    result, = eval(tasks=task, agent=FlakyAgent(), config=configuration(task), output_dir=output)
    assert len(instances) == len(set(instances)) == 2
    assert result["score_valid"] and not any(error["stage"] == "cleanup" for error in result["errors"])
    records = attempts(output)
    assert len(records) == 2
    assert records[0]["result"]["verification_result"] is None
    assert can_retry(records[0]["result"]["errors"][-1])
    assert (output / "000001/agent/logs/events.jsonl").exists()
    assert result["output_dir"] == "000001/attempt-000002"


def test_producer_task_retry_exhaustion(production_tasks, tmp_path):
    class FailingAgent(CooperativeAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            raise ConnectionResetError("injected transport interruption")

    task, output = production_tasks[0], tmp_path / "exhausted"
    result, = eval(tasks=task, agent=FailingAgent(), config=configuration(task), output_dir=output)
    assert len(attempts(output)) == 3
    assert result["errors"][-1]["type"] == "transport"
    assert result["verification_result"] is None and not result["score_valid"]
    assert not any(error["stage"] == "cleanup" for error in result["errors"])


@pytest.mark.parametrize("reason", [AgentStopReason.CONTEXT_LIMIT, AgentStopReason.STEP_LIMIT])
def test_producer_task_limits_still_verify(production_tasks, tmp_path, reason):
    class LimitedAgent(CooperativeAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            raise AgentError(ErrorType(reason.value))

    task, output = production_tasks[0], tmp_path / "limited"
    result, = eval(tasks=task, agent=LimitedAgent(), config=configuration(task), output_dir=output)
    assert result["agent_stop_reason"] == reason.value
    assert result["score_valid"] and result["verification_result"] is not None
    assert not any(error["stage"] == "cleanup" for error in result["errors"]) and len(attempts(output)) == 1


def test_producer_task_outer_timeout_still_verifies(production_tasks, tmp_path):
    class WaitingAgent(CooperativeAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            output.record("checkpoint", {"partial": True})
            self._cancelled.wait(60)
            self._check_stopped()
            return output.finish([])

    task, output = production_tasks[0], tmp_path / "timeout"
    config = configuration(task)
    config.agent_timeout = 0.1
    result, = eval(tasks=task, agent=WaitingAgent(), config=config, output_dir=output)
    assert result["agent_stop_reason"] == "timeout"
    assert result["score_valid"] and result["verification_result"] is not None
    assert not any(error["stage"] == "cleanup" for error in result["errors"]) and len(attempts(output)) == 1


@requires_model
def test_producer_task_real_auth_failure_is_not_retried(production_tasks, tmp_path):
    task, output = production_tasks[0], tmp_path / "auth"
    result, = eval(
        tasks=task, agent=make_agent("codex", key="invalid-test-key"),
        config=configuration(task), output_dir=output,
    )
    assert result["errors"][-1]["type"] == "authentication", result
    assert result["verification_result"] is None and not result["score_valid"]
    assert not any(error["stage"] == "cleanup" for error in result["errors"]) and len(attempts(output)) == 1


def test_producer_task_verifier_timeout_is_not_retried(production_tasks, tmp_path):
    task, output = production_tasks[0], tmp_path / "verify-timeout"
    with pytest.warns(UserWarning, match="verify_timeout"):
        result, = eval(
            tasks=task, agent=NoopAgent(), config=configuration(task, verify_timeout=0.01),
            output_dir=output,
        )
    assert result["errors"][-1]["type"] == "verifier", result
    assert result["verification_result"] is None and not result["score_valid"]
    assert not any(error["stage"] == "cleanup" for error in result["errors"]) and len(attempts(output)) == 1


def test_producer_task_interrupt_and_resume(production_tasks, tmp_path):
    interrupted = True

    class InterruptibleAgent(CooperativeAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            output.record("checkpoint", {"partial": True})
            if interrupted:
                raise KeyboardInterrupt()
            return output.finish([])

    task, output = production_tasks[0], tmp_path / "resume"
    kwargs = dict(tasks=task, agent=InterruptibleAgent(), config=configuration(task), output_dir=output)
    with pytest.raises(KeyboardInterrupt):
        eval(**kwargs)
    first = (output / "000001/agent/logs/events.jsonl").read_bytes()
    interrupted = False
    result, = eval(**kwargs, resume=True)
    assert result["score_valid"] and not any(error["stage"] == "cleanup" for error in result["errors"])
    assert len(attempts(output)) == 2
    assert (output / "000001/agent/logs/events.jsonl").read_bytes() == first
