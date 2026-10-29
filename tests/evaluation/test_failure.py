import pytest

from envweaver import AgentStopReason, ErrorType
from envweaver.errors import AgentError
from envweaver.evaluation.failure import StageTimeout, can_retry, classify, stop_reason


@pytest.mark.parametrize("error_type", list(ErrorType))
@pytest.mark.parametrize("message", ["context length exceeded, HTTP 401", "HTTP 503, connection reset"])
def test_policy_uses_registered_type_not_native_text(error_type, message):
    error = AgentError(type=error_type, message=message)
    assert classify(error, "agent") is error_type
    expected = AgentStopReason(error_type.value) if error_type.is_limit or error_type is ErrorType.CANCELLED else AgentStopReason.ERROR
    assert stop_reason(error) is expected


@pytest.mark.parametrize("stage", ["preflight", "setup", "agent", "verify", "cleanup"])
@pytest.mark.parametrize("error_type", list(ErrorType))
def test_retry_policy_uses_type_and_stage(stage, error_type):
    record = {"stage": stage, "type": error_type.value, "message": "arbitrary"}
    expected = stage not in ("verify", "cleanup") and error_type in (ErrorType.PROVIDER_TRANSIENT, ErrorType.TRANSPORT)
    assert can_retry(record) is expected


@pytest.mark.parametrize("stage", ["preflight", "setup", "agent", "verify", "cleanup"])
def test_operation_timeout_and_stage_deadline_are_distinct(stage):
    assert classify(TimeoutError(), stage) is ErrorType.TRANSPORT
    assert classify(StageTimeout(), stage) is ErrorType.TIMEOUT
    assert not can_retry({"stage": stage, "type": ErrorType.TIMEOUT})
    assert stop_reason(TimeoutError()) is AgentStopReason.ERROR
    assert stop_reason(StageTimeout()) is AgentStopReason.TIMEOUT


@pytest.mark.parametrize("error,stage,expected", [
    (ValueError(), "agent", ErrorType.CONFIGURATION),
    (PermissionError(), "setup", ErrorType.CONFIGURATION),
    (KeyboardInterrupt(), "agent", ErrorType.CANCELLED),
    (InterruptedError(), "verify", ErrorType.CANCELLED),
    (OSError(), "verify", ErrorType.VERIFIER),
    (OSError(), "setup", ErrorType.SANDBOX),
    (OSError(), "cleanup", ErrorType.UNKNOWN),
    (ConnectionResetError(), "setup", ErrorType.TRANSPORT),
])
def test_builtin_exception_mapping(error, stage, expected):
    assert classify(error, stage) is expected
