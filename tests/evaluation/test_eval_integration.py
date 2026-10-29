"""Opt-in Eval tests with real sandboxes and a deterministic Agent."""

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from envweaver import EvalConfig, Runtime, RuntimeRef, Task, Verifier, eval
from envweaver.errors import AgentError, ErrorType
from envweaver.evaluation.failure import can_retry
from tests.agents.helpers import CooperativeAgent

pytestmark = pytest.mark.integration


class SandboxAgent(CooperativeAgent):
    def run(self, goal, *, runtime, instance, output, cwd: str):
        action = goal[0]["text"]
        output.record("message", {"text": action})
        if action == "fail":
            raise OSError("injected agent failure")
        if action == "wait":
            runtime.exec(
                instance=instance,
                command=["python3", "-c", "from pathlib import Path; Path('/tmp/ew-eval/answer').write_text('partial')"],
            )
            self._cancelled.wait(30)
            self._check_stopped()
        code, _, stderr = runtime.exec(
            instance=instance,
            command=["python3", "-c", f"from pathlib import Path; Path('answer').write_text({action!r})"],
            cwd="/tmp/ew-eval",
        )
        assert code == 0, stderr
        return output.finish([{"type": "text", "text": action}])


def test_eval_real_custom_agent_uses_automatic_configuration(sandbox_task, tmp_path):
    executions = []

    class ConfiguredAgent(SandboxAgent):
        def prepare(self, *, runtime, instance, output, cwd: str):
            self.instance_id = instance.id
            executions.append(self)

        def run(self, goal, *, runtime, instance, output, cwd: str):
            if len(executions) == 1:
                raise ConnectionResetError("injected interruption")
            return super().run(runtime=runtime, instance=instance, output=output, cwd=cwd, goal=[{"type": "text", "text": self.config["answer"]}])

    agent = ConfiguredAgent(answer="correct", options={"temperature": 0.7}, api_key="test-secret")
    output = tmp_path / "custom-config"
    kwargs = dict(tasks=sandbox_task, agent=agent,
                  config=EvalConfig(setup_timeout=180, max_retries=1, retry_delay=0.01),
                  output_dir=output)
    (result,) = eval(**kwargs)
    assert result["score_valid"] and result["verification_result"]["score"] == 1
    assert len(executions) == 2 and executions[0].instance_id != executions[1].instance_id
    assert all(execution.config == agent.config for execution in executions)
    manifest = json.loads((output / "manifest.json").read_text())
    for attempt in manifest["tasks"]["0"]["attempts"]:
        saved = attempt["configuration"]["values"]["agent"]["config"]
        assert saved == {"answer": "correct", "options": {"temperature": 0.7}, "api_key": "[REDACTED]"}
    assert eval(**kwargs, resume=True) == [result]
    assert len(executions) == 2


