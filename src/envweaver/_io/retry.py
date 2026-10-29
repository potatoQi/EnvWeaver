"""Bounded recovery for transport operations, without replaying arbitrary commands."""

import re
import subprocess
import time
from contextvars import ContextVar

from ..errors import RuntimeError

_active = ContextVar("envweaver_transport_retry", default=False)


class ConnectionFailure(RuntimeError, ConnectionError):
    def __init__(self, message, *, before_dispatch=False):
        super().__init__(message)
        self.before_dispatch = before_dispatch


def connection_error(message):
    """Interpret transport diagnostics only, never command output or model messages."""
    text = message.lower()
    if any(value in text for value in (
        "permission denied", "host key verification failed", "remote host identification has changed",
    )):
        return RuntimeError(message)
    before_dispatch = bool(re.search(
        r"ssh: connect to host .*: (connection refused|connection timed out|no route to host|network is unreachable)",
        text,
    ))
    if before_dispatch or any(value in text for value in (
        "connection reset", "connection closed", "broken pipe", "connection timed out",
        "connection refused", "network is unreachable", "no route to host",
        "temporary failure in name resolution",
    )):
        return ConnectionFailure(message, before_dispatch=before_dispatch)
    return RuntimeError(message)


def retry(operation, *, replay=False, cancelled=None):
    """Try up to three times; nested calls share the outer recovery budget."""
    if _active.get():
        return operation()
    token = _active.set(True)
    try:
        for attempt in range(3):
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("Transport recovery cancelled")
            try:
                return operation()
            except (ConnectionError, TimeoutError, subprocess.TimeoutExpired) as error:
                if attempt == 2 or not (replay or getattr(error, "before_dispatch", False)):
                    raise
                delay = 0.5 * 2 ** attempt
                if cancelled is None:
                    time.sleep(delay)
                elif cancelled.wait(delay):
                    raise InterruptedError("Transport recovery cancelled") from error
    finally:
        _active.reset(token)
