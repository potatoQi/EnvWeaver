"""ATIF invariants and adapter-specific loss/association regressions."""

import base64
import json
from io import BytesIO

import pytest
from PIL import Image

from envweaver import ATIF_VERSION, AgentOutput
from envweaver.agents.adapters.codex.trajectory import convert as codex
from envweaver.agents.adapters.mini_swe.trajectory import convert as mini
from envweaver.agents.adapters.pi.trajectory import convert as pi
from envweaver.agents.adapters.terminus.trajectory import convert as terminus
from envweaver.agents.capture import Media, NativeFiles, json_lines
from envweaver.trajectory.atif import add_step, document, validate_trajectory


@pytest.fixture
def output(tmp_path):
    return AgentOutput(tmp_path, {"agent": "test", "agent_version": "1", "model": "test"})


@pytest.fixture
def png():
    buf = BytesIO()
    Image.new("RGB", (2, 2), "red").save(buf, format="PNG")
    return buf.getvalue()


def test_schema_version_is_independent_and_save_atomic(output):
    doc = document(output.result["metadata"], "run")
    add_step(doc, "user", "hello")
    output.save_trajectory(doc)
    assert output.result["artifacts"]["trajectory"] == "trajectory.json"
    assert (
        json.loads((output.directory / "trajectory.json").read_text())["schema_version"]
        == ATIF_VERSION
    )
    doc["schema_version"] = "ATIF-v99"
    with pytest.raises(ValueError):
        output.save_trajectory(doc)
    assert (
        json.loads((output.directory / "trajectory.json").read_text())["schema_version"]
        == ATIF_VERSION
    )


@pytest.mark.parametrize(
    "step",
    [
        {"step_id": 2, "source": "user", "message": "wrong sequence"},
        {"step_id": 1, "source": "user", "message": "", "metrics": {}},
        {
            "step_id": 1,
            "source": "agent",
            "message": "",
            "llm_call_count": 0,
            "reasoning_content": "invented",
        },
        {"step_id": 1, "source": "user", "message": [{"type": "text"}]},
        {"step_id": 1, "source": "user", "message": [{"type": "image"}]},
        {
            "step_id": 1,
            "source": "user",
            "message": [
                {
                    "type": "image",
                    "text": "invalid",
                    "source": {"path": "a", "media_type": "image/png"},
                }
            ],
        },
        {"step_id": 1, "source": "user", "message": "", "timestamp": "yesterday"},
        {
            "step_id": 1,
            "source": "agent",
            "message": "",
            "observation": {"results": [{"source_call_id": "missing", "content": "x"}]},
        },
        {
            "step_id": 1,
            "source": "agent",
            "message": "",
            "observation": {
                "results": [{"subagent_trajectory_ref": [{"session_id": "not-an-id"}]}]
            },
        },
    ],
)
def test_invalid_atif_is_rejected(output, step):
    doc = document(output.result["metadata"], "run")
    doc["steps"] = [step]
    with pytest.raises(ValueError):
        validate_trajectory(doc)


def test_octet_stream_image_and_content_addressing(output, png):
    media = Media(output)
    first = media.image("data:application/octet-stream;base64," + base64.b64encode(png).decode())
    second = media.image("data:image/png;base64," + base64.b64encode(png).decode())
    assert first == second
    assert (output.directory / first["source"]["path"]).read_bytes() == png
    assert len(output.result["artifacts"]["files"]) == 1


def test_partial_event_is_not_fabricated():
    assert json_lines('{"x":1}\n{"x":') == [{"x": 1}]
    with pytest.raises(ValueError):
        json_lines("{broken}\n")


class CodexTrace:
    def __init__(self, schema=2):
        self.schema = schema
        self.files = {}
        self.events = []

    def event(self, kind, tid="root", **data):
        self.events.append(
            {"schema_version": self.schema, "thread_id": tid, "payload": {"type": kind, **data}}
        )

    def payload(self, data):
        if self.schema == 3:
            index = len(self.files) + 1
            self.files[f"trace/payloads/000001/{index}.json"] = json.dumps(data)
            return {"raw_payload_id": f"raw_payload:{index}", "shard": 1}
        name = f"payloads/{len(self.files)}.json"
        self.files["trace/" + name] = json.dumps(data)
        return {"path": name}

    def call(self, tid, identity, request, response):
        self.event(
            "inference_started",
            tid,
            inference_call_id=identity,
            request_payload=self.payload(request),
        )
        self.event(
            "inference_completed",
            tid,
            inference_call_id=identity,
            response_payload=self.payload(response),
        )

    def finish(self):
        return {
            **self.files,
            "trace/trace.jsonl": "\n".join(json.dumps(e) for e in self.events) + "\n",
        }


