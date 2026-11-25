import json
import sys
from threading import Thread

import pytest

from envweaver import AgentOutput, AleClawAgent
from envweaver.agents.adapters.ale_claw.adapter import _failure
from envweaver.agents.adapters.ale_claw.trajectory import export, read_calls
from envweaver.errors import ErrorType


def test_run_exports_atif_before_worker_exits(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from envweaver.agents.adapters.ale_claw import adapter

    output = AgentOutput(tmp_path / "out", AleClawAgent().metadata)
    saved = output.directory / "trajectory.json"
    script = """
import json, sys, time
from pathlib import Path
job = json.load(sys.stdin)
path = Path("openclaw_sessions/main/transcript.jsonl")
path.parent.mkdir(parents=True)
def record(text):
    with path.open("a") as file:
        print(json.dumps({"type": "message", "message": {"role": "assistant",
              "content": [{"type": "text", "text": text}]}}), file=file)
record("first")
deadline = time.monotonic() + 10
while not Path(job["agent"]["snapshot"]).exists():
    if time.monotonic() > deadline:
        raise RuntimeError("No live ATIF")
    time.sleep(0.05)
trace = json.loads(Path(job["agent"]["snapshot"]).read_text())
assert [s["message"] for s in trace["steps"] if s["source"] == "agent"] == ["first"]
record("second")
print(json.dumps({"status": "completed"}))
"""
    (tmp_path / "worker.py").write_text(script)
    monkeypatch.setattr(adapter, "__file__", str(tmp_path / "adapter.py"))
    monkeypatch.setattr(adapter, "desktop", lambda *args: nullcontext("unused"))
    agent = AleClawAgent(options={"snapshot": str(saved)})
    agent._controller = (sys.executable, "test", "test")
    agent._os = "linux"
    result = agent.run(goal=[{"type": "text", "text": "go"}], runtime=None,
                       instance=None, output=output, cwd="/work")
    assert result["output"] == [{"type": "text", "text": "second"}]
    assert [s["message"] for s in json.loads(saved.read_text())["steps"]
            if s["source"] == "agent"] == ["first", "second"]


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, ErrorType.AUTHENTICATION),
        (429, ErrorType.PROVIDER_TRANSIENT),
        (500, ErrorType.PROVIDER_TRANSIENT),
        (400, ErrorType.CONFIGURATION),
    ],
)
def test_ale_provider_stop_reason(status, kind):
    for message in ["APIError", "OtherProviderException"]:
        assert _failure({"error": message, "error_type": "new_kind"}, [{"status": status}]) == kind


def test_unrecognized_native_error_preserves_failure():
    assert _failure({"error_type": "new_kind"}, []) == ErrorType.UNKNOWN
    assert _failure({"error_type": "step_limit"}, []) == ErrorType.STEP_LIMIT


def test_ale_config_and_task_input():
    agent = AleClawAgent(model="openai/test", options={"max_turns": 3}, port=9000)
    assert agent.new().config == agent.config
    agent.preflight([{"type": "text", "text": "read"}])
    with pytest.raises(ValueError, match="text goal"):
        agent.preflight([{"type": "image", "path": "/tmp/test.png"}])
    with pytest.raises(ValueError, match="model"):
        AleClawAgent(options={"model": "duplicate"})
    with pytest.raises(ValueError, match="port"):
        AleClawAgent(port=True)


