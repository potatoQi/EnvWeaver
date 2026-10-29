"""Each adapter owns its native error format; Eval only receives typed errors."""

import json
from types import SimpleNamespace

import pytest

from envweaver import (
    CodexAgent,
    ErrorType,
    Instance,
    MiniSweAgent,
    PiAgent,
    Runtime,
)
from envweaver.agents.adapters.codex import adapter as codex
from envweaver.agents.adapters.mini_swe import adapter as mini_swe
from envweaver.agents.adapters.pi import adapter as pi
from envweaver.agents.execution import AgentExecution
from envweaver.errors import AgentError


def assert_error(error, expected):
    assert isinstance(error, AgentError)
    assert error.type is expected


@pytest.mark.parametrize("message,expected", [
    ('{"error":{"code":"context_length_exceeded","message":"opaque"}}', ErrorType.CONTEXT_LIMIT),
    ('{"error":{"code":"invalid_api_key","message":"context length exceeded"}}', ErrorType.AUTHENTICATION),
    ('{"error":{"code":"insufficient_quota"}}', ErrorType.CONFIGURATION),
    ('{"error":{"code":"model_not_found"}}', ErrorType.CONFIGURATION),
    ('{"error":{"code":"rate_limit_exceeded"}}', ErrorType.PROVIDER_TRANSIENT),
    ("unexpected status 401 Unauthorized: injected", ErrorType.AUTHENTICATION),
    ("unexpected status 403 Forbidden: context length exceeded", ErrorType.AUTHENTICATION),
    ("exceeded retry limit, last status: 429 Too Many Requests", ErrorType.PROVIDER_TRANSIENT),
    ("unexpected status 503 Service Unavailable", ErrorType.PROVIDER_TRANSIENT),
    ("unexpected status 400 Bad Request", ErrorType.CONFIGURATION),
    ("Your input exceeds the context window of this model", ErrorType.CONTEXT_LIMIT),
    ("Codex ran out of room in the model's context window.", ErrorType.CONTEXT_LIMIT),
    ("stream disconnected before completion: transport error", ErrorType.PROVIDER_TRANSIENT),
    ("litellm.AuthenticationError: invalid key", ErrorType.UNKNOWN),
    ("request 401 ended for model 503", ErrorType.UNKNOWN),
    ('{"error":{"code":"unknown","message":"Invalid API key"}}', ErrorType.UNKNOWN),
    ('{"error":{"code":[]}}', ErrorType.UNKNOWN),
    (None, ErrorType.UNKNOWN),
])
def test_codex_error(message, expected):
    assert_error(codex._error(message), expected)


@pytest.mark.parametrize("message,expected", [
    ('400: {"code":"context_length_exceeded","message":"opaque"}', ErrorType.CONTEXT_LIMIT),
    ('400: {"message":"prompt is too long: 40000 tokens > 32768 maximum"}', ErrorType.CONTEXT_LIMIT),
    ('400: {"code":"1261","message":"Prompt exceeds max length"}', ErrorType.CONTEXT_LIMIT),
    ('400: {"message":"invalid model","debug":"prompt is too long"}', ErrorType.CONFIGURATION),
    ('401: {"message":"prompt is too long"}', ErrorType.AUTHENTICATION),
    ('429: {"message":"prompt is too long"}', ErrorType.PROVIDER_TRANSIENT),
    ('503: {"message":"prompt is too long"}', ErrorType.PROVIDER_TRANSIENT),
    ('404: {"message":"prompt is too long"}', ErrorType.CONFIGURATION),
    ('400: {"message":null}', ErrorType.CONFIGURATION),
    ('401: {"code":"invalid_api_key","message":"context length exceeded"}', ErrorType.AUTHENTICATION),
    ('429: {"code":"insufficient_quota"}', ErrorType.CONFIGURATION),
    ('429: {"code":"context_length_exceeded"}', ErrorType.PROVIDER_TRANSIENT),
    ('503: {"message":"opaque"}', ErrorType.PROVIDER_TRANSIENT),
    ('404: {"code":"model_not_found"}', ErrorType.CONFIGURATION),
    ("401 not-json", ErrorType.AUTHENTICATION),
    ("prompt is too long", ErrorType.CONTEXT_LIMIT),
    ("context_length_exceeded", ErrorType.CONTEXT_LIMIT),
    ("Rate limit: too many tokens", ErrorType.PROVIDER_TRANSIENT),
    ("Connection error.", ErrorType.PROVIDER_TRANSIENT),
    ("Request timed out.", ErrorType.PROVIDER_TRANSIENT),
    ("unexpected status 401 Unauthorized: injected", ErrorType.UNKNOWN),
    ("timeout must be positive", ErrorType.UNKNOWN),
    (None, ErrorType.UNKNOWN),
])
def test_pi_error(message, expected):
    assert_error(pi._error(message), expected)


