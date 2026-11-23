"""Publication boundaries and unchanged Mini snapshot regressions."""

import json
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from envweaver import AgentOutput, MiniSweAgent
from envweaver.trajectory.atif import add_step, document


@pytest.fixture
def output(tmp_path):
    return AgentOutput(tmp_path, {"agent": "test", "agent_version": "1", "model": "test"})


def trajectory(output, identity):
    value = document(output.result["metadata"], identity)
    add_step(value, "user", identity)
    return value


@pytest.mark.parametrize("name", [
    "trajectory.json", "trajectory..json", "../trajectory.child.json",
    "trajectory.folder/child.json", "trajectory.folder\\child.json",
    "trajectory.child\0.json", "child.json", 42,
])
def test_related_invalid_name_preserves_saved_trajectory(output, name):
    root = trajectory(output, "original")
    output.save_trajectory(root)
    before = (output.directory / "trajectory.json").read_bytes()
    result = output.result
    with pytest.raises(ValueError, match="filename"):
        output.save_trajectory(trajectory(output, "replacement"), related={name: root})
    assert (output.directory / "trajectory.json").read_bytes() == before
    assert output.result == result


def test_validate_entire_related_batch_before_writing(output):
    root = trajectory(output, "root")
    child = trajectory(output, "child")
    with pytest.raises(ValueError):
        output.save_trajectory(root, related={
            "trajectory.child.json": child, "trajectory.invalid.json": {},
        })
    assert not list(output.directory.glob("trajectory*.json"))
    output.save_trajectory(root, related={"trajectory.child.json": child})
    output.save_trajectory(root, related={"trajectory.child.json": child})
    assert json.loads((output.directory / "trajectory.child.json").read_text()) == child
    assert output.result["artifacts"]["files"] == ["trajectory.child.json"]


@pytest.fixture
def mini(output):
    agent = MiniSweAgent()
    agent._output = output
    agent._remote = "/unused"
    agent._native_count = 0
    agent._native_snapshot = None
    agent._snapshot = None
    agent._native = SimpleNamespace(files={}, poll=lambda sources: False)
    agent._read_remote = lambda path: b'{"messages": [], "info": {}}'
    return agent


def request(agent, call_id):
    event = {"type": "request", "call_id": call_id,
             "data": {"api": "completion", "request": {"messages": []}}}
    key = "mini/model-calls.jsonl"
    agent._native.files[key] = agent._native.files.get(key, "") + json.dumps(event) + "\n"


@pytest.mark.parametrize("body", [b'{"messages": []}', None, b'{"messages":'])
def test_mini_skips_unchanged_but_keeps_new_calls(mini, output, monkeypatch, body):
    mini._read_remote = lambda path: body
    request(mini, "first")
    mini._poll_native()
    path = output.directory / "trajectory.json"
    before = path.read_bytes()
    save = output.save_trajectory
    with monkeypatch.context() as patch:
        patch.setattr(output, "save_trajectory", lambda *a, **kw: pytest.fail("unchanged save"))
        for _ in range(5):
            mini._poll_native()
    assert path.read_bytes() == before

    request(mini, "second")
    # The native collector may already have consumed this change before a failed save.
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("disk unavailable")
        patch.setattr(output, "save_trajectory", fail)
        with pytest.raises(OSError):
            mini._poll_native()
    assert output.save_trajectory == save
    mini._poll_native()
    assert [step["extra"]["call_id"] for step in json.loads(path.read_text())["steps"]] == [
        "first", "second",
    ]


def test_mini_exports_calls_before_native_snapshot_exists(mini, output):
    mini._read_remote = lambda path: None
    request(mini, "pending")
    mini._poll_native()
    saved = json.loads((output.directory / "trajectory.json").read_text())
    assert saved["steps"][0]["extra"]["response_status"] == "pending"


def test_mini_retries_missing_media_without_new_native_records(mini, output):
    event = {"type": "request", "call_id": "image", "data": {
        "api": "completion", "request": {"messages": [{
            "role": "user", "content": [{
                "type": "image_url", "image_url": {"url": "/later.png"},
            }],
        }]},
    }}
    mini._native.files["mini/model-calls.jsonl"] = json.dumps(event) + "\n"
    image = None
    mini._read_remote = lambda path: image if path == "/later.png" else b'{"messages": []}'
    mini._poll_native()
    path = output.directory / "trajectory.json"
    assert json.loads(path.read_text())["extra"]["media_missing"] == ["image:/later.png"]
    stream = BytesIO()
    Image.new("RGB", (2, 2), "red").save(stream, format="PNG")
    image = stream.getvalue()
    mini._poll_native()
    saved = json.loads(path.read_text())
    assert saved["extra"]["media_missing"] == []
    reference = saved["steps"][0]["message"][0]["source"]["path"]
    assert (output.directory / reference).read_bytes() == image