@pytest.mark.parametrize("schema", [1, 2, 3])
def test_codex_threads_images_encryption_and_actual_context(output, png, schema):
    trace = CodexTrace(schema)
    trace.event("rollout_started", root_thread_id="root")
    for tid, parent in [("root", None), ("child", "root"), ("grandchild", "child")]:
        metadata = (
            {"session_source": {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}}
            if parent
            else {}
        )
        trace.event("thread_started", tid, thread_id=tid, metadata_payload=trace.payload(metadata))
        request = {
            "model": "test",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": tid}]}],
        }
        if tid == "grandchild":
            request["input"].append({"type": "compaction", "encrypted_content": "opaque"})
        if tid == "root":
            request["input"][0]["content"] += [
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64," + base64.b64encode(png).decode(),
                },
                {"type": "input_text", "text": "after image"},
            ]
        response = {
            "output_items": [
                {
                    "type": "reasoning",
                    "summary": [{"text": "visible"}],
                    "encrypted_content": "opaque",
                },
                {
                    "type": "function_call",
                    "call_id": "same-id",
                    "namespace": "multi_agent_v1",
                    "name": "spawn_agent",
                    "arguments": '{"x":1}',
                },
            ]
        }
        trace.call(tid, tid, request, response)
        child = {"root": "child", "child": "grandchild"}.get(tid)
        trace.event(
            "tool_call_ended",
            tid,
            tool_call_id="same-id",
            result_payload=trace.payload(
                {"response_item": {"output": json.dumps({"agent_id": child}) if child else "done"}}
            ),
        )
    doc = codex(trace.finish(), output)
    validate_trajectory(doc)
    assert len(doc["subagent_trajectories"]) == 1
    child = doc["subagent_trajectories"][0]
    assert len(child["subagent_trajectories"]) == 1
    assert [b["type"] for b in doc["steps"][0]["message"]] == ["text", "image", "text"]
    assert doc["steps"][-1]["tool_calls"][0]["function_name"] == "multi_agent_v1.spawn_agent"
    assert doc["steps"][-1]["observation"]["results"][0]["subagent_trajectory_ref"] == [
        {"trajectory_id": "child"}
    ]
    last = child["subagent_trajectories"][0]["steps"][-1]
    assert last["extra"]["context"]["complete"] is False
    assert "encrypted_compaction" in last["extra"]["context"]["missing"]
    saved = json.loads((output.directory / last["extra"]["context"]["path"]).read_text())
    assert saved["input"][-1]["encrypted_content"] == "opaque"
    assert last["reasoning_content"] == "visible"


@pytest.mark.parametrize("schema", [0, 4])
def test_codex_export_error_preserves_cli_stop_reason(output, schema):
    from types import SimpleNamespace

    from envweaver import CodexAgent, ErrorType

    agent = CodexAgent()
    agent._output = output
    agent._remote = "/sandbox"
    agent._native = SimpleNamespace(
        poll=lambda sources, **kwargs: True,
        files={"codex/trace.jsonl": json.dumps({"schema_version": schema})},
    )
    # Capture can run both before the terminal CLI event and in final cleanup.
    agent._poll_native()
    agent._on_event({"type": "turn.failed", "error": {"message": "Your input exceeds the context window"}})
    agent._poll_native()
    assert agent._run_error.type is ErrorType.CONTEXT_LIMIT
    assert f"schema {schema}" in output.result["metadata"]["trajectory_export_error"]


@pytest.mark.parametrize("schema", [1, 2, 3])
def test_codex_pending_request_survives_interruption(output, schema):
    trace = CodexTrace(schema)
    trace.event("rollout_started", root_thread_id="root")
    trace.event(
        "inference_started",
        inference_call_id="pending",
        request_payload=trace.payload({"input": []}),
    )
    doc = codex(trace.finish(), output)
    validate_trajectory(doc)
    assert doc["steps"][0]["extra"]["response_status"] == "pending"
    assert doc["steps"][0]["message"] == ""


