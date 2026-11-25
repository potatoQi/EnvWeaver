import json
from dataclasses import replace

import pytest

from envweaver import CodexAgent, ErrorType, MiniSweAgent, eval
from envweaver.errors import AgentError
from envweaver.evaluation.failure import can_retry
from tests.evaluation.test_eval import GoodAgent, config
from tests.evaluation.test_eval import task as task_fixture
from tests.io.test_output_addresses import address
from tests.io.test_output_addresses import ssh_files as ssh_files_fixture

task = task_fixture
ssh_files = ssh_files_fixture


def read(path):
    return json.loads(path.read_text())


@pytest.mark.parametrize("retry", [False, True])
def test_custom_agent_configuration_is_automatic_for_concurrency_and_retry(tmp_path, task, retry):
    executions = []

    class ConfiguredAgent(GoodAgent):
        def prepare(self, *, runtime, instance, output, cwd: str):
            self.options = self.config["options"]
            self.instance_id = instance.id
            executions.append(self)

        def run(self, goal, *, runtime, instance, output, cwd: str):
            self.options["tools"].append("execution-only")
            if retry and len(executions) == 1:
                raise ConnectionResetError("injected interruption")
            return super().run(goal=goal, runtime=runtime, instance=instance, output=output, cwd=cwd)

    original = {"tools": ["read"], "temperature": 0.7}
    agent = ConfiguredAgent(model="custom", endpoint="https://model.invalid", options=original,
                            api_key="custom-secret", cache=tmp_path / "cache")
    original["tools"].append("caller-only")
    output = tmp_path / "batch"
    results = eval(tasks=task if retry else [task, task], agent=agent,
                   config=config(max_retries=1, retry_delay=0.001), output_dir=output, concurrency=2)
    assert all(result["score_valid"] for result in results)
    assert len(executions) == 2
    assert executions[0] is not executions[1]
    assert executions[0].instance_id != executions[1].instance_id
    assert agent.config["options"]["tools"] == ["read"]
    assert all(execution.config == agent.config for execution in executions)
    assert all(execution.options["tools"] == ["read", "execution-only"] for execution in executions)
    assert not hasattr(agent, "options")
    records = read(output / "manifest.json")["tasks"]
    for entry in records.values():
        for attempt in entry["attempts"]:
            saved = attempt["configuration"]["values"]["agent"]["config"]
            assert saved["endpoint"] == "https://model.invalid"
            assert saved["options"] == {"tools": ["read"], "temperature": 0.7}
            assert saved["api_key"] == "[REDACTED]"
            assert saved["cache"] == str(tmp_path / "cache")
    assert "custom-secret" not in (output / "manifest.json").read_text()


@pytest.mark.parametrize("failure", [
    AgentError(ErrorType.PROVIDER_TRANSIENT, "service unavailable"),
    ConnectionResetError("connection reset"),
    TimeoutError("transfer timed out"),
])
@pytest.mark.parametrize("provide_config", [False, True])
def test_transient_errors_do_not_restart_task_by_default(tmp_path, task, failure, provide_config):
    class FailingAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            raise failure

    task.verifier.required_env = []
    output = tmp_path / "batch"
    kwargs = {"config": config()} if provide_config else {}
    (result,) = eval(tasks=task, agent=FailingAgent(), output_dir=output, **kwargs)
    assert can_retry(result["errors"][-1])
    assert not any(error["stage"] == "cleanup" for error in result["errors"])
    assert len(task.verifier.runtime.starts) == 2
    entry = read(output / "manifest.json")["tasks"]["0"]
    assert len(entry["attempts"]) == 1
    assert entry["complete"]
    assert entry["attempts"][0]["configuration"]["values"]["eval"]["max_retries"] == 0