def test_eval_real_retry_and_resume(sandbox_task, tmp_path):
    calls = 0
    instances = []

    class FlakyAgent(SandboxAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            nonlocal calls
            calls += 1
            instances.append(instance.id)
            if calls == 1:
                output.record("error", {"text": "injected stream interruption"})
                raise AgentError(ErrorType.PROVIDER_TRANSIENT, "stream interrupted")
            return super().run(goal=goal, runtime=runtime, instance=instance, output=output, cwd=cwd)

    output = tmp_path / "retry"
    kwargs = dict(
        tasks=sandbox_task, agent=FlakyAgent(),
        config=EvalConfig(setup_timeout=180, max_retries=2, retry_delay=0.01), output_dir=output,
    )
    (result,) = eval(**kwargs)
    assert calls == 2 and len(set(instances)) == 2
    assert result["verification_result"]["score"] == 1
    assert result["score_valid"]
    assert result["output_dir"] == "000001/attempt-000002"
    assert (output / "000001/agent/logs/events.jsonl").exists()
    assert eval(**kwargs, resume=True) == [result]
    assert calls == 2


def test_eval_real_timeout_then_verifier_failure(sandbox_task, tmp_path):
    task = replace(sandbox_task, goal=[{"type": "text", "text": "wait"}])
    task.verifier.entry.write_text("raise RuntimeError('injected verification failure')\n")
    output = tmp_path / "multiple-errors"
    kwargs = dict(
        tasks=task, agent=SandboxAgent(),
        config=EvalConfig(setup_timeout=180, agent_timeout=1), output_dir=output,
    )
    (result,) = eval(**kwargs)
    errors = result["errors"]
    assert [error["stage"] for error in errors] == ["agent", "verify"], result
    assert errors[0]["type"] == "timeout"
    assert errors[1]["type"] == "verifier"
    assert "injected verification failure" in errors[1]["message"]
    assert "error" not in result
    assert all("previous" not in error for error in errors)
    assert not any(error["stage"] == "cleanup" for error in result["errors"])
    assert not result["score_valid"]
    assert result["verification_result"] is None
    assert json.loads((output / "000001/result.json").read_text()) == result
    attempts = json.loads((output / "manifest.json").read_text())["tasks"]["0"]["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["result"] == result
    assert eval(**kwargs, resume=True) == [result]


@pytest.fixture(params=["docker", "qemu"])
def sandbox_task(request, tmp_path):
    if request.param == "docker":
        image = os.environ.get("ENVWEAVER_TEST_EVAL_DOCKER")
        if not image:
            pytest.skip("requires ENVWEAVER_TEST_EVAL_DOCKER with EnvWeaver installed")
        ref = RuntimeRef("docker", image, {"command": ["sleep", "infinity"]})
    else:
        filename = os.environ.get("ENVWEAVER_TEST_QEMU_REF")
        if not filename:
            pytest.skip("requires ENVWEAVER_TEST_QEMU_REF with EnvWeaver installed")
        ref = RuntimeRef.from_dict(json.loads(Path(filename).read_text()))
    runtime = Runtime(root=tmp_path / "records")
    entry = tmp_path / "verifier/run.py"
    entry.parent.mkdir()
    entry.write_text(
        "import argparse,json,tempfile\n"
        "from pathlib import Path\n"
        "from envweaver import Runtime\n"
        "p=argparse.ArgumentParser(); p.add_argument('--instance')\n"
        "r=Runtime(); i=r.connect(path=p.parse_args().instance)\n"
        "with tempfile.TemporaryDirectory() as d:\n"
        " f=Path(d)/'answer'; r.download(instance=i,source='/tmp/ew-eval/answer',target=f)\n"
        " print(json.dumps({'score':float(f.read_text()=='correct'),'details':{'answer':f.read_text()}}))\n"
    )
    context = tmp_path / "input"
    context.write_text("input")
    task = Task(
        name="real-eval", task_ref="integration", metadata={"backend": ref.backend},
        goal=[{"type": "text", "text": "correct"}], task_runtime_ref=ref,
        context=[{"source": str(context), "target": "/tmp/ew-eval/input"}],
        setup={"command": "test -f /tmp/ew-eval/input", "timeout": 10},
        cwd="/tmp/ew-eval", verifier_runtime_ref=ref,
        verifier=Verifier(entry=entry, runtime=runtime, timeout=30),
    )
    yield task
    # Successful cleanup removes every Runtime instance record.
    assert not list((tmp_path / "records/instances").glob("*/instance.json"))


def test_eval_real_concurrency_and_scores(sandbox_task, tmp_path):
    other = replace(sandbox_task, name="incorrect", goal=[{"type": "text", "text": "incorrect"}])
    results = eval(
        tasks=[sandbox_task, other], agent=SandboxAgent(),
        config=EvalConfig(setup_timeout=180), output_dir=tmp_path / "run", concurrency=2,
    )
    assert [r["verification_result"]["score"] for r in results] == [1, 0], results
    assert all(r["errors"] == [] for r in results), results
    assert json.loads((tmp_path / "run/results.json").read_text()) == results
    for index in (1, 2):
        directory = tmp_path / f"run/{index:06d}/agent"
        trajectory = directory / "logs/events.jsonl"
        events = [json.loads(line) for line in trajectory.read_text().splitlines()]
        assert any(event["type"] == "message" for event in events)
        assert [event["seq"] for event in events] == list(range(1, len(events) + 1))


@pytest.mark.parametrize("override", [False, True])
def test_eval_real_verifier_environment(sandbox_task, tmp_path, monkeypatch, override):
    monkeypatch.setenv("EW_VERIFIER_REQUIRED", "from-process")
    monkeypatch.setenv("EW_VERIFIER_EMPTY", "")
    monkeypatch.setenv("EW_VERIFIER_UNRELATED", "not-forwarded")
    sandbox_task.verifier.required_env = ["EW_VERIFIER_REQUIRED", "EW_VERIFIER_EMPTY"]
    env = {"EW_VERIFIER_EXTRA": "explicit-extra"}
    if override:
        env["EW_VERIFIER_REQUIRED"] = "from-config"
    expected = {
        "EW_VERIFIER_REQUIRED": "from-config" if override else "from-process",
        "EW_VERIFIER_EMPTY": "", "EW_VERIFIER_EXTRA": "explicit-extra",
    }
    sandbox_task.verifier.entry.write_text(
        "import json, os\n"
        f"expected = {expected!r}\n"
        "observed = {name: os.environ.get(name) for name in expected}\n"
        "assert 'EW_VERIFIER_UNRELATED' not in os.environ\n"
        "print(json.dumps({'score': float(observed == expected)}))\n"
    )
    (result,) = eval(
        tasks=sandbox_task, agent=SandboxAgent(),
        config=EvalConfig(setup_timeout=180, verifier_env=env), output_dir=tmp_path / "env",
    )
    assert result["errors"] == []
    assert result["verification_result"] == {"score": 1}
    assert result["score_valid"]


def test_eval_real_cleanup_error_preserves_score(sandbox_task, tmp_path, monkeypatch):
    sandbox_task.verifier.entry.write_text('print(\'{"score": 0.8}\')\n')
    runtime = sandbox_task.verifier.runtime
    close = runtime.close

    def close_with_error(*, instance):
        close(instance=instance)
        raise OSError("injected cleanup failure after closing sandbox")

    monkeypatch.setattr(runtime, "close", close_with_error)
    output = tmp_path / "cleanup-score"
    (result,) = eval(
        tasks=sandbox_task, agent=SandboxAgent(),
        config=EvalConfig(setup_timeout=180), output_dir=output,
    )
    assert result["verification_result"] == {"score": 0.8}
    assert result["score_valid"]
    assert [error["stage"] for error in result["errors"]] == ["cleanup", "cleanup"]
    assert all(error["message"].startswith("close:") and not can_retry(error) for error in result["errors"])
    assert json.loads((output / "000001/result.json").read_text()) == result
    assert json.loads((output / "results.json").read_text()) == [result]


def test_eval_repeated_batches(sandbox_task, tmp_path):
    size = int(os.environ.get("ENVWEAVER_TEST_EVAL_BATCH_SIZE", "0"))
    if size < 4:
        pytest.skip("requires ENVWEAVER_TEST_EVAL_BATCH_SIZE >= 4")
    tasks = [
        replace(
            sandbox_task, name=f"task-{index}",
            goal=[{"type": "text", "text": "correct" if index % 2 == 0 else "incorrect"}],
        )
        for index in range(size)
    ]
    for attempt in range(2):
        output = tmp_path / f"batch-{attempt}"
        results = eval(
            tasks=iter(tasks), agent=SandboxAgent(),
            config=EvalConfig(setup_timeout=600, verify_timeout=180),
            output_dir=output, concurrency=3,
        )
        assert [r["name"] for r in results] == [task.name for task in tasks]
        assert all(r["errors"] == [] for r in results), results
        assert [r["verification_result"]["score"] for r in results] == [
            int(index % 2 == 0) for index in range(size)
        ]
        assert json.loads((output / "results.json").read_text()) == results
        assert len(list(output.glob("*/result.json"))) == size
        assert not list((tmp_path / "records/instances").glob("*/instance.json"))


@pytest.mark.parametrize("failure", [
    "setup", "setup_timeout", "setup_command_timeout", "agent", "agent_timeout",
    "verify", "verify_timeout",
])
def test_eval_real_failures_cleanup(sandbox_task, tmp_path, failure):
    task = sandbox_task
    config = EvalConfig(setup_timeout=180)
    if failure == "setup":
        task = replace(task, setup={"command": "exit 7", "timeout": 10})
    elif failure == "setup_timeout":
        task = replace(task, setup=None)
        config.setup_timeout = 0.05
    elif failure == "setup_command_timeout":
        task = replace(task, setup={"command": "sleep 30", "timeout": 0.2})
    elif failure.startswith("agent"):
        task = replace(task, goal=[{"type": "text", "text": "wait" if failure.endswith("timeout") else "fail"}])
        if failure.endswith("timeout"):
            config.agent_timeout = 0.5
    else:
        Path(task.verifier.entry).write_text(
            "import time; time.sleep(30)" if failure.endswith("timeout") else "print('invalid json')"
        )
        if failure.endswith("timeout"):
            task.verifier.timeout = 0.5
    (result,) = eval(tasks=task, agent=SandboxAgent(), config=config, output_dir=tmp_path / "run")
    assert result["errors"][-1]["stage"] == failure.split("_")[0], result
    if failure == "agent_timeout":
        assert result["verification_result"]["score"] == 0
    else:
        assert result["verification_result"] is None
    assert not any(error["stage"] == "cleanup" for error in result["errors"]), result
    if failure.startswith("agent"):
        assert result["agent_result"]["artifacts"]["logs"]


def test_eval_real_resume_with_changed_agent_and_config(sandbox_task, tmp_path):
    class PartialAgent(SandboxAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            if goal[0]["text"] == "incorrect":
                raise InterruptedError("pause this task")
            return super().run(goal=goal, runtime=runtime, instance=instance, output=output, cwd=cwd)

    tasks = [sandbox_task, replace(sandbox_task, name="pending", goal=[{"type": "text", "text": "incorrect"}])]
    output = tmp_path / "changed-config"
    first = eval(tasks=tasks, agent=PartialAgent(model="old"), config=EvalConfig(max_retries=0),
                 output_dir=output, concurrency=2)
    old = json.loads((output / "manifest.json").read_text())
    old_result = (output / "000001/result.json").read_bytes()
    settings = EvalConfig(agent_timeout=20, verify_timeout=40, task_runtime_config={"cpus": 1})
    results = eval(tasks=tasks, agent=SandboxAgent(model="new"), config=settings,
                   output_dir=output, concurrency=2, resume=True)
    assert results[0] == first[0]
    assert (output / "000001/result.json").read_bytes() == old_result
    assert results[1]["score_valid"] and results[1]["verification_result"]["score"] == 0
    assert results[1]["agent_result"]["metadata"]["model"] == "new"
    current = json.loads((output / "manifest.json").read_text())["tasks"]
    assert current["0"] == old["tasks"]["0"]
    assert current["1"]["attempts"][0] == old["tasks"]["1"]["attempts"][0]
    snapshot = current["1"]["attempts"][1]["configuration"]["values"]
    assert snapshot["eval"]["agent_timeout"] == 20
    assert snapshot["eval"]["task_runtime_config"] == {"cpus": 1}
    assert snapshot["agent"]["config"]["model"] == "new"


@pytest.mark.parametrize("directory_source", ["context", "setup", "missing"])
def test_eval_real_required_cwd(sandbox_task, tmp_path, directory_source):
    workdir = "/tmp/ew-required-cwd"
    context = [{**sandbox_task.context[0], "target": workdir + "/input"}] if directory_source == "context" else []
    setup = {"command": ["mkdir", "-p", workdir], "timeout": 10} if directory_source == "setup" else None
    task = replace(sandbox_task, cwd=workdir, context=context, setup=setup)
    task.verifier.entry.write_text('print(\'{"score": 1}\')\n')

    class WorkingDirectoryAgent(CooperativeAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            code, stdout, stderr = runtime.exec(
                instance=instance, command=["python3", "-c", "import os; print(os.getcwd())"],
                cwd=cwd,
            )
            assert code == 0, stderr
            assert stdout.strip() == workdir
            return output.finish([{"type": "text", "text": stdout.strip()}])

    (result,) = eval(
        tasks=task, agent=WorkingDirectoryAgent(), config=EvalConfig(setup_timeout=180),
        output_dir=tmp_path / "required-cwd",
    )
    if directory_source == "missing":
        assert result["errors"][0]["stage"] == "setup"
        assert "Cannot enter Agent working directory" in result["errors"][0]["message"]
        assert result["agent_stop_reason"] is None
        assert result["verification_result"] is None
    else:
        assert result["errors"] == []
        assert result["agent_result"]["output"] == [{"type": "text", "text": workdir}]
        assert result["verification_result"] == {"score": 1}