def test_pi_parallel_tools_images_and_compaction(output, png):
    image = {"type": "image", "mimeType": "image/png", "data": base64.b64encode(png).decode()}
    request = {"messages": [{"role": "system", "content": "exact prompt"}], "tools": []}

    def event(kind, data, call="1"):
        return {"type": kind, "call_id": call, "data": data}

    events = [
        event(
            "message",
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "before"},
                    image,
                    {"type": "text", "text": "after"},
                ],
            },
        ),
        event("request", {"payload": request}),
        event(
            "message",
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "visible"},
                    {
                        "type": "toolCall",
                        "id": "one",
                        "name": "read",
                        "arguments": {"path": "image.png"},
                    },
                    {
                        "type": "toolCall",
                        "id": "two",
                        "name": "bash",
                        "arguments": {"command": "false"},
                    },
                ],
                "stopReason": "toolUse",
            },
        ),
        event(
            "message",
            {"role": "toolResult", "toolCallId": "two", "content": "error", "isError": True},
        ),
        event("message", {"role": "toolResult", "toolCallId": "one", "content": [image]}),
        event("compaction", {"summary": "short"}),
        event("request", {"payload": {"messages": [{"role": "user", "content": "short"}]}}, "2"),
    ]
    doc = pi(events, output)
    validate_trajectory(doc)
    step = doc["steps"][1]
    assert [v["source_call_id"] for v in step["observation"]["results"]] == ["two", "one"]
    assert step["observation"]["results"][0]["extra"]["is_error"]
    assert json.loads((output.directory / step["extra"]["context"]["path"]).read_text()) == request
    assert doc["steps"][-1]["extra"]["response_status"] == "pending"


def test_mini_retries_and_final_tool_result(output):
    def event(kind, key, data):
        return {"type": kind, "call_id": key, "data": data}

    request = {
        "api": "completion",
        "request": {"model": "test", "messages": [{"role": "user", "content": "task"}]},
    }
    events = [
        event("request", "failed", request),
        event("error", "failed", {"message": "HTTP 503"}),
        event("request", "ok", request),
        event(
            "response",
            "ok",
            {
                "choices": [
                    {
                        "message": {
                            "content": "done",
                            "tool_calls": [
                                {
                                    "id": "last",
                                    "function": {"name": "finish", "arguments": "invalid JSON"},
                                }
                            ],
                        }
                    }
                ]
            },
        ),
    ]
    native = {
        "messages": [{"role": "tool", "tool_call_id": "last", "content": "submission accepted"}]
    }
    doc = mini(events, output, native=native)
    validate_trajectory(doc)
    agents = [s for s in doc["steps"] if s["source"] == "agent"]
    assert agents[0]["extra"]["response_status"] == "failed"
    assert agents[1]["tool_calls"][0]["extra"]["raw_arguments"] == "invalid JSON"
    assert agents[1]["observation"]["results"][0]["source_call_id"] == "last"


def test_terminus_does_not_guess_ambiguous_context(output):
    native = document(output.result["metadata"], "run")
    add_step(native, "agent", "same")
    events = []
    for key in ["one", "two"]:
        events.extend(
            [
                {
                    "type": "request",
                    "call_id": key,
                    "data": {
                        "api": "acompletion",
                        "request": {"messages": [{"role": "user", "content": key}]},
                    },
                },
                {
                    "type": "response",
                    "call_id": key,
                    "data": {"choices": [{"message": {"content": "same"}}]},
                },
            ]
        )
    doc = terminus({"trajectory.json": native}, events, output)["trajectory.json"]
    validate_trajectory(doc)
    assert not doc["steps"][0]["extra"]["context"]["complete"]
    assert len(doc["extra"]["model_calls"]) == 2


def test_native_files_partial_snapshot_and_escaped_secret(output, tmp_path):
    import sys

    from tests.agents.test_cli_agents import LocalTransport

    folder = tmp_path / "source"
    folder.mkdir()
    path = folder / "snapshot.json"
    path.write_text("{")
    capture = NativeFiles(
        runtime=LocalTransport(), instance=None, python=sys.executable,
        output=output, redact=lambda text: text.replace('secret"\nvalue', "MASKED"),
    )
    assert not capture.poll({"test": str(folder)})
    path.write_text(json.dumps({"secret": 'secret"\nvalue'}))
    assert capture.poll({"test": str(folder)})
    assert json.loads(capture.files["test/snapshot.json"])["secret"] == "MASKED"
    assert not capture.poll({"test": str(folder)})