def test_transient_retry_uses_fresh_sandboxes_and_keeps_attempts(tmp_path, task):
    agents = []

    class FlakyAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            if len(agents) < 3:
                raise AgentError(ErrorType.PROVIDER_TRANSIENT, "stream interrupted")
            return super().run(goal=goal, runtime=runtime, instance=instance, output=output, cwd=cwd)

        def new(self):
            agent = super().new()
            agents.append(agent)
            return agent

    output = tmp_path / "batch"
    (result,) = eval(tasks=task, agent=FlakyAgent(), config=config(max_retries=2, retry_delay=0.001), output_dir=output)
    assert result["score_valid"]
    assert len(agents) == 3
    assert len(task.verifier.runtime.starts) == 6
    assert not task.verifier.runtime.active
    entry = read(output / "manifest.json")["tasks"]["0"]
    assert entry["complete"]
    assert len(entry["attempts"]) == 3
    assert read(output / "000001/result.json")["errors"][-1]["type"] == "provider_transient"
    assert read(output / "000001/attempt-000003/result.json") == result
    assert read(output / "results.json") == [result]


@pytest.mark.parametrize("kinds,attempts,complete", [
    ([ErrorType.PROVIDER_TRANSIENT, ErrorType.CONFIGURATION], 1, True),
    ([ErrorType.CONFIGURATION, ErrorType.PROVIDER_TRANSIENT], 1, True),
    ([ErrorType.CANCELLED, ErrorType.TRANSPORT], 1, False),
    ([ErrorType.TRANSPORT, ErrorType.CANCELLED], 1, False),
    ([ErrorType.TRANSPORT, ErrorType.PROVIDER_TRANSIENT], 2, True),
    ([], 1, True),
])
def test_retry_checks_all_errors_regardless_of_order(tmp_path, task, monkeypatch, kinds, attempts, complete):
    from tests.evaluation.test_eval import evaluation

    original = evaluation._evaluate
    calls = 0

    def evaluate(*args):
        nonlocal calls
        calls += 1
        if calls > 1:
            return original(*args)
        result = evaluation._result(task)
        result["errors"] = [
            evaluation._error_record("agent", AgentError(kind, kind.value)) for kind in kinds
        ]
        return result

    monkeypatch.setattr(evaluation, "_evaluate", evaluate)
    output = tmp_path / "batch"
    eval(tasks=task, agent=GoodAgent(), config=config(max_retries=2, retry_delay=0.001), output_dir=output)
    entry = read(output / "manifest.json")["tasks"]["0"]
    assert calls == len(entry["attempts"]) == attempts
    assert entry["complete"] is complete


@pytest.mark.parametrize("failure", [
    AgentError(ErrorType.AUTHENTICATION, "401"),
    AgentError(ErrorType.CONFIGURATION, "unknown model"),
    RuntimeError("unknown failure"),
    AgentError(ErrorType.TOKEN_LIMIT),
])
def test_permanent_errors_and_limits_are_not_retried(tmp_path, task, failure):
    class FailingAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            raise failure

    (result,) = eval(tasks=task, agent=FailingAgent(), config=config(max_retries=2), output_dir=tmp_path / "batch")
    assert len(task.verifier.runtime.starts) == 2
    assert result["score_valid"] == (isinstance(failure, AgentError) and failure.type.is_limit)
    assert not can_retry(result["errors"][-1])


def test_retry_exhaustion_keeps_invalid_score_and_history(tmp_path, task):
    class FailingAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            raise ConnectionResetError("connection reset")

    output = tmp_path / "batch"
    (result,) = eval(tasks=task, agent=FailingAgent(), config=config(max_retries=2, retry_delay=0.001), output_dir=output)
    assert not result["score_valid"]
    assert result["verification_result"] is None
    assert len(read(output / "manifest.json")["tasks"]["0"]["attempts"]) == 3
    assert len(task.verifier.runtime.starts) == 6


