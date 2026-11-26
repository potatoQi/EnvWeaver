import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from envweaver import (
    AgentOutput,
    EvalConfig,
    Runtime,
    RuntimeRef,
    StirrupAgent,
    Task,
    Verifier,
    eval,
)
from envweaver.agents.capture import litellm
from envweaver.trajectory.atif import validate_trajectory


def test_configuration_is_reusable():
    agent = StirrupAgent(model="openai/test", context_window_tokens=8192, max_tokens=512)
    assert agent.new().config == agent.config
    for options in (
        {"system_prompt_mode": "replace"},
        {"system_prompt_mode": "unknown"},
        {"command_timeout": 0},
        {"submission_dir": "relative"},
    ):
        with pytest.raises(ValueError):
            StirrupAgent(model="openai/test", context_window_tokens=65536, **options)
    for options in ({"max_tokens": 0}, {"max_tokens": 8193}, {"max_turns": True}):
        with pytest.raises(ValueError):
            StirrupAgent(model="openai/test", context_window_tokens=8192, **options)


def test_native_failure_categories(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    exceptions = pytest.importorskip("stirrup.core.exceptions")
    httpx = pytest.importorskip("httpx")
    pytest.importorskip("litellm")
    from envweaver.agents.adapters.stirrup.runner import failure

    incomplete = exceptions.IncompleteResponseError("incomplete response")
    assert failure(incomplete)["type"] == "unknown"
    incomplete.__cause__ = httpx.ReadError("connection lost")
    assert failure(incomplete)["type"] == "provider_transient"
    if hasattr(exceptions, "CompactionRequiredError"):
        error = exceptions.CompactionRequiredError("request too large", reason="request_size")
        assert failure(error)["type"] == "context_limit"


def test_public_request_identifiers_survive_eval_configuration():
    from envweaver.evaluation.store import configuration

    agent = StirrupAgent(
        model="openai/test",
        context_window_tokens=65536,
        client_kwargs={
            "prompt_cache_key": "session",
            "extra_headers": {"Idempotency-Key": "unique"},
        },
    )
    settings = EvalConfig(task_runtime_config={"cache_key": "private-setting"})
    values = configuration(agent, settings)["values"]
    assert values["agent"]["config"]["client_kwargs"] == agent.config["client_kwargs"]
    assert values["eval"]["task_runtime_config"]["cache_key"] == "[REDACTED]"


def test_client_credentials_only_from_env():
    for options in (
        {"api_key": "secret"},
        {"extra_headers": {"Authorization": "secret"}},
        {"extra_body": {"X-Api-Key": "secret"}},
        {"extra_headers": {"Cookie": "session=secret"}},
        {"base_url": "https://gateway/v1?api_key=secret"},
        {"api_base": "http://user:secret@gateway/v1"},
        {"api_base": "http://gateway/v1?access_token=secret"},
    ):
        with pytest.raises(ValueError, match="credentials"):
            StirrupAgent(model="openai/test", context_window_tokens=65536, client_kwargs=options)


def test_expanded_tool_message_keeps_wire_labels(monkeypatch, tmp_path):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    pytest.importorskip("stirrup")
    pytest.importorskip("litellm")
    from envweaver.agents.adapters.stirrup import runner

    render = runner.to_openai_messages
    image = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "fixture"}}]}

    def expanded(messages):
        wire = []
        for message in messages:
            wire.extend(render([message]))
            if isinstance(message, runner.ToolMessage):
                wire.append(image)
        return wire

    monkeypatch.setattr(runner, "to_openai_messages", expanded)
    goal = runner.UserMessage(content="same text")
    trace = runner.RunTrace(goal)
    trace.messages = [
        goal,
        runner.ToolMessage(tool_call_id="t", content="ok"),
        runner.TurnWarningMessage(content="same text"),
    ]
    wire = expanded(trace.messages)
    assert trace.wire_message_kinds(wire) == {"2": "tool_result", "3": "turn_warning"}
    assert set(trace.wire_message_kinds([*wire, image]).values()) == {"unattributed"}
    monkeypatch.setattr(runner, "CALLS", tmp_path / "calls.jsonl")
    trace.positions["t"] = ("r", 0)
    trace.tool_result(trace.messages[1])
    event = json.loads(runner.CALLS.read_text())
    assert (
        event["call_id"] == "r"
        and event["data"]["content"] == render([trace.messages[1]])[0]["content"]
    )


