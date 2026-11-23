import json

import pytest

from envweaver import AgentOutput, ClawEvalAgent, Runtime
from envweaver.agents.adapters.claw_eval.trajectory import convert
from envweaver.trajectory.atif import validate_trajectory


def test_prepare_requires_eval_to_own_the_tool_container():
    with pytest.raises(ValueError, match="Eval-managed"):
        ClawEvalAgent(model={}).prepare(runtime=Runtime(), instance=None, output=None, cwd="/")


@pytest.mark.parametrize("options, expected_cache", [({}, True), ({"cache": False}, False)])
def test_claw_native_config_roundtrip(monkeypatch, options, expected_cache):
    model = {"model_id": "local-model", "api_key": "model-secret", "extra_body": {"thinking": True}}
    agent = ClawEvalAgent(model=model, **options)
    clone = agent.new()
    assert clone.config["cache"] is expected_cache
    assert clone._identity() == agent._identity()
    assert clone.metadata["model"] == "local-model"
    model["extra_body"].clear()
    assert clone.config["model"]["extra_body"] == {"thinking": True}
    assert clone._redact("model-secret") == "[REDACTED]"
    captured = command_input(monkeypatch, clone)
    assert captured == {
        "prompt": "book the meeting",
        "images": [],
        "model": clone.config["model"],
    }


def command_input(monkeypatch, agent):
    captured = {}

    def upload(name, text):
        captured.update(json.loads(text))
        return "/sandbox/" + name

    monkeypatch.setattr(agent, "_upload_text", upload)
    agent._python, agent._remote = "python", "/sandbox"
    agent._command("book the meeting", [])
    return captured


def test_claw_leaderboard_defaults(monkeypatch):
    agent = ClawEvalAgent(model={"model": "actor"})
    assert command_input(monkeypatch, agent)["model"] == {
        "model_id": "actor",
        "extra_body": {"reasoning": {"effort": "high"}},
    }

    agent = ClawEvalAgent(
        model={"reasoning_effort": "low", "context_window": 32768},
    )
    assert command_input(monkeypatch, agent)["model"] == {
        "context_window": 32768,
        "reasoning_effort": "low",
    }


def test_flat_actor_settings(monkeypatch):
    settings = {
        "model": "actor",
        "base_url": "http://gateway:8000/v1",
        "api_key": "test-key",
        "temperature": 0.3,
        "top_p": 0.9,
        "top_k": 20,
        "reasoning_effort": "medium",
    }
    agent = ClawEvalAgent(model=settings).new()
    assert agent.metadata["model"] == "actor"
    assert command_input(monkeypatch, agent)["model"] == {
        "model_id": "actor",
        "base_url": "http://gateway:8000/v1",
        "api_key": "test-key",
        "temperature": 0.3,
        "reasoning_effort": "medium",
        "extra_body": {"top_p": 0.9, "top_k": 20},
    }
    assert agent.config["model"] == settings

    agent = ClawEvalAgent(model={"model": "actor", "top_k": 0})
    assert command_input(monkeypatch, agent)["model"]["extra_body"] == {
        "reasoning": {"effort": "high"},
        "top_k": 0,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"model": "local-model"},
        {"model": None},
        {"model": {}, "cache": "false"},
        {"model": {}, "prompt": []},
        {"model": {}, "media": False},
        {"model": {}, "user_agent_model": {"model_id": "unused"}},
        {"config_file": "general.yaml"},
    ],
)
def test_claw_requires_native_config_dicts(kwargs):
    with pytest.raises(TypeError):
        ClawEvalAgent(**kwargs)


def test_trace_preserves_retries_tool_results_and_final_reply(tmp_path):
    output = AgentOutput(tmp_path, {"agent": "claw-eval", "agent_version": "1", "model": None})

    def event(n, kind, **fields):
        return dict(timestamp=f"2026-11-22T10:00:0{n}Z", type=kind, **fields)

    def call(n, kind, **fields):
        return dict(at=f"2026-11-22T10:00:0{n}+00:00", kind=kind, **fields)

    req = {"model": "qwen", "messages": [{"role": "system", "content": "Use tools"}]}
    tool = {"type": "tool_use", "id": "x", "name": "calendar", "input": {"hour": 15}}
    answer = {"role": "assistant", "content": [{"type": "text", "text": "Booked."}]}
    result = {
        "type": "tool_result",
        "tool_use_id": "x",
        "is_error": False,
        "content": [{"type": "text", "text": "created"}],
    }
    usage = {"input_tokens": 5, "output_tokens": 2}
    events = [
        event(0, "trace_start", model="qwen", trace_id="trial"),
        event(
            1, "message", message={"role": "user", "content": [{"type": "text", "text": "book"}]}
        ),
        event(5, "message", message={"role": "user", "content": [result]}),
        event(8, "message", message=answer),
        event(9, "trace_end", model_input_tokens=10, model_output_tokens=4),
    ]
    calls = [
        call(2, "request", body=req),
        call(3, "request", body=req),
        call(4, "response", message={"role": "assistant", "content": [tool]}, usage=usage),
        call(6, "request", body=req | {"messages": [{"role": "tool", "content": "created"}]}),
        call(7, "response", message=answer, usage=usage),
    ]
    doc, reply = convert(events, calls, output)
    validate_trajectory(doc)
    _, _, retried, action, final = doc["steps"]
    assert retried["extra"]["response_status"] == "retried"
    assert (
        action["observation"]["results"][0]["source_call_id"]
        == action["tool_calls"][0]["tool_call_id"]
    )
    assert reply == final["message"] == answer["content"]
    assert (
        json.loads((tmp_path / final["extra"]["context"]["path"]).read_text()) == calls[3]["body"]
    )
    partial, _ = convert(events[:2], calls[:1], output)
    validate_trajectory(partial)
    assert partial["steps"][-1]["extra"]["response_status"] == "pending"


@pytest.mark.parametrize(
    "status, code, message, expected",
    [
        (400, "context_length_exceeded", "opaque", "context_limit"),
        (
            400,
            400,
            "This model's maximum context length is 262144 tokens. However, "
            "you requested 0 output tokens and your prompt contains at least 262145 input tokens, "
            "for a total of at least 262145 tokens.",
            "context_limit",
        ),
        (400, None, "Unsupported parameter: context_window", "configuration"),
        (401, None, "prompt is too long", "authentication"),
        (429, None, "prompt is too long", "provider_transient"),
        (503, None, "prompt is too long", "provider_transient"),
    ],
)
def test_claw_provider_stop_reason(status, code, message, expected):
    # The native harness and its OpenAI client are installed in the Claw runner image.
    pytest.importorskip("claw_eval")
    import httpx
    from openai import OpenAI

    from envweaver.agents.adapters.claw_eval.runner import _error_reason

    response = httpx.Response(
        status,
        json={"error": {"code": code, "message": message}},
        request=httpx.Request("POST", "http://localhost/v1/chat/completions"),
    )
    with OpenAI(api_key="offline-test") as client:
        error = client._make_status_error_from_response(response)
    assert _error_reason(error) == expected
    wrapped = RuntimeError("Model endpoint rejected multimodal input")
    wrapped.__cause__ = error
    assert _error_reason(wrapped) == expected