def test_model_capture_nested_calls_opaque_options_and_error(tmp_path, monkeypatch):
    import asyncio
    import sys
    from types import SimpleNamespace

    from envweaver.agents.adapters.sandbox._model_capture import install
    from envweaver.trajectory.atif import context

    seen = []

    def completion(**kwargs):
        seen.append(kwargs)
        if kwargs.get("fail"):
            raise ConnectionError("temporary")
        return {"choices": [{"message": {"content": "ok"}}]}

    async def acompletion(**kwargs):
        return module.completion(**kwargs)

    module = SimpleNamespace(completion=completion, acompletion=acompletion)
    monkeypatch.setitem(sys.modules, "litellm", module)
    monkeypatch.setenv("TEST_API_KEY", 'escaped"\nsecret')
    path = tmp_path / "calls.jsonl"
    install(path)
    opaque = object()
    result = asyncio.run(
        module.acompletion(
            messages=[{"role": "user", "content": 'escaped"\nsecret'}],
            api_key='escaped"\nsecret',
            callback=opaque,
        )
    )
    assert result["choices"][0]["message"]["content"] == "ok"
    assert seen[0]["callback"] is opaque
    with pytest.raises(ConnectionError):
        module.completion(fail=True)
    events = json_lines(path.read_text())
    assert [e["type"] for e in events] == ["request", "response", "request", "error"]
    assert "api_key" not in events[0]["data"]["request"]
    assert events[0]["data"]["request"]["messages"][0]["content"] == "[REDACTED]"
    output = AgentOutput(tmp_path / "out", {"agent": "test", "agent_version": "1", "model": "m"})
    extra = context(output, events[0]["data"]["request"], source="test")
    assert extra["context"]["complete"] is False
    assert extra["context"]["missing"] == ["unserializable:object"]


def test_pi_compaction_keeps_its_own_actual_model_context(output):
    summary_request = {
        "messages": [{"role": "system", "content": "summarize only selected history"}]
    }
    chunks = (
        "data: "
        + json.dumps({"choices": [{"delta": {"content": "short summary"}}]})
        + "\n\ndata: [DONE]\n\n"
    )
    events = [
        {"type": "compaction_start", "call_id": "1", "data": {}},
        {
            "type": "http_request",
            "call_id": "http:2",
            "data": {"phase": "compaction", "payload": summary_request},
        },
        {"type": "http_response", "call_id": "http:2", "data": {"status": 200, "body": chunks}},
        {
            "type": "compaction",
            "call_id": "1",
            "data": {"compactionEntry": {"summary": "short summary"}},
        },
        {
            "type": "request",
            "call_id": "2",
            "data": {"payload": {"messages": [{"role": "user", "content": "short summary"}]}},
        },
    ]
    doc = pi(events, output)
    validate_trajectory(doc)
    child = doc["subagent_trajectories"][0]
    assert child["steps"][0]["message"] == "short summary"
    saved = json.loads(
        (output.directory / child["steps"][0]["extra"]["context"]["path"]).read_text()
    )
    assert saved == summary_request
    assert (
        doc["steps"][0]["observation"]["results"][0]["subagent_trajectory_ref"][0]["trajectory_id"]
        == child["trajectory_id"]
    )


def test_terminus_continuation_files_are_portable(output):
    root = document(output.result["metadata"], "root")
    add_step(root, "user", "start")
    root["continued_trajectory_ref"] = "trajectory.cont-1.json"
    child = document(output.result["metadata"], "next")
    add_step(child, "user", "summary")
    documents = terminus({"trajectory.json": root, "trajectory.cont-1.json": child}, [], output)
    assert not (output.directory / "trajectory.cont-1.json").exists()
    doc = documents.pop("trajectory.json")
    output.save_trajectory(doc, related=documents)
    continued = output.directory / doc["continued_trajectory_ref"]
    assert continued.is_file()
    validate_trajectory(json.loads(continued.read_text()))
    assert "trajectory.cont-1.json" in output.result["artifacts"]["files"]


