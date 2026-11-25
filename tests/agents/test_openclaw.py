import json

import pytest

from envweaver import AgentOutput, OpenClawAgent
from envweaver.agents.adapters._provider_http import model_calls
from envweaver.agents.adapters.openclaw.adapter import failure, read_envelope, repeated_outage
from envweaver.agents.adapters.openclaw.trajectory import convert
from envweaver.errors import ErrorType
from envweaver.trajectory.atif import validate_trajectory


def test_adapter_controls_attempt_state_and_text_input():
    agent = OpenClawAgent(model="local/model", env={"OPENAI_API_KEY": "secret"})
    assert agent.new().config == agent.config
    with pytest.raises(ValueError, match="image"):
        agent.preflight([{"type": "image", "path": "picture.jpg"}])
    with pytest.raises(ValueError, match="override"):
        OpenClawAgent(extra_args=["--state-dir=/tmp/shared"]).preflight([])


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (400, "context_length_exceeded", ErrorType.CONTEXT_LIMIT),
        (400, "invalid_parameter", ErrorType.CONFIGURATION),
        (401, "invalid_api_key", ErrorType.AUTHENTICATION),
        (429, "rate_limit_exceeded", ErrorType.PROVIDER_TRANSIENT),
        (503, "service_unavailable", ErrorType.PROVIDER_TRANSIENT),
    ],
)
def test_provider_failure(status, code, expected):
    result = {"error": {"kind": "fallback_exhausted", "message": "Model failed"}}
    call = {"status": status, "body": json.dumps({"error": {"code": code}})}
    assert failure(result, [call]).type == expected


@pytest.mark.parametrize("response_ids", [["r1", "r2"], ["r1", "r1"], [None, None]])
def test_transcript_recovers_requests_from_unique_replies(tmp_path, response_ids):
    output = AgentOutput(tmp_path, OpenClawAgent(model="test/mock").metadata)
    records, messages = [], [{"role": "user", "content": "Read the marker."}]
    for n, identity in enumerate(response_ids):
        request = {"messages": [{"role": "user", "content": str(n)}], "tools": []}
        records.extend(
            [
                {"id": str(n), "request": request},
                {
                    "id": str(n),
                    "status": 200,
                    "body": "data: "
                    + json.dumps(
                        {
                            "id": identity,
                            "choices": [
                                {
                                    "delta": {"content": "blue"}
                                    if n
                                    else {
                                        "reasoning_content": "inspect",
                                        "tool_calls": [{"id": "tool1"}],
                                    }
                                }
                            ],
                        }
                    )
                    + "\n\n",
                },
            ]
        )
        block = (
            {"type": "text", "text": "blue"}
            if n
            else {
                "type": "toolCall",
                "id": "tool1",
                "name": "read",
                "arguments": {"path": "marker"},
            }
        )
        messages.append({"role": "assistant", "content": [block], "responseId": identity})
        if not n:
            messages.append({"role": "toolResult", "toolCallId": "tool1", "content": "blue"})
    calls = model_calls(records, output, source="openclaw.http")
    doc = convert("one-session", messages, calls, {"final": "blue"}, output)
    validate_trajectory(doc)
    first, last = doc["steps"][1:]
    assert (
        first["observation"]["results"][0]["source_call_id"]
        == first["tool_calls"][0]["tool_call_id"]
    )
    assert last["message"][0]["text"] == "blue"
    for n, step in enumerate([first, last]):
        ctx = step["extra"]["context"]
        assert ctx["complete"]
        assert json.loads((tmp_path / ctx["path"]).read_text()) == records[2 * n]["request"]


