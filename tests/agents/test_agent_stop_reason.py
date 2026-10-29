import json
from types import SimpleNamespace

import pytest

from envweaver import AgentStopReason, ErrorType, Instance, MiniSweAgent, Runtime
from envweaver.agents.execution import AgentExecution
from envweaver.errors import AgentError


@pytest.mark.parametrize("reason,value,verify", [
    (AgentStopReason.COMPLETED, "completed", True),
    (AgentStopReason.TIMEOUT, "timeout", True),
    (AgentStopReason.CONTEXT_LIMIT, "context_limit", True),
    (AgentStopReason.STEP_LIMIT, "step_limit", True),
    (AgentStopReason.COST_LIMIT, "cost_limit", True),
    (AgentStopReason.TOKEN_LIMIT, "token_limit", True),
    (AgentStopReason.OTHER_LIMIT, "other_limit", True),
    (AgentStopReason.CANCELLED, "cancelled", False),
    (AgentStopReason.ERROR, "error", False),
])
def test_reason_json_contract_and_verification_policy(reason, value, verify):
    assert reason.should_verify is verify
    assert json.loads(json.dumps({"reason": reason})) == {"reason": value}
    assert AgentStopReason(value) is reason


@pytest.mark.parametrize("error_type", ["step_limit", "typo", None, 123, AgentStopReason.STEP_LIMIT])
def test_agent_error_requires_registered_type(error_type):
    with pytest.raises(TypeError, match="ErrorType"):
        AgentError(type=error_type)


def test_other_limit_preserves_provider_details():
    error = AgentError(
        type=ErrorType.OTHER_LIMIT,
        message="LimitsExceeded: native agent does not distinguish steps from cost",
    )
    assert error.type is ErrorType.OTHER_LIMIT
    assert str(error) == "LimitsExceeded: native agent does not distinguish steps from cost"


@pytest.mark.parametrize("status,detail,expected", [
    ("BadRequestError", "litellm.BadRequestError: OpenAIException - Invalid API key", ErrorType.AUTHENTICATION),
    ("BadRequestError", "litellm.BadRequestError: OpenAIException - context length exceeded", ErrorType.CONTEXT_LIMIT),
    ("APIConnectionError", "connection reset", ErrorType.PROVIDER_TRANSIENT),
    ("UnrecognizedError", "", ErrorType.UNKNOWN),
])
def test_mini_reads_error_from_exit_event(tmp_path, monkeypatch, status, detail, expected):
    agent = MiniSweAgent()
    agent_execution = AgentExecution(agent)
    agent._native_count = 0
    agent._remote = "/agent"
    monkeypatch.setattr(agent, "prepare", lambda **kwargs: setattr(agent, "_output", kwargs["output"]))
    monkeypatch.setattr(Runtime, "exec", lambda *args, **kwargs: (0, "", ""))
    agent_execution.prepare(runtime=Runtime(), instance=Instance("test", "local"),
                            output_dir=tmp_path / "agent", cwd=str(tmp_path))
    native = {
        "messages": [
            {"role": "assistant", "content": "Invalid API key, context length exceeded"},
            {"role": "exit", "content": detail, "extra": {"exception_str": detail}},
        ],
        "info": {"exit_status": status, "submission": ""},
    }
    monkeypatch.setattr(agent, "_read_remote", lambda path: json.dumps(native).encode())
    agent._native = SimpleNamespace(files={}, poll=lambda sources: False)
    agent._snapshot = None
    agent._poll_native()
    assert isinstance(agent._run_error, AgentError)
    assert agent._run_error.type is expected
