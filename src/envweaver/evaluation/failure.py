"""Normalize exceptions once; execution policy uses only stage and ErrorType."""

from ..errors import AgentError, AgentStopReason, ErrorType
from ..errors import RuntimeError as SandboxError


class StageTimeout(TimeoutError):
    """An Eval deadline expired, rather than an operation inside the worker."""


def classify(error, stage):
    if isinstance(error, (InterruptedError, KeyboardInterrupt)):
        return ErrorType.CANCELLED
    if isinstance(error, AgentError):
        return error.type
    if isinstance(error, StageTimeout):
        return ErrorType.TIMEOUT
    if isinstance(error, (ValueError, TypeError, FileNotFoundError, PermissionError)):
        return ErrorType.CONFIGURATION
    if isinstance(error, (ConnectionError, TimeoutError)):
        return ErrorType.TRANSPORT
    if stage == "verify":
        return ErrorType.VERIFIER
    if isinstance(error, SandboxError) or stage == "setup":
        return ErrorType.SANDBOX
    return ErrorType.UNKNOWN


def stop_reason(error):
    error_type = classify(error, "agent")
    if error_type.is_limit or error_type is ErrorType.CANCELLED:
        return AgentStopReason(error_type.value)
    return AgentStopReason.ERROR


def can_retry(record):
    return record["stage"] not in ("verify", "cleanup") and record["type"] in (
        ErrorType.PROVIDER_TRANSIENT, ErrorType.TRANSPORT,
    )
