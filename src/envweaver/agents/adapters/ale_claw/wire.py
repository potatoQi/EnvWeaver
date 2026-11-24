"""Capture AleClaw's asynchronous model HTTP exchanges in the isolated worker."""

import asyncio
import json
from contextlib import ExitStack, contextmanager
from functools import partialmethod
from uuid import uuid4

import httpx

try:
    import httpx2
except ImportError:
    httpx2 = None


@contextmanager
def capture(path):
    transports = (httpx, httpx2) if httpx2 else (httpx,)
    transport_errors = (asyncio.TimeoutError, *(m.RequestError for m in transports))

    def record(call_id, **fields):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"id": call_id, **fields}, ensure_ascii=False) + "\n")

    async def send(client, original, request, **kwargs):
        if request.method != "POST" or not request.url.path.rstrip("/").endswith(
            ("/chat/completions", "/responses", "/messages")
        ):
            return await original(client, request, **kwargs)
        try:
            payload = json.loads(await request.aread())
        except (UnicodeDecodeError, ValueError):
            return await original(client, request, **kwargs)
        if not isinstance(payload, dict) or not payload.get("model"):
            return await original(client, request, **kwargs)
        key = uuid4().hex
        record(key, request=payload)
        try:
            response = await original(client, request, **kwargs)
            # AleClaw normally requests JSON. Leave an explicitly streamed response to the SDK.
            if response.headers.get("content-type", "").startswith("text/event-stream"):
                record(key, status=response.status_code, response_missing="stream")
            else:
                await response.aread()
                record(key, status=response.status_code, body=response.text)
        except transport_errors as exc:
            record(key, transport_error=type(exc).__name__)
            raise
        return response

    with ExitStack() as restore:
        for cls in dict.fromkeys(m.AsyncClient for m in transports):
            original = cls.send
            restore.callback(setattr, cls, "send", original)
            cls.send = partialmethod(send, original)
        yield