@pytest.mark.integration
@pytest.mark.parametrize("scenario", ["normal", "context_text"])
def test_real_cli_eval(tmp_path, scenario):
    import os

    from envweaver import EvalConfig, Runtime, RuntimeRef, Task, Verifier, eval
    from tests.agents.test_native_lifecycle_integration import start_api

    image = os.environ.get("ENVWEAVER_TEST_OPENCLAW_IMAGE")
    if not image:
        pytest.skip("requires a Docker image with OpenClaw and EnvWeaver")
    server, worker, requests, _, release = start_api(
        "openclaw", scenario, "printf BLUE > answer.txt"
    )
    try:
        provider = {
            "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
            "api": "openai-completions",
            "apiKey": "local-test-key",
            "models": [{"id": "mock", "name": "mock", "contextWindow": 65536}],
        }
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"models": {"providers": {"mock": provider}}}))
        entry = tmp_path / "verifier/run.py"
        entry.parent.mkdir()
        entry.write_text(
            "import argparse,json\nfrom envweaver import Runtime\n"
            "p=argparse.ArgumentParser();p.add_argument('--instance');r=Runtime()\n"
            "i=r.connect(path=p.parse_args().instance)\n"
            "code,out,_=r.exec(instance=i,command=['cat','/workspace/answer.txt'])\n"
            "print(json.dumps({'score':float(code==0 and out=='BLUE')}))\n"
        )
        ref = RuntimeRef("docker", image, {"network": "host", "command": ["sleep", "infinity"]})
        task = Task(
            name="write",
            task_ref="write",
            metadata={},
            cwd="/workspace",
            goal=[{"type": "text", "text": "Write BLUE to answer.txt."}],
            context=[{"source": str(config), "target": "/workspace/config.json"}],
            task_runtime_ref=ref,
            verifier_runtime_ref=ref,
            verifier=Verifier(entry=entry, runtime=Runtime(), timeout=30),
        )
        (result,) = eval(
            tasks=task,
            agent=OpenClawAgent(
                model="mock/mock", extra_args=["--config", "/workspace/config.json"]
            ),
            config=EvalConfig(agent_timeout=60, max_retries=0),
            output_dir=tmp_path / "out",
        )
        assert result["score_valid"], result["errors"]
        assert result["verification_result"]["score"] == (1 if scenario == "normal" else 0)
        if scenario == "normal":
            (directory,) = (tmp_path / "out").glob("*/agent")
            doc = json.loads((directory / "trajectory.json").read_text())
            steps = [s for s in doc["steps"] if s["source"] == "agent"]
            assert all(s["extra"]["context"]["complete"] for s in steps)
            assert [
                json.loads((directory / s["extra"]["context"]["path"]).read_text()) for s in steps
            ] == requests
        else:
            assert result["agent_stop_reason"] == "context_limit"
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.parametrize(
    ("status", "body", "category"),
    [
        (
            200,
            'data: {"type":"error","error":{"type":"overloaded_error"}}\n\n',
            ErrorType.PROVIDER_TRANSIENT,
        ),
        (
            200,
            'data: {"type":"error","code":"server_error","message":"busy"}\n\n',
            ErrorType.PROVIDER_TRANSIENT,
        ),
        (
            200,
            'data: {"object":"chat.completion.chunk","choices":[{"finish_reason":null}]}\n\n',
            ErrorType.PROVIDER_TRANSIENT,
        ),
        (
            200,
            'data: {"type":"message_start","message":{"id":"msg"}}\n\n',
            ErrorType.PROVIDER_TRANSIENT,
        ),
        (
            200,
            'data: {"type":"response.created","response":{"id":"resp"}}\n\n',
            ErrorType.PROVIDER_TRANSIENT,
        ),
        (429, '{"error":{"code":"insufficient_quota"}}', ErrorType.CONFIGURATION),
        (429, '{"error":{"type":"invalid_request_error"}}', ErrorType.PROVIDER_TRANSIENT),
        (400, "prompt is too long", ErrorType.CONTEXT_LIMIT),
        (400, '{"error":"prompt is too long"}', ErrorType.CONTEXT_LIMIT),
        (400, '{"message":"prompt exceeds max length"}', ErrorType.CONTEXT_LIMIT),
        (400, "ordinary non-JSON bad request", ErrorType.CONFIGURATION),
    ],
)
def test_wire_failure_precedes_cli_timeout(status, body, category):
    envelope = {"status": "timeout", "error": {"kind": "timeout"}}
    assert failure(envelope, [{"status": status, "body": body}]).type == category