def test_terminus_copied_context_does_not_consume_original_call(output):
    original = document(output.result["metadata"], "summary")
    add_step(original, "agent", "summary")
    continued = document(output.result["metadata"], "continued")
    add_step(continued, "agent", "summary", is_copied_context=True)
    events = [
        {
            "type": "request",
            "call_id": "call",
            "data": {"api": "acompletion", "request": {"messages": []}},
        },
        {
            "type": "response",
            "call_id": "call",
            "data": {"choices": [{"message": {"content": "summary"}}]},
        },
    ]
    # A continuation can appear before the original summarizer in a native snapshot.
    documents = terminus(
        {"trajectory.cont-1.json": continued, "trajectory.json": original}, events, output
    )
    doc = documents["trajectory.json"]
    child = documents["trajectory.cont-1.json"]
    assert doc["steps"][0]["extra"]["context"]["complete"]
    assert child["steps"][0]["extra"]["context"] == doc["steps"][0]["extra"]["context"]


def test_codex_waits_for_first_inference_snapshot(output):
    trace = CodexTrace()
    trace.event("rollout_started", root_thread_id="root")
    trace.event("thread_started", thread_id="root")
    assert codex(trace.finish(), output) is None


@pytest.mark.parametrize("schema", [1, 2, 3])
def test_codex_tool_result_can_precede_completed_inference(output, schema):
    trace = CodexTrace(schema)
    trace.event("rollout_started", root_thread_id="root")
    trace.event(
        "inference_started", inference_call_id="c", request_payload=trace.payload({"input": []})
    )
    trace.event(
        "tool_call_ended",
        tool_call_id="t",
        result_payload=trace.payload({"response_item": {"output": "early result"}}),
    )
    trace.event(
        "inference_completed",
        inference_call_id="c",
        response_payload=trace.payload(
            {
                "output_items": [
                    {"type": "function_call", "call_id": "t", "name": "shell", "arguments": "{}"}
                ]
            }
        ),
    )
    trace.event("thread_started", "child", thread_id="child")
    doc = codex(trace.finish(), output)
    validate_trajectory(doc)
    assert doc["steps"][0]["observation"]["results"][0]["content"] == "early result"
    assert doc["extra"]["pending_thread_ids"] == ["child"]


def test_mini_changed_context_does_not_duplicate_tool_results(output):
    messages = [{"role": "system", "content": "original"}]
    events = [
        {
            "type": "request",
            "call_id": "a",
            "data": {"api": "completion", "request": {"messages": messages}},
        },
        {
            "type": "response",
            "call_id": "a",
            "data": {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"id": "t", "function": {"name": "shell", "arguments": "{}"}}
                            ]
                        }
                    }
                ]
            },
        },
    ]
    messages = [
        *messages,
        {"role": "assistant", "content": ""},
        {"role": "tool", "tool_call_id": "t", "content": "once"},
    ]
    for identity, system in [("b", "original"), ("c", "updated")]:
        events.append(
            {
                "type": "request",
                "call_id": identity,
                "data": {
                    "api": "completion",
                    "request": {"messages": [{"role": "system", "content": system}, *messages[1:]]},
                },
            }
        )
    doc = mini(events, output)
    validate_trajectory(doc)
    tool_step = next(s for s in doc["steps"] if s.get("tool_calls"))
    assert len(tool_step["observation"]["results"]) == 1


def test_pi_http_retries_and_compaction_failure_are_preserved(output):
    def event(kind, call, **data):
        return {"type": kind, "call_id": call, "data": data}

    events = [
        event("request", "a", payload={"messages": []}),
        event(
            "http_request",
            "http:1",
            phase="agent",
            agent_call_id="a",
            payload={"messages": [], "attempt": 1},
        ),
        event("http_response", "http:1", status=503, body='{"error":"busy"}'),
        event(
            "http_request",
            "http:2",
            phase="agent",
            agent_call_id="a",
            payload={"messages": [], "attempt": 2},
        ),
        event(
            "http_response", "http:2", status=200, body='{"choices":[{"message":{"content":"ok"}}]}'
        ),
        event(
            "message",
            "a",
            role="assistant",
            content=[{"type": "text", "text": "ok"}],
            stopReason="stop",
        ),
        event("compaction_start", "a"),
        event("http_request", "http:3", phase="compaction", payload={"messages": []}),
        event("http_error", "http:3", message="connection reset"),
    ]
    doc = pi(events, output)
    validate_trajectory(doc)
    calls = doc["extra"]["model_calls"]
    assert [c["response_status"] for c in calls] == ["failed", "completed", "failed"]
    assert [
        json.loads((output.directory / c["context"]["path"]).read_text()).get("attempt")
        for c in calls
    ] == [1, 2, None]
    assert doc["subagent_trajectories"][0]["steps"][0]["extra"]["response_status"] == "failed"