@pytest.mark.parametrize("cancel_verify", [False, True])
@pytest.mark.parametrize("fail_cleanup", [False, True])
def test_multiple_errors_preserve_order_and_resume_state(
    tmp_path, task, cancel_verify, fail_cleanup,
):
    class LimitedAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            raise AgentError(ErrorType.CONTEXT_LIMIT)

    def verify(**kwargs):
        if cancel_verify:
            raise InterruptedError("verification cancelled")
        raise OSError("verification failed")

    task.verifier.run = verify
    task.verifier.runtime.fail_close = fail_cleanup
    output = tmp_path / "batch"
    kwargs = dict(tasks=task, agent=LimitedAgent(), config=config(max_retries=2), output_dir=output)
    (result,) = eval(**kwargs)
    errors = result["errors"]
    assert [error["stage"] for error in errors] == ["agent", "verify"] + ["cleanup"] * (2 if fail_cleanup else 0)
    assert [error["type"] for error in errors] == [
        "context_limit", "cancelled" if cancel_verify else "verifier",
    ] + ["unknown"] * (2 if fail_cleanup else 0)
    assert "error" not in result
    assert all(set(error) == {"stage", "type", "message"} for error in errors)
    assert not any(can_retry(error) for error in errors)
    assert sum(error["stage"] == "cleanup" for error in result["errors"]) == (2 if fail_cleanup else 0)
    assert not result["score_valid"]
    assert len(task.verifier.runtime.starts) == 2  # Neither record permits an automatic retry.
    assert not task.verifier.runtime.active
    assert read(output / "000001/result.json") == result
    assert read(output / "results.json") == [result]
    entry = read(output / "manifest.json")["tasks"]["0"]
    assert entry["result"] == result
    assert entry["complete"] is not cancel_verify
    assert len(entry["attempts"]) == 1
    assert entry["attempts"][0]["result"] == result

    if cancel_verify:
        task.verifier.run = lambda **kwargs: {"score": 1}
        task.verifier.runtime.fail_close = False
        (resumed,) = eval(**{**kwargs, "agent": GoodAgent()}, resume=True)
        assert resumed["errors"] == []
        assert resumed["score_valid"]
        assert read(output / "manifest.json")["tasks"]["0"]["attempts"][0] == entry["attempts"][0]
    else:
        assert eval(**kwargs, resume=True) == [result]
        assert len(task.verifier.runtime.starts) == 2


@pytest.mark.parametrize("remote", [False, True])
def test_resume_skips_completed_tasks_locally_and_over_ssh(tmp_path, task, ssh_files, remote):
    output = tmp_path / "batch"
    target = address(output) if remote else output
    kwargs = dict(tasks=[task, task], agent=GoodAgent(), config=config(), output_dir=target)
    first = eval(**kwargs, concurrency=2)
    starts = len(task.verifier.runtime.starts)
    assert eval(**kwargs, resume=True) == first
    assert len(task.verifier.runtime.starts) == starts
    assert read(output / "results.json") == first


def test_resume_interrupted_attempt_preserves_artifacts(tmp_path, task):
    interrupted = True

    class InterruptibleAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            if interrupted:
                output.record("checkpoint", {"text": "partial"})
                raise KeyboardInterrupt()
            return super().run(goal=goal, runtime=runtime, instance=instance, output=output, cwd=cwd)

    output = tmp_path / "batch"
    kwargs = dict(tasks=task, agent=InterruptibleAgent(), config=config(), output_dir=output)
    with pytest.raises(KeyboardInterrupt):
        eval(**kwargs)
    original = (output / "000001/agent/logs/events.jsonl").read_bytes()
    interrupted = False
    (result,) = eval(**kwargs, resume=True)
    assert result["score_valid"]
    assert (output / "000001/agent/logs/events.jsonl").read_bytes() == original
    assert (output / "000001/attempt-000002/agent/result.json").is_file()


@pytest.mark.parametrize("change", ["task", "input", "verifier", "count"])
def test_resume_rejects_changed_inputs(tmp_path, task, change):
    output = tmp_path / "batch"
    kwargs = dict(tasks=task, agent=GoodAgent(), config=config(), output_dir=output)
    eval(**kwargs)
    if change == "task":
        kwargs["tasks"] = replace(task, goal=[{"type": "text", "text": "different"}])
    elif change == "input":
        from pathlib import Path
        Path(task.context[0]["source"]).write_text("changed")
    elif change == "verifier":
        task.verifier.entry.write_text('print(\'{"score": 1}\')')
    else:
        kwargs["tasks"] = []
    with pytest.raises(ValueError, match="changed|differs"):
        eval(**kwargs, resume=True)
    assert len(task.verifier.runtime.starts) == 2