def test_duplicate_provider_ids_are_not_ambiguous_observations(tmp_path):
    output = AgentOutput(tmp_path, {"agent": "mini-swe", "model": "mock", "agent_version": "test"})
    tools = [
        {"id": id_, "function": {"name": "test", "arguments": "{}"}}
        for id_ in ("repeat", "repeat", "r:0")
    ]
    reply = {"role": "assistant", "tool_calls": tools}
    events = [
        {"type": "request", "call_id": "r", "data": {"api": "acompletion", "request": {}}},
        {"type": "response", "call_id": "r", "data": {"choices": [{"message": reply}]}},
    ]
    messages = [
        reply,
        {"role": "tool", "tool_call_id": "repeat", "content": "ambiguous"},
        {"role": "tool", "tool_call_id": "r:0", "content": "known"},
    ]
    doc = litellm.convert(events, output, session_id="test", native_messages=messages)
    validate_trajectory(doc)
    step, unresolved = doc["steps"]
    assert step["extra"]["duplicate_tool_call_ids"] == ["repeat"]
    assert step["extra"]["native_response"]["choices"][0]["message"] == reply
    assert step["observation"]["results"] == [{"source_call_id": "r:0", "content": "known"}]
    assert unresolved["extra"]["unresolved_tool_call_id"] == "repeat"