def test_jsonl_preserves_unicode_separators():
    event = {"text": "A\u0085B\u2028C\u2029D 中文"}
    assert json_lines(json.dumps(event, ensure_ascii=False) + "\r\n") == [event]


def test_pi_sse_preserves_unicode_separators():
    from envweaver.agents.adapters.pi.trajectory import _compaction_response

    value = "A\u0085B\u2028C\u2029D 中文"
    body = (
        "data: "
        + json.dumps({"choices": [{"delta": {"content": value}}]}, ensure_ascii=False)
        + "\r\n\r\ndata: [DONE]\r\n"
    )
    assert _compaction_response({"body": body}) == (value, "", True)


def test_native_snapshot_waits_for_utf8_suffix(output, tmp_path):
    import sys

    from tests.agents.test_cli_agents import LocalTransport

    folder = tmp_path / "source"
    folder.mkdir()
    path = folder / "events.jsonl"
    capture = NativeFiles(
        runtime=LocalTransport(), instance=None, python=sys.executable,
        output=output, redact=lambda text: text,
    )
    first = b'{"text":"ready"}\n'
    second = json.dumps({"text": "中文\u2028"}, ensure_ascii=False).encode() + b"\n"
    path.write_bytes(first + second[:11])  # Ends in the middle of the first Chinese character.
    assert capture.poll({"test": str(folder)})
    assert json_lines(capture.files["test/events.jsonl"]) == [{"text": "ready"}]
    path.write_bytes(first + second)
    assert capture.poll({"test": str(folder)})
    assert json_lines(capture.files["test/events.jsonl"])[1] == {"text": "中文\u2028"}
    path.write_bytes(b'{"text":"invalid\xff"}\n')
    with pytest.raises(UnicodeDecodeError):
        capture.poll({"test": str(folder)})


@pytest.mark.parametrize("body", ["null", "[]", "123", '"unexpected"'])
def test_pi_unrecognized_json_response_is_not_a_capture_failure(body):
    from envweaver.agents.adapters.pi.trajectory import _compaction_response

    assert _compaction_response({"body": body}) == ("", "", False)


def test_mini_partial_snapshot_still_exports_new_model_calls(output, monkeypatch):
    from types import SimpleNamespace

    from envweaver import MiniSweAgent

    agent = MiniSweAgent()
    agent._output = output
    agent._remote = "/sandbox"
    agent._native_count = 0
    agent._native_snapshot = None
    agent._snapshot = None
    events = [
        {
            "type": "request",
            "call_id": "first",
            "data": {"api": "completion", "request": {"messages": []}},
        },
        {
            "type": "response",
            "call_id": "first",
            "data": {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"id": "tool", "function": {"name": "shell", "arguments": "{}"}}
                            ]
                        }
                    }
                ]
            },
        },
    ]
    native = {
        "messages": [{"role": "tool", "tool_call_id": "tool", "content": "retained"}],
        "info": {"config": {"secret": "private"}},
    }
    bodies = iter([json.dumps(native).encode(), b'{"messages":', b"\xe4"])
    agent._native = SimpleNamespace(files={}, poll=lambda sources: False)
    monkeypatch.setattr(agent, "_read_remote", lambda path: next(bodies))
    agent._native.files["mini/model-calls.jsonl"] = "\n".join(json.dumps(e) for e in events)
    agent._poll_native()
    events.append(
        {
            "type": "request",
            "call_id": "second",
            "data": {"api": "completion", "request": {"messages": []}},
        }
    )
    agent._native.files["mini/model-calls.jsonl"] = "\n".join(json.dumps(e) for e in events)
    for _ in range(2):
        agent._poll_native()
        doc = json.loads((output.directory / "trajectory.json").read_text())
        assert len(doc["steps"]) == 2
        assert doc["steps"][0]["observation"]["results"][0]["content"] == "retained"
        assert doc["steps"][1]["extra"]["response_status"] == "pending"
        assert "config" not in doc["extra"]["native_info"]


