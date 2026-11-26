"""Sandbox-only recording of LiteLLM call inputs and responses. No training conversion."""

import contextvars
import functools
import inspect
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

if __package__:
    from ..._redaction import redact_data
else:
    from _redaction import redact_data

_LOCK = threading.Lock()
_ACTIVE = contextvars.ContextVar("envweaver_model_capture", default=False)


def install(path, *, request_metadata=None):
    import litellm

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    secrets = [
        v
        for k, v in os.environ.items()
        if v and any(part in k.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    ]

    def serialize(value):
        if isinstance(value, type) and hasattr(value, "model_json_schema"):
            return value.model_json_schema()
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        return {"envweaver_unserializable_type": type(value).__qualname__}

    def redact(text):
        for secret in secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    def record(kind, call_id, value):
        event = {"type": kind, "call_id": call_id, "time": time.time(), "data": value}
        text = json.dumps(
            redact_data(json.loads(json.dumps(event, default=serialize, allow_nan=False)), redact),
            ensure_ascii=False,
        )
        with _LOCK, path.open("a", encoding="utf-8") as stream:
            stream.write(text + "\n")

    def wrap(function, name):
        @contextmanager
        def capture(args, kwargs):
            # Only transport credentials are excluded; tools and messages remain intact.
            payload = {
                k: v
                for k, v in kwargs.items()
                if k
                not in (
                    "api_key",
                    "headers",
                    "extra_headers",
                    "http_client",
                    "client",
                    "logging_obj",
                )
            }
            if args:
                bound = inspect.signature(function).bind_partial(*args, **kwargs)
                payload.update(
                    {
                        k: v
                        for k, v in bound.arguments.items()
                        if k in ("model", "messages", "input")
                    }
                )
            call_id = uuid.uuid4().hex
            metadata = request_metadata(call_id, payload) if request_metadata else {}
            record("request", call_id, {"api": name, "request": payload, **metadata})
            token = _ACTIVE.set(True)
            try:
                yield call_id
            except BaseException as error:
                record("error", call_id, {"type": type(error).__name__, "message": str(error)})
                raise
            finally:
                _ACTIVE.reset(token)

        if inspect.iscoroutinefunction(function):

            @functools.wraps(function)
            async def asynchronous(*args, **kwargs):
                if _ACTIVE.get():
                    return await function(*args, **kwargs)
                with capture(args, kwargs) as call_id:
                    result = await function(*args, **kwargs)
                record("response", call_id, result)
                return result

            return asynchronous

        @functools.wraps(function)
        def synchronous(*args, **kwargs):
            if _ACTIVE.get():
                return function(*args, **kwargs)
            with capture(args, kwargs) as call_id:
                result = function(*args, **kwargs)
            record("response", call_id, result)
            return result

        return synchronous

    for name in ("completion", "acompletion", "responses", "aresponses"):
        function = getattr(litellm, name, None)
        if function is not None:
            setattr(litellm, name, wrap(function, name))