def test_last_tool_result_survives_without_a_followup_request(tmp_path):
    output = AgentOutput(tmp_path, {"agent": "stirrup", "agent_version": "test", "model": "mock"})
    call = {"id": "finish-1", "function": {"name": "finish", "arguments": "{}"}}
    events = [
        {"type": "request", "call_id": "r1", "data": {"api": "acompletion", "request": {}}},
        {
            "type": "response",
            "call_id": "r1",
            "data": {"choices": [{"message": {"role": "assistant", "tool_calls": [call]}}]},
        },
        {
            "type": "tool_result",
            "call_id": "r1",
            "data": {
                "tool_call_id": "finish-1",
                "call_index": 0,
                "content": "missing file",
                "extra": {"is_error": True},
            },
        },
    ]
    events.insert(
        2, {"type": "request", "call_id": "r2", "data": {"api": "acompletion", "request": {}}}
    )
    doc = litellm.convert(events, output, session_id="stirrup-test")
    validate_trajectory(doc)
    assert doc["trajectory_id"] == doc["session_id"] == "stirrup-test"
    assert "observation" not in doc["steps"][-1]
    assert doc["steps"][0]["observation"]["results"] == [
        {"source_call_id": "finish-1", "content": "missing file", "extra": {"is_error": True}}
    ]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("case", "stop", "valid"),
    [
        ("success", "completed", True),
        ("interactive_tools", "completed", True),
        ("summary", "completed", True),
        ("summary_after_overflow", "completed", True),
        ("missing_finish_id", "completed", True),
        ("duplicate_ids", "configuration", False),
        ("missing_intermediate_id", "configuration", False),
        ("unavailable", "provider_transient", False),
        ("rate_limit", "provider_transient", False),
        ("turns", "step_limit", True),
        ("length", "token_limit", True),
        ("context", "context_limit", True),
        ("bad_request", "configuration", False),
        ("auth", "authentication", False),
    ],
)
def test_eval(tmp_path, case, stop, valid):
    image = os.getenv("ENVWEAVER_TEST_STIRRUP_IMAGE")
    if not image:
        pytest.skip("requires a Linux image with stirrup[litellm] and a verifier with EnvWeaver")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            overflow = case == "summary_after_overflow" and len(requests) == 3
            status = {
                "context": 400,
                "bad_request": 400,
                "auth": 401,
                "unavailable": 503,
                "rate_limit": 429,
            }.get("context" if overflow else case, 200)
            if status != 200:
                code = {
                    "context": "context_length_exceeded",
                    "bad_request": "invalid_request_error",
                    "auth": "invalid_api_key",
                    "rate_limit": "rate_limit_exceeded",
                    "unavailable": "server_error",
                }["context" if overflow else case]
                message = (
                    "This model's maximum context length is 8192 tokens. However, your messages resulted in 9000 tokens."
                    if overflow
                    else code
                )
                reply = {"error": {"code": code, "message": message, "type": code}}
            else:
                first = len(requests) == 1
                run_command = first or (case == "summary_after_overflow" and len(requests) == 2)
                name = "code_exec" if run_command else "finish"
                args = (
                    {"cmd": "cat /tmp/stirrup/input.txt > answer.txt"}
                    if run_command
                    else {"reason": "Copied the file.", "paths": ["answer.txt"]}
                )
                if case == "success":
                    if first:
                        args["cmd"] += " && printf second > second.txt"
                    else:
                        args["paths"] = ["answer.txt", "answer.txt", "second.txt"]
                if case == "interactive_tools":
                    name, args = [
                        (
                            "code_exec",
                            {
                                "cmd": """python3 -c "from PIL import Image; Image.new('RGB',(2000,1000)).save('big.png')" """
                            },
                        ),
                        ("view_image", {"path": "/workspace/big.png"}),
                        ("finish", {"reason": "outside root", "paths": ["/etc/hosts"]}),
                        ("code_exec", {"cmd": "sleep 5"}),
                        ("abandon_task_finish", {"reason": "Missing required input."}),
                    ][len(requests) - 1]
                missing_id = case == ("missing_intermediate_id" if first else "missing_finish_id")
                msg = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "" if missing_id else name,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                }
                if case == "duplicate_ids":
                    msg["tool_calls"] *= 2
                summary = (case, len(requests)) in (("summary", 2), ("summary_after_overflow", 4))
                if summary:
                    msg = {"role": "assistant", "content": "The input has been copied."}
                prompt_tokens = 7000 if case == "summary" and first else 100
                reply = {
                    "id": "chat-" + name,
                    "created": 1,
                    "model": "mock",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": msg,
                            "finish_reason": "length"
                            if case == "length"
                            else ("stop" if summary else "tool_calls"),
                        }
                    ],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": 20,
                        "total_tokens": prompt_tokens + 20,
                    },
                }
            raw = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_):
            pass

    material = tmp_path / "input.txt"
    material.write_text("stirrup-check")
    verifier = tmp_path / "verifier" / "run.py"
    verifier.parent.mkdir()
    verifier.write_text(
        "import argparse,json\nfrom envweaver import Runtime\n"
        "p=argparse.ArgumentParser();p.add_argument('--instance');r=Runtime()\n"
        "i=r.connect(path=p.parse_args().instance)\n"
        "code,out,err=r.exec(instance=i,command=['cat','/workspace/answer.txt'])\n"
        "print(json.dumps({'score':int(code==0 and out=='stirrup-check')}))\n"
    )
    ref = RuntimeRef("docker", image, {"command": ["sleep", "infinity"], "network": "host"})
    verify_ref = RuntimeRef(
        "docker",
        os.getenv("ENVWEAVER_TEST_SERVICE_IMAGE", image),
        {"command": ["sleep", "infinity"], "network": "host"},
    )
    task = Task(
        name=case,
        task_ref="stirrup/" + case,
        metadata={},
        cwd="/workspace",
        task_runtime_ref=ref,
        verifier_runtime_ref=verify_ref,
        context=[{"source": str(material), "target": "/tmp/stirrup/input.txt"}],
        goal=[{"type": "text", "text": "Copy input.txt to answer.txt and finish."}],
        verifier=Verifier(entry=verifier, runtime=Runtime(), timeout=30),
    )
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            agent = StirrupAgent(
                model="openai/mock",
                context_window_tokens=8192,
                max_tokens=512,
                max_turns={"turns": 1, "interactive_tools": 8}.get(case, 3),
                web=case == "success",
                **(
                    {
                        "view_image": True,
                        "allow_abandon": True,
                        "submission_dir": "/workspace",
                        "system_prompt_mode": "replace",
                        "system_prompt": "Use the task sandbox.",
                        "command_timeout": 1,
                    }
                    if case in ("success", "interactive_tools")
                    else {}
                ),
                client_kwargs={
                    "api_base": f"http://127.0.0.1:{server.server_port}/v1",
                    "prompt_cache_key": "session-1",
                },
                env={
                    "OPENAI_API_KEY": "stirrup-test-key",
                    **({"BRAVE_API_KEY": "test-registration-only"} if case == "success" else {}),
                },
            )
            (result,) = eval(
                tasks=task,
                agent=agent,
                output_dir=tmp_path / "eval",
                config=EvalConfig(agent_timeout=120, max_retries=0),
            )
        finally:
            server.shutdown()
            thread.join()

    assert result["score_valid"] is valid, result["errors"]
    assert (result["agent_stop_reason"] if valid else result["errors"][0]["type"]) == stop, result
    if valid:
        assert result["verification_result"]["score"] == int(
            case in ("success", "summary", "summary_after_overflow", "missing_finish_id", "turns")
        ), result
    (trajectory,) = (tmp_path / "eval").glob("*/agent/trajectory.json")
    doc = json.loads(trajectory.read_text())
    validate_trajectory(doc)
    steps = [s for s in doc["steps"] if s["source"] == "agent"]
    assert requests[0]["prompt_cache_key"] == "session-1"
    if case == "success":
        tools = {t["function"]["name"] for t in requests[0]["tools"]}
        assert tools == {
            "code_exec",
            "view_image",
            "finish",
            "abandon_task_finish",
            "web_search",
            "fetch_web_page",
        }
        assert all(
            req["messages"][0]
            == {"role": "system", "content": [{"type": "text", "text": "Use the task sandbox."}]}
            for req in requests
        )
        agent_result = json.loads((trajectory.parent / "result.json").read_text())
        assert not agent_result["metadata"]["abandoned"]
        assert agent_result["metadata"]["submitted_paths"] == [
            "answer.txt",
            "answer.txt",
            "second.txt",
        ]
        submitted = agent_result["metadata"]["submitted_files"]
        assert set(submitted) == {"answer.txt", "second.txt"}
        saved = submitted["answer.txt"]
        assert (trajectory.parent / submitted["second.txt"]).read_text() == "second"
        assert saved in agent_result["artifacts"]["files"]
        assert (trajectory.parent / saved).read_text() == "stirrup-check"
    if case == "interactive_tools":
        import base64
        import io

        from PIL import Image

        images = [
            part["image_url"]["url"]
            for msg in requests[2]["messages"]
            if isinstance(msg.get("content"), list)
            for part in msg["content"]
            if part.get("type") == "image_url"
        ]
        image = Image.open(io.BytesIO(base64.b64decode(images[0].split(",", 1)[1])))
        assert image.width * image.height <= 1_000_000
        assert "Submit files from /workspace" in str(requests[3]["messages"])
        assert "timeout" in str(requests[4]["messages"][-2:]).lower()
        info = json.loads((trajectory.parent / "result.json").read_text())["metadata"]
        assert info["abandoned"] is True and info["submitted_files"] == {}
    if case == "duplicate_ids":
        assert "model gateway returned duplicate tool call IDs" in result["errors"][0]["message"]
        (step,) = steps
        assert step["extra"]["duplicate_tool_call_ids"] == ["code_exec"]
        assert [c["extra"]["provider_tool_call_id"] for c in step["tool_calls"]] == [
            "code_exec"
        ] * 2
        assert "observation" not in step
    if case in ("unavailable", "rate_limit"):
        # The HTTP client can also retry inside a single LiteLLM call.
        assert len(requests) >= len(steps) >= 1
        assert all(req == requests[0] for req in requests)
        requests = [requests[0]] * len(steps)
    assert len(steps) == len(requests) > 0
    assert sum(s["source"] == "user" for s in doc["steps"]) == 1
    assert ("fetch_web_page" in {t["function"]["name"] for t in requests[0]["tools"]}) == (
        case == "success"
    )
    if case == "rate_limit":
        assert len(steps) == 3
        assert all(step["extra"]["retry_of"] == steps[0]["extra"]["call_id"] for step in steps[1:])
    for step, req in zip(steps, requests, strict=True):
        context = step["extra"]["context"]
        recorded = json.loads((trajectory.parent / context["path"]).read_text())
        assert recorded["messages"] == req["messages"] and context["complete"]
    if case == "summary_after_overflow" and doc["agent"]["version"] == "0.2.0":
        # This version drops the rejected turn and retries without a summary request.
        assert len(requests[3]["messages"]) < len(requests[2]["messages"])
        assert all(s["extra"].get("kind") != "summary" for s in steps)
    elif case in ("summary", "summary_after_overflow"):
        (summary_step,) = [s for s in steps if s["extra"].get("kind") == "summary"]
        assert summary_step["extra"]["trigger"] == (
            "token_threshold" if case == "summary" else "token_overflow"
        )
        kinds = {s["extra"].get("kind") for s in doc["steps"] if s["source"] == "system"}
        assert {"summary_prompt", "summary_bridge", "summary_ack"} <= kinds
    if case == "missing_intermediate_id":
        assert "nonempty ID" in result["errors"][0]["message"]
        assert (
            steps[0]["tool_calls"][0]["tool_call_id"]
            == steps[0]["observation"]["results"][0]["source_call_id"]
        )
    if case in ("success", "missing_finish_id"):
        for step in steps:
            (call,) = step["tool_calls"]
            (observation,) = step["observation"]["results"]
            assert call["tool_call_id"] == observation["source_call_id"]
        assert steps[-1]["observation"]["results"][0]["content"] == [
            {"type": "text", "text": "Copied the file."}
        ]
