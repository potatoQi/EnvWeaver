import re

from ...errors import ErrorType

_CODE_TYPES = {
    "invalid_api_key": ErrorType.AUTHENTICATION,
    "insufficient_quota": ErrorType.CONFIGURATION,
    "model_not_found": ErrorType.CONFIGURATION,
    "rate_limit_exceeded": ErrorType.PROVIDER_TRANSIENT,
}


def provider_error_type(code):
    return _CODE_TYPES.get(code) if isinstance(code, str) else None


def context_limit_message(message):
    """Recognize explicit overflow in provider error messages."""
    return isinstance(message, str) and bool(re.search(
        r"context[_ ]length[_ ]exceeded|prompt (?:is )?too long"
        r"|prompt exceeds max length|exceeds the context window"
        r"|this model's maximum context length is \d+ tokens\. however, .*?"
        r"please reduce the length of the input prompt",
        message, re.IGNORECASE | re.DOTALL,
    ))