@pytest.mark.parametrize("status,detail,expected", [
    ("AuthenticationError", "context length exceeded", ErrorType.AUTHENTICATION),
    ("PermissionDeniedError", "HTTP 503", ErrorType.AUTHENTICATION),
    ("ContextWindowExceededError", "invalid API key", ErrorType.CONTEXT_LIMIT),
    ("LimitsExceeded", "", ErrorType.OTHER_LIMIT),
    ("TimeExceeded", "", ErrorType.TIMEOUT),
    ("APIConnectionError", "opaque", ErrorType.PROVIDER_TRANSIENT),
    ("RateLimitError", "context length exceeded", ErrorType.PROVIDER_TRANSIENT),
    ("APITimeoutError", "", ErrorType.PROVIDER_TRANSIENT),
    ("InternalServerError", "", ErrorType.PROVIDER_TRANSIENT),
    ("NotFoundError", "", ErrorType.CONFIGURATION),
    ("BadRequestError", "timeout must be positive", ErrorType.CONFIGURATION),
    ("BadRequestError", "litellm.BadRequestError: OpenAIException - context length exceeded", ErrorType.CONTEXT_LIMIT),
    ("BadRequestError", "litellm.BadRequestError: OpenAIException - Invalid API key", ErrorType.AUTHENTICATION),
    ("UnrecognizedError", "Invalid API key, context length exceeded", ErrorType.UNKNOWN),
    ("UnrecognizedError", "unexpected status 401 Unauthorized", ErrorType.UNKNOWN),
])
def test_mini_error(status, detail, expected):
    assert_error(mini_swe._error(status, detail), expected)


@pytest.mark.parametrize("adapter", [CodexAgent, PiAgent, MiniSweAgent])
def test_successful_reply_is_never_classified_as_an_error(tmp_path, monkeypatch, adapter):
    text = "context_length_exceeded; Invalid API key; unexpected status 503"
    agent = adapter()
    agent_execution = AgentExecution(agent)
    monkeypatch.setattr(agent, "prepare", lambda **kwargs: setattr(agent, "_output", kwargs["output"]))
    monkeypatch.setattr(Runtime, "exec", lambda *args, **kwargs: (0, "", ""))
    agent_execution.prepare(runtime=Runtime(), instance=Instance("test", "local"),
                            output_dir=tmp_path, cwd=str(tmp_path))
    if adapter is CodexAgent:
        agent._on_event({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
        agent._on_event({"type": "turn.completed"})
    elif adapter is PiAgent:
        agent._on_event({"type": "message_end", "message": {
            "role": "assistant", "content": [{"type": "text", "text": text}], "stopReason": "stop",
        }})
        agent._on_event({"type": "agent_settled", "aborted": False})
    else:
        agent._remote = "/agent"
        agent._native_count = 0
        native = {"info": {"exit_status": "Submitted", "submission": text},
                  "messages": [{"role": "assistant", "content": text}]}
        monkeypatch.setattr(agent, "_read_remote", lambda path: json.dumps(native).encode())
        agent._native = SimpleNamespace(files={}, poll=lambda sources: False)
        agent._snapshot = None
        agent._poll_native()
    assert agent._completed and agent._run_error is None
    assert agent_execution.output.result["output"] == [{"type": "text", "text": text}]