@pytest.mark.parametrize("case", ["goal", "image", "context", "setup", "mini_version"])
def test_preflight_rejects_inputs_before_starting_sandboxes(tmp_path, task, case):
    agent = GoodAgent()
    if case == "goal":
        task = replace(task, goal=[])
    elif case == "image":
        task = replace(task, goal=[{"type": "image", "path": "missing.png"}])
        agent = MiniSweAgent()
    elif case == "context":
        task = replace(task, context=[{"source": "missing", "target": "/input"}])
    elif case == "setup":
        task = replace(task, setup={"command": "true", "timeout": -1})
    else:
        agent = MiniSweAgent(version="1.0")
    (result,) = eval(tasks=task, agent=agent, config=config(), output_dir=tmp_path / "batch")
    assert result["errors"][-1]["stage"] == "preflight"
    assert not task.verifier.runtime.starts
    assert not result["score_valid"]


def test_manifest_never_stores_agent_or_verifier_secrets(tmp_path, task):
    output = tmp_path / "batch"
    # Stop before preparation so no external CLI is installed.
    task = replace(task, goal=[])
    eval(tasks=task, agent=CodexAgent(env={"OPENAI_API_KEY": "private-agent-key"}),
         config=config(), output_dir=output)
    assert "private-agent-key" not in (output / "manifest.json").read_text()
    settings = read(output / "manifest.json")["tasks"]["0"]["attempts"][0]["configuration"]["values"]
    assert settings["eval"]["verifier_env"]["REQUIRED"] == "[REDACTED]"
    assert settings["agent"]["config"]["env"]["OPENAI_API_KEY"] == "[REDACTED]"


@pytest.mark.parametrize("score", [0, 0.8, 1])
@pytest.mark.parametrize("limited", [False, True])
def test_cleanup_failure_preserves_verified_score(tmp_path, task, score, limited):
    class ScoredAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            if limited:
                raise AgentError(ErrorType.STEP_LIMIT)
            return super().run(goal=goal, runtime=runtime, instance=instance, output=output, cwd=cwd)

    task.verifier.runtime.fail_close = True
    task.verifier.run = lambda **kwargs: {"score": score}
    output = tmp_path / "batch"
    kwargs = dict(tasks=task, agent=ScoredAgent(), config=config(), output_dir=output)
    (result,) = eval(**kwargs)
    assert result["verification_result"]["score"] == score
    assert result["score_valid"]
    assert sum(error["stage"] == "cleanup" for error in result["errors"]) == 2
    assert len(task.verifier.runtime.starts) == 2
    assert read(output / "000001/result.json") == result
    assert read(output / "results.json") == [result]
    assert read(output / "manifest.json")["tasks"]["0"]["result"] == result
    assert eval(**kwargs, resume=True) == [result]


def test_cleanup_failure_still_prevents_retry_after_execution_failure(tmp_path, task):
    class FailingAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            raise ConnectionResetError("connection reset")

    task.verifier.runtime.fail_close = True
    (result,) = eval(tasks=task, agent=FailingAgent(), config=config(max_retries=2), output_dir=tmp_path / "batch")
    assert can_retry(result["errors"][0])
    assert all(not can_retry(error) for error in result["errors"][1:])
    assert any(error["stage"] == "cleanup" for error in result["errors"])
    assert result["verification_result"] is None
    assert not result["score_valid"]
    assert len(task.verifier.runtime.starts) == 2


def test_creation_failure_during_retry_does_not_abort_batch(tmp_path, task):
    calls = 0

    class FlakyAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            if calls == 1:
                raise ConnectionResetError("connection reset")
            return super().run(goal=goal, runtime=runtime, instance=instance, output=output, cwd=cwd)

        def new(self):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("creation unavailable")
            return super().new()

    results = eval(tasks=[task, task], agent=FlakyAgent(), config=config(max_retries=2, retry_delay=0.001),
                   output_dir=tmp_path / "batch")
    assert results[0]["errors"][-1]["message"] == "creation unavailable"
    assert results[1]["score_valid"]
    assert not task.verifier.runtime.active


def test_preflight_rejects_cli_output_override(tmp_path, task):
    (result,) = eval(
        tasks=task, agent=CodexAgent(extra_args=["--output-last-message", "/somewhere"]),
        config=config(), output_dir=tmp_path / "batch",
    )
    assert result["errors"][-1]["type"] == "configuration"
    assert not task.verifier.runtime.starts