def test_terminal_transport_diagnostic_and_cli_usage_classification():
    envelope = {"status": "timeout", "error": {"kind": "timeout"}}
    assert failure(envelope, []).type == ErrorType.PROVIDER_TRANSIENT
    terminal = {"stopReason": "error", "diagnostics": [{"type": "provider_transport_failure"}]}
    assert failure(envelope, [], terminal).type == ErrorType.PROVIDER_TRANSIENT
    assert failure({"error": {"type": "cli_error"}}, []).type == ErrorType.CONFIGURATION
    assert (
        failure(
            {"error": "unexpected failure"}, [], {"stopReason": "error", "diagnostics": [None]}
        ).type
        == ErrorType.UNKNOWN
    )


def test_interrupted_attempt_keeps_goal_and_unanswered_requests(tmp_path):
    output = AgentOutput(tmp_path, OpenClawAgent().metadata)
    from envweaver.trajectory.atif import add_step, document

    initial = document(output.result["metadata"], "started")
    add_step(initial, "user", "Read the marker.")
    output.save_trajectory(initial)
    calls = model_calls(
        [
            {"id": "a", "request": {"model": "test", "messages": []}},
            {"id": "b", "transport_error": "ECONNREFUSED"},
        ],
        output,
        source="openclaw.http",
    )
    doc = convert("started", [], calls, {}, output)
    validate_trajectory(doc)
    assert doc["steps"] == initial["steps"]
    assert doc["extra"]["model_calls"] == calls
    assert not doc["extra"]["capture"]["complete"]
    assert calls[0]["context"]["complete"] and not calls[1]["context"]["complete"]


def test_noisy_stdout_uses_last_complete_envelope():
    ok = {"ok": True, "status": "ok", "payloads": [{"ok": False}]}
    text = 'diagnostic {"ok":false}\nmore: ' + json.dumps(ok, indent=2) + '\npartial {"ok":'
    assert read_envelope(text) == ok
    assert read_envelope('only a log {"unrelated":true}') == {}


@pytest.mark.parametrize(
    ("thinking", "expected"), [(None, None), ("trace-b", "b"), ("unknown", None)]
)
def test_reply_matching_uses_only_visible_thinking(tmp_path, thinking, expected):
    output = AgentOutput(tmp_path, OpenClawAgent().metadata)
    parts = [{"type": "text", "text": "Done"}]
    if thinking:
        parts.insert(0, {"type": "thinking", "thinking": thinking})
    records = []
    for key, reasoning in [("a", ""), ("b", "trace-b")]:
        reply = {"content": "Done", "reasoning_content": reasoning}
        records.extend(
            [
                {"id": key, "request": {"messages": []}},
                {"id": key, "status": 200, "body": json.dumps({"choices": [{"message": reply}]})},
            ]
        )
    doc = convert(
        "session",
        [{"role": "assistant", "content": parts}],
        model_calls(records, output, source="openclaw.http"),
        {},
        output,
    )
    extra = doc["steps"][0]["extra"]
    assert extra.get("call_id") == expected
    assert extra["context"]["complete"] is (expected is not None)


def test_ambiguous_reply_stays_unmapped_and_internal_messages_keep_origin(tmp_path):
    output = AgentOutput(tmp_path, OpenClawAgent().metadata)
    messages = [
        {"role": "user", "content": "goal", "provenance": {"kind": "external_user"}},
        {"role": "assistant", "stopReason": "error", "content": [], "errorMessage": "failed"},
        {"role": "user", "content": "resume", "provenance": {"kind": "internal_system"}},
        {
            "role": "assistant",
            "stopReason": "error",
            "content": [{"type": "text", "text": "partial"}],
        },
    ]
    calls = model_calls(
        [
            row
            for i in ("a", "b")
            for row in [
                {"id": i, "request": {"messages": []}},
                {"id": i, "status": 200, "body": '{"choices":[{"message":{"content":"partial"}}]}'},
            ]
        ],
        output,
        source="openclaw.http",
    )
    doc = convert("session", messages, calls, {}, output)
    validate_trajectory(doc)
    assert [s["source"] for s in doc["steps"]] == ["user", "system", "system", "agent"]
    last = doc["steps"][-1]["extra"]
    assert last["response_status"] == "incomplete" and not last["context"]["complete"]