def test_ale_native_trajectory(tmp_path):
    root = tmp_path / "raw"
    transcript = root / "openclaw_sessions/session/transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    call = {"id": "c1", "type": "function_call", "name": "read", "arguments": '{"path":"a"}'}
    result = {"type": "tool_result", "tool_use_id": "c1", "content": "42"}
    rows = [
        ("assistant", [call]),
        ("tool", [result]),
        ("assistant", [{"type": "text", "text": "DONE"}]),
    ]
    transcript.write_text(
        "".join(
            json.dumps({"type": "message", "message": {"role": r, "content": b}}) + "\n"
            for r, b in rows
        )
    )
    rows = [
        {"id": "failed", "request": {"messages": []}},
        {"id": "failed", "status": 500, "body": "internal error"},
        {"id": "ok", "request": {"model": "test", "messages": []}},
        {
            "id": "ok",
            "status": 200,
            "body": json.dumps(
                {
                    "choices": [{"message": {"tool_calls": [{"id": "c1"}]}}],
                    "usage": {"completion_tokens": 5},
                }
            ),
        },
    ]
    (root / "model.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    agent = AleClawAgent()
    agent._secrets = ()
    output = AgentOutput(tmp_path / "out", agent.metadata)
    calls = read_calls(root, output, agent._clean)
    assert export(root, "read", output, agent._clean, calls) == "DONE"
    doc = json.loads((output.directory / "trajectory.json").read_text())
    call_step, final = doc["steps"][1:]
    assert [c["status"] for c in calls] == [500, 200]
    assert calls[1]["usage"] == {"completion_tokens": 5}
    assert call_step["extra"]["call_id"] == "ok"
    assert call_step["tool_calls"] == [
        {"tool_call_id": "c1", "function_name": "read", "arguments": {"path": "a"}}
    ]
    assert call_step["observation"]["results"][0]["content"][0]["text"] == "42"
    assert json.loads((output.directory / call_step["extra"]["context"]["path"]).read_text()) == {
        "model": "test",
        "messages": [],
    }
    assert not final["extra"]["context"]["complete"]


@pytest.mark.parametrize("unique_ids", [True, False])
def test_identical_replies_require_distinct_response_ids(tmp_path, unique_ids):
    transcript = tmp_path / "openclaw_sessions/main/transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    messages = []
    records = []
    for i in range(4):
        identity = f"r{i}" if unique_ids else "r"
        msg = {
            "role": "assistant",
            "responseId": identity,
            "content": [{"type": "text", "text": "same"}],
        }
        messages.append({"type": "message", "message": msg})
        records.extend(
            [
                {"id": str(i), "request": {"messages": []}},
                {
                    "id": str(i),
                    "status": 200,
                    "body": json.dumps(
                        {"id": identity, "choices": [{"message": {"content": "same"}}]}
                    ),
                },
            ]
        )
    transcript.write_text("".join(json.dumps(row) + "\n" for row in messages))
    (tmp_path / "model.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records))
    output = AgentOutput(tmp_path / "out", AleClawAgent().metadata)

    def clean(value):
        return value

    export(tmp_path, "goal", output, clean, read_calls(tmp_path, output, clean))
    trajectory = json.loads((output.directory / "trajectory.json").read_text())
    links = [s["extra"].get("call_id") for s in trajectory["steps"] if s["source"] == "agent"]
    assert links == (["0", "1", "2", "3"] if unique_ids else [None] * 4)


def test_stop_ale_controller(tmp_path):
    import time

    agent = AleClawAgent()
    errors = []

    def run():
        try:
            agent._execute([sys.executable, "-c", "import time; time.sleep(60)"], tmp_path)
        except InterruptedError as error:
            errors.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while agent._process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert agent._process is not None
    finally:
        agent.stop(timeout=2)
        thread.join(3)
    assert not thread.is_alive() and len(errors) == 1


def test_qemu_forward_targets_the_attempt_guest(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from envweaver.agents.adapters.ale_claw import desktop as module

    commands = []

    @contextmanager
    def fake(command):
        commands.append(command)
        yield "http://127.0.0.1:31001"

    monkeypatch.setattr(module, "forward", fake)
    instance = SimpleNamespace(
        backend="qemu", data={"ssh": {"user": "guest", "host": "127.0.0.1", "port": 30123}}
    )
    with module.desktop(None, instance, 8123) as url:
        assert url == "http://127.0.0.1:31001"
    assert commands[0][-6:] == ["-p", "30123", "-W", "127.0.0.1:8123", "--", "guest@127.0.0.1"]


def test_ale_export_partial_turn_media_and_followup(tmp_path):
    import base64

    transcript = tmp_path / "openclaw_sessions/desktop/transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    rows = [
        {
            "role": "assistant",
            "content": [{"type": "computer_call", "id": "c", "action": {"type": "screenshot"}}],
        },
        {
            "role": "tool",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "c",
                    "content": "unrelated_screenshot_after.png",
                    "images": [
                        {
                            "media_type": "image/png",
                            "data": base64.b64encode(b"exact-image").decode(),
                        }
                    ],
                }
            ],
        },
        {"role": "user", "content": [{"type": "text", "text": "Look at the new screenshot."}]},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "Done"}],
            "stopReason": "end_turn",
        },
    ]
    transcript.write_text(
        "".join(json.dumps({"type": "message", "message": row}) + "\n" for row in rows)
    )
    record = {
        "id": "r",
        "request": {"tools": [{"parameters": {"properties": {"password": {"type": "string"}}}}]},
    }
    response = {
        "id": "r",
        "status": 200,
        "body": json.dumps({"choices": [{"message": {"content": "Done"}}]}),
    }
    (tmp_path / "model.jsonl").write_text(
        json.dumps(record) + "\n" + json.dumps(response) + "\n" + '{"id":'
    )
    agent = AleClawAgent()
    # This substring occurs in the encoded screenshot but must not change its bytes.
    agent._secrets = ["ZXhhY3Q"]
    output = AgentOutput(tmp_path / "out", agent.metadata)
    assert (
        export(tmp_path, "start", output, agent._clean, read_calls(tmp_path, output, agent._clean))
        == "Done"
    )
    doc = json.loads((output.directory / "trajectory.json").read_text())
    assert doc["extra"]["missing"] == []
    (picture,) = doc["steps"][1]["observation"]["results"][0]["content"]
    assert (output.directory / picture["source"]["path"]).read_bytes() == b"exact-image"
    assert doc["steps"][2]["message"] == "Look at the new screenshot."
    extra = doc["steps"][3]["extra"]
    assert extra["stop_reason"] == "end_turn" and extra["context"]["complete"]
    request = json.loads((output.directory / extra["context"]["path"]).read_text())
    assert request == {"tools": [{"parameters": {"properties": {"password": {"type": "string"}}}}]}