@pytest.mark.parametrize("late_payload", [False, True])
@pytest.mark.parametrize("schema", [1, 2, 3])
def test_codex_failed_inference_keeps_partial_response(output, late_payload, schema):
    trace = CodexTrace(schema)
    trace.event("rollout_started", root_thread_id="root")
    trace.event(
        "inference_started",
        inference_call_id="partial",
        request_payload=trace.payload({"model": "test", "input": []}),
    )
    response = {
        "output_items": [
            {"type": "message", "content": [{"type": "output_text", "text": "already received"}]},
            {"type": "reasoning", "summary": [{"text": "visible reasoning"}]},
            {
                "type": "function_call",
                "call_id": "tool",
                "name": "shell",
                "arguments": '{"cmd":"echo ok"}',
            },
        ],
        "token_usage": None,
    }
    ref = trace.payload(response)
    response_path = next(reversed(trace.files))
    trace.event(
        "inference_failed",
        inference_call_id="partial",
        error="stream interrupted",
        partial_response_payload=ref,
    )
    trace.event(
        "tool_call_ended",
        tool_call_id="tool",
        result_payload=trace.payload({"response_item": {"output": "ok"}}),
    )
    files = trace.finish()
    if late_payload:
        pending = codex({k: v for k, v in files.items() if k != response_path}, output)
        validate_trajectory(pending)
        assert pending["steps"][0]["extra"]["response_status"] == "failed"
        assert pending["steps"][0]["extra"]["response_incomplete"] is True
    doc = codex(files, output)
    validate_trajectory(doc)
    step = doc["steps"][0]
    assert step["extra"]["response_status"] == "failed"
    assert step["extra"]["response_incomplete"] is True
    assert step["extra"]["failure"]["error"] == "stream interrupted"
    assert step["extra"]["native_response"] == "logs/native/" + response_path
    assert step["message"] == [{"type": "text", "text": "already received"}]
    assert step["reasoning_content"] == "visible reasoning"
    assert step["tool_calls"][0]["tool_call_id"] == "tool"
    assert step["observation"]["results"][0]["content"] == "ok"
    assert not step.get("metrics")


@pytest.mark.parametrize("path", ["/sandbox/deleted.png", "https://example.invalid/image.png"])
def test_missing_media_is_explicit_without_fabricated_bytes(output, path):
    media = Media(output, read_remote=lambda path: None)
    image = media.image(path)
    doc = document(output.result["metadata"], "missing-media")
    add_step(doc, "user", [image])
    doc["extra"] = {"media_missing": media.missing}
    validate_trajectory(doc)
    assert image["source"]["path"] == path
    assert doc["extra"]["media_missing"] == ["image:" + path]
    assert not output.result["artifacts"]["files"]


