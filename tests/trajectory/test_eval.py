import importlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from envweaver import TrajPool, eval
from envweaver._util import read_json
from envweaver.errors import AgentError, ErrorType
from envweaver.trajectory.atif import add_step, document
from tests.evaluation.test_eval import GoodAgent, config
from tests.evaluation.test_eval import task as task


class TracedAgent(GoodAgent):
    def run(self, *, output, **kwargs):
        trajectory = document(output.result["metadata"], "session")
        add_step(trajectory, "user", "solve")
        output.save_trajectory(trajectory=trajectory)
        return super().run(output=output, **kwargs)


def test_rollout_metadata_starts_empty_and_is_independent(tmp_path, task):
    task = replace(task, metadata={"tags": ["task-only"], "source": {"pipeline": "test"}})
    pool = TrajPool(tmp_path / "pool")
    results = eval(
        tasks=[task, task],
        agent=TracedAgent(model="model"),
        config=config(),
        output_dir=tmp_path / "run",
        traj_pool=pool,
        concurrency=2,
    )
    for result in results:
        assert result["metadata"] == {}
        assert read_json(result["traj_ref"])["metadata"] == {}
        assert result["agent_result"]["metadata"]["model"] == "model"
    results[0]["metadata"]["tags"] = ["rollout-only"]
    assert results[1]["metadata"] == {}
    assert task.metadata == {"tags": ["task-only"], "source": {"pipeline": "test"}}


def test_eval_pool_resume_and_zero_score(tmp_path, task):
    pool = TrajPool(tmp_path / "pool")
    kwargs = dict(
        tasks=task,
        agent=TracedAgent(model="model"),
        config=config(),
        output_dir=tmp_path / "run",
        traj_pool=pool,
    )
    results = eval(**kwargs)
    ref = results[0]["traj_ref"]
    assert read_json(ref)["verification_result"]["score"] == 0
    assert read_json(ref)["configuration"]["eval"]["verifier_env"]["REQUIRED"] == "[REDACTED]"
    assert eval(**kwargs, resume=True) == results
    assert sorted(str(path) for path in pool.root.glob("*/result.json")) == [ref]
    assert results[0]["duration_seconds"] >= 0
    assert json.loads((tmp_path / "run/000001/result.json").read_text()) == results[0]


def test_retry_records_are_independent(tmp_path, task, monkeypatch):
    calls = []

    class Flaky(TracedAgent):
        def run(self, **kwargs):
            result = super().run(**kwargs)
            calls.append(True)
            if len(calls) == 1:
                raise AgentError(ErrorType.PROVIDER_TRANSIENT, "network")
            return result

    pool = TrajPool(tmp_path / "pool", completed_only=False)
    save = Mock(wraps=pool.save)
    monkeypatch.setattr(pool, "save", save)
    kwargs = dict(
        tasks=task,
        agent=Flaky(),
        config=config(max_retries=1, retry_delay=0.001),
        output_dir=tmp_path / "run",
        traj_pool=pool,
    )
    (result,) = eval(**kwargs)
    records = [
        read_json(ref) for ref in sorted(str(path) for path in pool.root.glob("*/result.json"))
    ]
    first = tmp_path / "run/000001"
    attempts = [call(output_dir=first), call(output_dir=first / "attempt-000002")]
    assert save.call_args_list == attempts
    assert len(records) == 2 and len({r["id"] for r in records}) == 2
    assert {r["agent_stop_reason"] for r in records} == {"error", "completed"}
    assert not any("attempt" in r or "sample_id" in r for r in records)
    assert result["score_valid"]
    assert not (
        Path(sorted(str(path) for path in pool.root.glob("*/result.json"))[0]).parent
        / "attempt-000002"
    ).exists()
    save.reset_mock()
    assert eval(**kwargs, resume=True) == [result]
    assert save.call_args_list == attempts


def test_pool_failure_preserves_eval_and_can_resume(tmp_path, task, monkeypatch):
    pool = TrajPool(tmp_path / "pool")
    module = importlib.import_module("envweaver.trajectory.pool")
    original = module._dispatch

    def fail(root, payload):
        if payload["action"] == "publish_directory":
            raise OSError("injected disk failure")
        return original(root, payload)

    kwargs = dict(
        tasks=task,
        agent=TracedAgent(),
        config=config(),
        output_dir=tmp_path / "run",
        traj_pool=pool,
    )
    monkeypatch.setattr(module, "_dispatch", fail)
    with pytest.raises(OSError, match="disk failure"):
        eval(**kwargs)
    assert json.loads((tmp_path / "run/000001/result.json").read_text())["score_valid"]
    assert sorted(str(path) for path in pool.root.glob("*/result.json")) == []
    monkeypatch.setattr(module, "_dispatch", original)
    (result,) = eval(**kwargs, resume=True)
    assert result["traj_ref"] in sorted(str(path) for path in pool.root.glob("*/result.json"))
    assert len(task.verifier.runtime.starts) == 2
    assert json.loads((tmp_path / "run/000001/result.json").read_text()) == result