def test_ale_cancelled_before_transcript_preserves_wire_image(tmp_path):
    agent = AleClawAgent()
    agent._secrets = ()
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}}
                ],
            }
        ]
    }
    (tmp_path / "model.jsonl").write_text(json.dumps({"id": "pending", "request": payload}) + "\n")
    output = AgentOutput(tmp_path / "out", agent.metadata)
    calls = read_calls(tmp_path, output, agent._clean)
    export(tmp_path, "start", output, agent._clean, calls)
    doc = json.loads((output.directory / "trajectory.json").read_text())
    assert doc["extra"]["missing"] == ["transcript"]
    (call,) = doc["extra"]["model_calls"]
    assert call["context"]["complete"] and "status" not in call
    assert json.loads((output.directory / call["context"]["path"]).read_text()) == payload


@pytest.mark.parametrize("module", ["httpx", "httpx2"])
def test_http_capture_records_physical_attempts(tmp_path, module):
    import asyncio

    client = pytest.importorskip(module)
    from envweaver.agents.adapters.ale_claw.wire import capture

    responses = iter([500, 200])
    received = []

    def respond(request):
        received.append(json.loads(request.content))
        return client.Response(next(responses), json={"id": "reply"})

    path = tmp_path / "http.jsonl"
    endpoint = "http://local.test/v1/chat/completions"
    payload = {"model": "test", "messages": [{"role": "user", "content": "screen"}]}
    original = client.AsyncClient.send

    async def run():
        async with client.AsyncClient(transport=client.MockTransport(respond)) as session:
            with capture(path):
                for _ in range(2):
                    await session.post(endpoint, json=payload, headers={"authorization": "secret"})

    asyncio.run(run())
    assert client.AsyncClient.send is original
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["status"] for row in rows if "status" in row] == [500, 200]
    assert rows[0]["id"] == rows[1]["id"] != rows[2]["id"] == rows[3]["id"]
    assert [row["request"] for row in rows if "request" in row] == received
    assert "secret" not in path.read_text()