def test_resume_does_not_create_agents_for_completed_tasks(tmp_path, task):
    broken = False

    class CreationAgent(GoodAgent):
        def new(self):
            if broken:
                raise RuntimeError("cannot initialize")
            return super().new()

    output = tmp_path / "batch"
    kwargs = dict(tasks=task, agent=CreationAgent(), config=config(), output_dir=output)
    results = eval(**kwargs)
    manifest = read(output / "manifest.json")
    broken = True
    assert eval(**kwargs, resume=True) == results
    assert read(output / "manifest.json") == manifest
    assert read(output / "results.json") == results


@pytest.mark.parametrize("values", [
    {"max_retries": -1}, {"max_retries": True}, {"max_retries": 1.5},
    {"retry_delay": 0}, {"retry_delay": float("inf")},
])
def test_retry_config_validation(values):
    with pytest.raises(ValueError):
        config(**values)


def test_result_write_failure_preserves_verified_score(tmp_path, task, monkeypatch):
    from tests.evaluation.test_eval import evaluation

    original = evaluation.write_json
    failed = False

    def write(path, value, **kwargs):
        nonlocal failed
        if path.parent.name == "000001" and path.name == "result.json" and not failed:
            failed = True
            raise OSError("storage temporarily unavailable")
        return original(path, value, **kwargs)

    monkeypatch.setattr(evaluation, "write_json", write)
    (result,) = eval(tasks=task, agent=GoodAgent(), config=config(), output_dir=tmp_path / "batch")
    assert result["verification_result"]["score"] == 0
    assert result["score_valid"]
    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["stage"] == "cleanup" and error["message"].startswith("save_result:")
    assert error["type"] == "unknown" and not can_retry(error)
    assert read(tmp_path / "batch/000001/result.json") == result


def test_interrupt_during_backoff_leaves_task_resumable(tmp_path, task, monkeypatch):
    from envweaver.evaluation.panel import _Panel

    original = _Panel.update
    interrupted = False

    def update(self, key, name, stage, result=None, **kwargs):
        nonlocal interrupted
        if stage == "retry" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt()
        return original(self, key, name, stage, result, **kwargs)

    class FlakyAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            if not interrupted:
                raise ConnectionResetError("connection reset")
            return super().run(goal=goal, runtime=runtime, instance=instance, output=output, cwd=cwd)

    monkeypatch.setattr(_Panel, "update", update)
    output = tmp_path / "batch"
    kwargs = dict(tasks=task, agent=FlakyAgent(), config=config(max_retries=2), output_dir=output)
    with pytest.raises(KeyboardInterrupt):
        eval(**kwargs)
    assert not read(output / "manifest.json")["tasks"]["0"]["complete"]
    (result,) = eval(**kwargs, resume=True)
    assert result["score_valid"]