@pytest.mark.parametrize("raw", ['{"a":', "[]", "null", "false", "42", '"quoted 中文"'])
@pytest.mark.parametrize("adapter", ["codex", "mini"])
def test_non_object_tool_arguments_are_preserved(output, raw, adapter):
    if adapter == "codex":
        trace = CodexTrace()
        trace.event("rollout_started", root_thread_id="root")
        trace.call(
            "root",
            "call",
            {"input": []},
            {
                "output_items": [
                    {"type": "function_call", "call_id": "tool", "name": "shell", "arguments": raw},
                ]
            },
        )
        doc = codex(trace.finish(), output)
    else:
        doc = mini(
            [
                {
                    "type": "request",
                    "call_id": "call",
                    "data": {"api": "completion", "request": {"messages": []}},
                },
                {
                    "type": "response",
                    "call_id": "call",
                    "data": {
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {
                                            "id": "tool",
                                            "function": {"name": "shell", "arguments": raw},
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                },
            ],
            output,
        )
    validate_trajectory(doc)
    call = doc["steps"][0]["tool_calls"][0]
    assert call["extra"]["raw_arguments"] == raw
    assert call["arguments"] == {}


@pytest.mark.parametrize("schema", [1, 2, 3])
def test_codex_reused_tool_ids_keep_results_on_their_inference(output, schema):
    trace = CodexTrace(schema)
    trace.event("rollout_started", root_thread_id="root")
    request = {"input": []}
    response = {
        "output_items": [
            {"type": "function_call", "call_id": "reused", "name": "shell", "arguments": "{}"}
        ]
    }
    # A streamed tool can finish after the next inference has already started.
    trace.call("root", "first", request, response)
    trace.event("tool_call_started", tool_call_id="reused")
    trace.call("root", "second", request, response)
    trace.event(
        "tool_call_ended",
        tool_call_id="reused",
        result_payload=trace.payload({"response_item": {"output": "first result"}}),
    )
    trace.event("tool_call_started", tool_call_id="reused")
    trace.event(
        "tool_call_ended",
        tool_call_id="reused",
        result_payload=trace.payload({"response_item": {"output": "second result"}}),
    )
    doc = codex(trace.finish(), output)
    validate_trajectory(doc)
    assert [s["observation"]["results"][0]["content"] for s in doc["steps"]] == [
        "first result",
        "second result",
    ]
    assert all(len(s["observation"]["results"]) == 1 for s in doc["steps"])


def test_duplicate_tool_id_in_one_step_is_rejected(output):
    doc = document(output.result["metadata"], "duplicate")
    call = {"tool_call_id": "same", "function_name": "shell", "arguments": {}}
    add_step(doc, "agent", "", tool_calls=[call, call.copy()])
    with pytest.raises(ValueError, match="Duplicate ATIF tool_call_id"):
        validate_trajectory(doc)


def test_codex_live_shard_and_nested_fragments(output, tmp_path):
    import struct
    import sys

    import zstandard

    from envweaver.agents.adapters.codex.payloads import read_payload, unpack_shard
    from tests.agents.test_cli_agents import LocalTransport

    def record(i, obj):
        identity = f"raw_payload:{i}".encode()
        data = json.dumps(obj).encode()
        return struct.pack("<HQ", len(identity), len(data)) + identity + data

    def ref(i, *parts):
        return {"raw_payload_id": f"raw_payload:{i}", "shard": 1, "parts": list(parts)}

    folder = tmp_path / "bundle"
    (folder / "payloads").mkdir(parents=True)
    shard = folder / "payloads/000001.zst"
    request = ref(1, ref(2, ref(3)), ref(4))
    compressor = zstandard.ZstdCompressor().compressobj()
    first = record(1, ['{"model":"demo","input":[', ",", "]}"]) + record(2, ['{"text":', "}"])
    shard.write_bytes(compressor.compress(first) + compressor.flush(zstandard.COMPRESSOBJ_FLUSH_BLOCK))
    collector = NativeFiles(
        runtime=LocalTransport(), instance=None, python=sys.executable,
        output=output, redact=lambda s: s.replace("SECRET", "redacted"),
    )

    def snapshot():
        return collector.poll({"bundle": str(folder)}, archives={".zst": unpack_shard})

    assert snapshot()
    missing = set()
    assert read_payload(collector.files, "bundle", request, missing) is None and missing
    with shard.open("ab") as stream:
        stream.write(compressor.compress(record(3, "SECRET\n测试") + record(4, None)))
        stream.write(compressor.flush())
    assert snapshot()
    missing.clear()
    assert read_payload(collector.files, "bundle", request, missing) == {
        "model": "demo", "input": [{"text": "redacted\n测试"}, None],
    }
    assert not missing and not snapshot()
    for status in ("truncated", "omitted"):
        part = {**ref(3), "capture": {"status": status}}
        assert read_payload(collector.files, "bundle", ref(1, ref(2, part), ref(4)), missing) is None
        assert "raw_payload:3" in missing
    damaged = zstandard.ZstdCompressor().compress(record(1, {})[:-1])
    with pytest.raises(ValueError, match="Partial record"):
        list(unpack_shard("000001.zst", damaged))

def test_codex_omitted_request_keeps_the_observed_answer(output):
    trace = CodexTrace(3)
    trace.event("inference_started", inference_call_id="short",
                request_payload={"raw_payload_id": "raw_payload:8", "shard": None,
                                 "capture": {"status": "omitted", "original_bytes": 1024}})
    trace.event("inference_completed", inference_call_id="short", response_payload=trace.payload({
        "output_items": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
    }))
    doc = codex(trace.finish(), output)
    validate_trajectory(doc)
    step, = doc["steps"]
    assert doc["extra"]["capture"] == step["extra"]["context"] == {
        "complete": False, "missing": ["raw_payload:8"],
    }
    assert step["message"] == [{"type": "text", "text": "ok"}]
