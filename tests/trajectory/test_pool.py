import base64
import json
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

from envweaver import AgentOutput, TrajPool
from envweaver._util import read_json, write_json
from envweaver.agents.trajectory import add_step, document
from envweaver.trajectory.pool import completed


def rollout(root, *, reason="completed", score=0, errors=None, task_ref="/tasks/example/task.json"):
    output = AgentOutput(
        root / "agent", {"agent": "fixture", "agent_version": "1", "model": "test-model"}
    )
    context = output.save_file(b'{"messages":[]}', ".json")
    image = output.save_file(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aT1sAAAAASUVORK5CYII="
        ),
        ".png",
    )
    value = document(output.result["metadata"], uuid.uuid4().hex)
    add_step(
        value,
        "user",
        [
            {"type": "text", "text": "solve"},
            {"type": "image", "source": {"media_type": "image/png", "path": image}},
        ],
    )
    add_step(value, "agent", "done", extra={"context": {"path": context, "complete": True}})
    child = document(output.result["metadata"], uuid.uuid4().hex)
    add_step(child, "user", "child")
    value["continued_trajectory_ref"] = "trajectory.child.json"
    output.save_trajectory(trajectory=value, related={"trajectory.child.json": child})
    output.record("message", {"text": "raw"})
    result = dict(
        id=uuid.uuid4().hex,
        name="example",
        task_ref=task_ref,
        metadata={"tags": ["seed"]},
        agent_result=output.finish([]),
        agent_stop_reason=reason,
        errors=errors or [],
        verification_result={"score": score} if score is not None else None,
        score_valid=score is not None,
        started_at="2026-10-29T00:00:00+00:00",
        ended_at="2026-10-29T00:00:01+00:00",
        duration_seconds=1,
    )
    write_json(root / "result.json", result)
    return result


@pytest.mark.parametrize(
    "reason,error,score,expected",
    [
        ("completed", None, 0, True),
        ("completed", None, 1, True),
        ("completed", None, 0.5, True),
        *[
            (reason, {"stage": "agent", "type": reason, "message": "limit"}, 0, True)
            for reason in (
                "timeout",
                "context_limit",
                "step_limit",
                "token_limit",
                "cost_limit",
                "other_limit",
            )
        ],
        (
            "error",
            {"stage": "agent", "type": "provider_transient", "message": "network"},
            None,
            False,
        ),
        ("completed", {"stage": "verify", "type": "verifier", "message": "bad"}, None, False),
        ("cancelled", None, 1, False),
        ("completed", {"stage": "cleanup", "type": "unknown", "message": "stop"}, 1, False),
        ("completed", None, None, False),
        ("completed", None, True, False),
        ("completed", None, 2, False),
    ],
)
def test_completion_policy(tmp_path, reason, error, score, expected):
    source = tmp_path / "source"
    result = rollout(source, reason=reason, score=score, errors=[error] if error else [])
    pool = TrajPool(tmp_path / "pool")
    assert completed(result) is expected
    assert (pool.save(output_dir=source) is not None) is expected
    assert len(sorted(str(path) for path in pool.root.glob("*/result.json"))) == int(expected)
    other = TrajPool(tmp_path / "all", completed_only=False)
    assert other.save(output_dir=source)


def test_concurrent_saves_of_same_rollout_publish_one_record(tmp_path):
    source = tmp_path / "source"
    rollout(source)
    pool = TrajPool(tmp_path / "pool")
    with ThreadPoolExecutor(max_workers=8) as executor:
        refs = list(executor.map(lambda _: pool.save(output_dir=source), range(16)))
    assert len(set(refs)) == 1
    assert sorted(str(path) for path in pool.root.glob("*/result.json")) == [refs[0]]
    assert not list(pool.root.glob(".archive-*"))


def test_archive_stands_alone_and_is_idempotent(tmp_path):
    source = tmp_path / "source"
    result = rollout(source)
    (source / "attempt-000002").mkdir()
    (source / "attempt-000002/result.json").write_text("must not archive")
    pool = TrajPool(tmp_path / "pool")
    ref = pool.save(output_dir=source)
    stored = read_json(ref)
    stored["metadata"]["tags"] = ["reviewed"]
    write_json(Path(ref), stored)
    assert pool.save(output_dir=source) == ref
    assert read_json(ref)["metadata"]["tags"] == ["reviewed"]
    assert not (Path(ref).parent / "attempt-000002").exists()
    shutil.rmtree(source)
    agent = Path(ref).parent / "agent"
    for path in result["agent_result"]["artifacts"]["files"]:
        assert (agent / path).is_file()
    assert (
        json.loads((agent / "trajectory.json").read_text())["continued_trajectory_ref"]
        == "trajectory.child.json"
    )


def test_concurrent_independent_rollouts(tmp_path):
    sources = [tmp_path / f"source-{i}" for i in range(24)]
    for source in sources:
        rollout(source)
    pool = TrajPool(tmp_path / "pool")
    with ThreadPoolExecutor(max_workers=8) as executor:
        refs = list(executor.map(lambda source: pool.save(output_dir=source), sources))
    assert (
        len(set(refs)) == len(sorted(str(path) for path in pool.root.glob("*/result.json"))) == 24
    )
    assert not list((tmp_path / "pool").glob(".publish-*"))


def test_failure_with_only_logs_is_retained(tmp_path):
    source = tmp_path / "source"
    result = rollout(source, reason="error", score=None)
    result["agent_result"] = None
    write_json(source / "result.json", result)
    pool = TrajPool(tmp_path / "pool", completed_only=False)
    ref = pool.save(output_dir=source)
    assert (Path(ref).parent / "agent/logs/events.jsonl").exists()


def test_invalid_artifact_does_not_publish(tmp_path):
    source = tmp_path / "source"
    rollout(source)
    (source / "agent/escape").symlink_to(tmp_path)
    pool = TrajPool(tmp_path / "pool")
    with pytest.raises(ValueError, match="regular"):
        pool.save(output_dir=source)
    assert sorted(str(path) for path in pool.root.glob("*/result.json")) == []


def test_missing_artifact_does_not_publish(tmp_path):
    source = tmp_path / "source"
    result = rollout(source)
    path = result["agent_result"]["artifacts"]["files"][0]
    (source / "agent" / path).unlink()
    pool = TrajPool(tmp_path / "pool")
    with pytest.raises(FileNotFoundError):
        pool.save(output_dir=source)
    assert sorted(str(path) for path in pool.root.glob("*/result.json")) == []


def test_missing_atif_is_not_complete(tmp_path):
    result = rollout(tmp_path / "source")
    value = deepcopy(result)
    value["agent_result"]["artifacts"]["trajectory"] = None
    assert not completed(value)


def test_public_arguments(tmp_path):
    with pytest.raises(TypeError):
        TrajPool(tmp_path, completed_only=1)
    with pytest.raises(ValueError):
        TrajPool("")


def test_missing_unlisted_context_is_rejected(tmp_path):
    source = tmp_path / "source"
    rollout(source)
    path = source / "agent/trajectory.json"
    value = json.loads(path.read_text())
    value["steps"][1]["extra"]["context"]["path"] = "files/missing.json"
    write_json(path, value)
    pool = TrajPool(tmp_path / "pool")
    with pytest.raises(FileNotFoundError):
        pool.save(output_dir=source)
    assert sorted(str(path) for path in pool.root.glob("*/result.json")) == []