def test_outage_requires_exclusively_repeated_retryable_http_failures():
    limited = {"status": 429}
    assert repeated_outage([limited, {"status": 503}])
    for calls in (
        [],
        [limited],
        [limited, {}],
        [{"status": 200}, limited],
        [limited, {"status": 429, "body": '{"error":{"code":"insufficient_quota"}}'}],
    ):
        assert not repeated_outage(calls)


def test_live_snapshot_reads_only_the_explicit_root(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path

    from envweaver.agents.adapters.openclaw import adapter

    node = shutil.which("node")
    if not node:
        pytest.skip("snapshot.cjs requires Node")
    script = tmp_path / "snapshot.cjs"
    shutil.copy(Path(adapter.__file__).with_name(script.name), script)
    (tmp_path / "trace").mkdir()
    cli = tmp_path / "openclaw"
    cli.write_text('#!/bin/sh\ncat "$(dirname "$0")/catalog.json"\n')
    cli.chmod(0o700)
    sdk = tmp_path / "node_modules/openclaw/plugin-sdk"
    sdk.mkdir(parents=True)
    (sdk / "session-transcript-runtime.js").write_text(
        "exports.readSessionTranscriptEvents = async s => "
        "[{type:'message', message:{role:'assistant', content:s.sessionId}}];"
    )

    def row(scope, identity):
        return {"key": f"agent:team:{scope}:{identity}", "sessionId": identity, "agentId": "team"}

    def snapshot(rows, mode="live", pinned=None):
        (tmp_path / "catalog.json").write_text(json.dumps({
            "stores": [{"agentId": "team", "path": "/store"}], "sessions": rows,
        }))
        return json.loads(subprocess.check_output(
            [node, script, cli, mode, json.dumps(pinned)], text=True
        ))

    child, root = row("subagent", "child"), row("explicit", "root")
    messages = tmp_path / "trace/messages.json"
    assert snapshot([child]) is None
    assert snapshot([root, row("explicit", "other"), child]) is None
    assert not messages.exists()
    pinned = snapshot([child, root])
    assert pinned["sessionId"] == "root"
    assert list(json.loads(messages.read_text())) == ["root"]
    assert snapshot([child, root], "final", pinned) == pinned
    assert set(json.loads(messages.read_text())) == {"child", "root"}


@pytest.mark.parametrize("final_id", ["main-session", "other-session"])
def test_empty_final_snapshot_only_reuses_same_session(tmp_path, final_id):
    from unittest.mock import Mock

    agent = OpenClawAgent()
    agent._output = AgentOutput(tmp_path, agent.metadata)
    agent._runtime = Mock()
    agent._runtime.exec.return_value = (0, "null", "")
    agent._instance = Mock()
    agent._remote = "/attempt"
    agent._sandbox_path = "/bin"
    agent._main_session = {"sessionId": "main-session"}
    agent._completed = True
    files = {"openclaw/messages.json": json.dumps({
        "main-session": [
            {"role": "user", "content": "goal"},
            {"role": "assistant", "content": [{"type": "text", "text": "observed"}]},
        ]
    })}

    def poll(sources):
        files["openclaw/messages.json"] = "{}"
        return True

    agent._native = Mock(files=files, poll=poll)
    agent._export({"sessionId": final_id, "ok": True, "final": "finished"})
    doc = json.loads((tmp_path / "trajectory.json").read_text())
    validate_trajectory(doc)
    replies = [s["message"] for s in doc["steps"] if s["source"] == "agent"]
    expected = [[{"type": "text", "text": "observed"}]] if final_id == "main-session" else ["finished"]
    assert replies == expected
    assert agent._output.result["metadata"]["trajectory_export_error"]