@pytest.mark.parametrize("remote", [False, True])
def test_resume_changed_configuration_preserves_completed_and_attempt_history(tmp_path, task, ssh_files, remote):
    class PartialAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            if goal[0]["text"] == "pending":
                raise InterruptedError("interrupted")
            return super().run(goal=goal, runtime=runtime, instance=instance, output=output, cwd=cwd)

    tasks = [task, replace(task, name="pending", goal=[{"type": "text", "text": "pending"}])]
    output = tmp_path / "batch"
    target = address(output) if remote else output
    initial = eval(tasks=tasks, agent=PartialAgent(model="old-model"), config=config(max_retries=0),
                   output_dir=target)
    old = read(output / "manifest.json")
    original_result = (output / "000001/result.json").read_bytes()
    assert old["tasks"]["0"]["complete"] and not old["tasks"]["1"]["complete"]
    calls = 0

    class ReplacementAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            nonlocal calls
            calls += 1
            assert goal[0]["text"] == "pending"
            if calls == 1:
                raise ConnectionResetError("retry with current configuration")
            return super().run(goal=goal, runtime=runtime, instance=instance, output=output, cwd=cwd)

    current = config(max_retries=1, retry_delay=0.001, verify_timeout=10,
                     agent_timeout=15, task_runtime_config={"memory": "2g"})
    results = eval(tasks=tasks, agent=ReplacementAgent(model="new-model"), config=current,
                   output_dir=target, resume=True, concurrency=2)
    assert results[0] == initial[0]
    assert (output / "000001/result.json").read_bytes() == original_result
    assert results[1]["score_valid"]
    assert results[1]["agent_result"]["metadata"]["model"] == "new-model"
    assert calls == 2
    assert len(task.verifier.runtime.starts) == 8
    assert task.verifier.runtime.starts[4]["ref"].config["memory"] == "2g"
    assert task.verifier.calls[-1]["timeout"] == 10
    saved = read(output / "manifest.json")["tasks"]
    assert saved["0"] == old["tasks"]["0"]
    attempts = saved["1"]["attempts"]
    assert len(attempts) == 3
    assert attempts[0] == old["tasks"]["1"]["attempts"][0]
    first, second, third = [item["configuration"] for item in attempts]
    assert first["values"]["agent"]["config"]["model"] == "old-model"
    assert first["values"]["eval"]["max_retries"] == 0
    assert second == third and first["fingerprint"] != second["fingerprint"]
    assert second["values"]["agent"]["config"]["model"] == "new-model"
    assert second["values"]["eval"]["agent_timeout"] == 15
    assert second["values"]["eval"]["max_retries"] == 1
    assert not task.verifier.runtime.active


def test_resume_all_completed_ignores_new_agent_configuration(tmp_path, task):
    output = tmp_path / "batch"
    results = eval(tasks=task, agent=GoodAgent(), config=config(), output_dir=output)
    manifest = read(output / "manifest.json")

    class UnusedAgent(GoodAgent):
        def new(self):
            pytest.fail("completed tasks must not create an Agent")

        def _identity(self):
            pytest.fail("completed tasks must not inspect Agent configuration")

    assert eval(tasks=task, agent=UnusedAgent(model="new"), config=config(agent_timeout=12),
                output_dir=output, resume=True) == results
    assert read(output / "manifest.json") == manifest
    assert len(task.verifier.runtime.starts) == 2


def test_resume_creation_failure_records_current_config_and_remains_resumable(tmp_path, task):
    output = tmp_path / "batch"

    class UnavailableAgent(GoodAgent):
        def new(self):
            raise RuntimeError("unavailable")

    for model in ("first", "second"):
        result, = eval(tasks=task, agent=UnavailableAgent(model=model), config=config(),
                       output_dir=output, resume=model == "second")
        assert result["errors"][-1]["stage"] == "preflight"
    record = read(output / "manifest.json")["tasks"]["0"]
    assert not record["complete"]
    assert [attempt["configuration"]["values"]["agent"]["config"]["model"]
            for attempt in record["attempts"]] == ["first", "second"]
    result, = eval(tasks=task, agent=GoodAgent(), config=config(), output_dir=output, resume=True)
    assert result["score_valid"]
    assert len(task.verifier.runtime.starts) == 2


@pytest.mark.parametrize("snapshot", [
    None, {},
    {"values": [], "fingerprint": "a" * 64},
    {"values": {"agent": {}, "eval": {}}, "fingerprint": "invalid"},
    {"values": {"agent": {}}, "fingerprint": "a" * 64},
])
def test_resume_rejects_missing_or_invalid_configuration_snapshot(tmp_path, task, snapshot):
    output = tmp_path / "batch"
    eval(tasks=task, agent=GoodAgent(), config=config(), output_dir=output)
    path = output / "manifest.json"
    saved = read(path)
    attempt = saved["tasks"]["0"]["attempts"][0]
    if snapshot is None:
        del attempt["configuration"]
    else:
        attempt["configuration"] = snapshot
    path.write_text(json.dumps(saved))
    before = path.read_bytes()
    starts = len(task.verifier.runtime.starts)

    with pytest.raises(ValueError, match="Invalid Eval manifest"):
        eval(tasks=task, agent=GoodAgent(), config=config(), output_dir=output, resume=True)

    assert path.read_bytes() == before
    assert len(task.verifier.runtime.starts) == starts