def test_limit_is_scored_and_archived(tmp_path, task):
    class Limited(TracedAgent):
        def run(self, **kwargs):
            super().run(**kwargs)
            raise AgentError(ErrorType.STEP_LIMIT, "limit")

    pool = TrajPool(tmp_path / "pool")
    (result,) = eval(
        tasks=task, agent=Limited(), config=config(), output_dir=tmp_path / "run", traj_pool=pool
    )
    assert result["agent_stop_reason"] == "step_limit"
    assert result["traj_ref"] in sorted(str(path) for path in pool.root.glob("*/result.json"))


@pytest.mark.parametrize("failure", ["prepare", "network", "verify", "cleanup"])
@pytest.mark.parametrize("completed_only", [True, False])
def test_incomplete_eval_archive_policy(tmp_path, task, failure, completed_only):
    class Broken(TracedAgent):
        def prepare(self, **kwargs):
            super().prepare(**kwargs)
            if failure == "prepare":
                raise OSError("prepare failed")

        def run(self, **kwargs):
            result = super().run(**kwargs)
            if failure == "network":
                raise AgentError(ErrorType.PROVIDER_TRANSIENT, "network failed")
            return result

    if failure == "verify":

        def fail_verify(**kwargs):
            raise OSError("verification failed")

        task.verifier.run = fail_verify
    task.verifier.runtime.fail_close = failure == "cleanup"
    pool = TrajPool(tmp_path / "pool", completed_only=completed_only)
    (result,) = eval(
        tasks=task,
        agent=Broken(),
        config=config(max_retries=0),
        output_dir=tmp_path / "run",
        traj_pool=pool,
    )
    assert result["errors"]
    assert not task.verifier.runtime.active
    assert len(sorted(str(path) for path in pool.root.glob("*/result.json"))) == (
        0 if completed_only else 1
    )
    assert ("traj_ref" in result) is not completed_only
    if not completed_only:
        assert read_json(result["traj_ref"])["errors"] == result["errors"]


def test_external_timeout_keeps_scored_trajectory(tmp_path, task):
    class Waiting(TracedAgent):
        def run(self, **kwargs):
            super().run(**kwargs)
            self._cancelled.wait(5)
            self._check_stopped()

    pool = TrajPool(tmp_path / "pool")
    (result,) = eval(
        tasks=task,
        agent=Waiting(),
        config=config(agent_timeout=0.05),
        output_dir=tmp_path / "run",
        traj_pool=pool,
    )
    assert result["agent_stop_reason"] == "timeout"
    assert result["verification_result"]["score"] == 0
    assert result["traj_ref"] in sorted(str(path) for path in pool.root.glob("*/result.json"))
    assert not task.verifier.runtime.active


def test_concurrent_eval_archives_each_rollout(tmp_path, task):
    pool = TrajPool(tmp_path / "pool")
    results = eval(
        tasks=[task] * 8,
        agent=TracedAgent(),
        config=config(),
        output_dir=tmp_path / "run",
        traj_pool=pool,
        concurrency=4,
    )
    assert len(sorted(str(path) for path in pool.root.glob("*/result.json"))) == len(results) == 8
    assert len({r["id"] for r in results}) == 8
    assert not task.verifier.runtime.active
    for result in results:
        assert result["traj_ref"] in sorted(str(path) for path in pool.root.glob("*/result.json"))
        assert (
            json.loads((tmp_path / "run" / result["output_dir"] / "result.json").read_text())
            == result
        )


@pytest.mark.parametrize("completed_only", [True, False])
def test_interrupt_preserves_result_and_applies_archive_policy(tmp_path, task, completed_only):
    class Interrupted(TracedAgent):
        def run(self, **kwargs):
            super().run(**kwargs)
            raise KeyboardInterrupt()

    pool = TrajPool(tmp_path / "pool", completed_only=completed_only)
    with pytest.raises(KeyboardInterrupt):
        eval(
            tasks=task,
            agent=Interrupted(),
            config=config(),
            output_dir=tmp_path / "run",
            traj_pool=pool,
        )
    result = json.loads((tmp_path / "run/000001/result.json").read_text())
    assert result["agent_stop_reason"] == "cancelled"
    assert result["errors"][-1]["type"] == "cancelled"
    assert json.loads((tmp_path / "run/results.json").read_text()) == [result]
    assert len(sorted(str(path) for path in pool.root.glob("*/result.json"))) == (
        0 if completed_only else 1
    )
    assert not task.verifier.runtime.active
