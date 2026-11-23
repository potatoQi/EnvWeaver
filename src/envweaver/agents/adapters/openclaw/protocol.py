"""Read provider responses recorded at OpenClaw's HTTP boundary."""

import json

from ....errors import ErrorType
from .._provider_errors import context_limit_message, provider_error_type


def events(body):
    try:
        value = json.loads(body)
    except ValueError:
        for line in body.splitlines():
            if line.startswith("data:"):
                try:
                    value = json.loads(line[5:])
                except ValueError:
                    continue
                if isinstance(value, dict):
                    yield value
    else:
        if isinstance(value, dict):
            yield value


def response_id(body):
    for event in events(body):
        record = event.get("message") or event.get("response") or event
        if isinstance(record, dict) and isinstance(identity := record.get("id"), str) and identity:
            return identity
    return None


def provider_failure(call):
    """Return a category only when the wire response provides failure evidence."""
    status = call.get("status")
    if status in (401, 403):
        return ErrorType.AUTHENTICATION
    if status == 408 or isinstance(status, int) and 500 <= status < 600:
        return ErrorType.PROVIDER_TRANSIENT
    body = call.get("body", "")
    if status == 400 and context_limit_message(body) and not body.lstrip().startswith(("{", "[")):
        return ErrorType.CONTEXT_LIMIT
    started = finished = False
    for event in events(body):
        response = event.get("response")
        error = event.get("error") or (
            response.get("error") if isinstance(response, dict) else None
        )
        if error is None and event.get("type") == "error":
            error = event
        if status == 400 and context_limit_message(
            error if isinstance(error, str) else event.get("message")
        ):
            return ErrorType.CONTEXT_LIMIT
        if isinstance(error, dict):
            code = error.get("code") or error.get("type")
            if status in (400, 200) and (
                code == "context_length_exceeded" or context_limit_message(error.get("message"))
            ):
                return ErrorType.CONTEXT_LIMIT
            if reason := provider_error_type(code):
                return reason
            if status == 429:
                return ErrorType.PROVIDER_TRANSIENT
            if code in ("overloaded_error", "server_error", "api_error", "rate_limit_error"):
                return ErrorType.PROVIDER_TRANSIENT
            if code in ("authentication_error", "permission_error"):
                return ErrorType.AUTHENTICATION
            if code == "invalid_request_error":
                return ErrorType.CONFIGURATION
        if event.get("object") == "chat.completion.chunk":
            started = True
            finished |= any(
                isinstance(choice, dict) and choice.get("finish_reason") is not None
                for choice in event.get("choices") or []
            )
        if event.get("type") in ("message_start", "response.created"):
            started = True
        if event.get("type") in ("message_stop", "response.completed", "response.incomplete"):
            finished = True
    if status == 429:
        return ErrorType.PROVIDER_TRANSIENT
    if isinstance(status, int) and 400 <= status < 500:
        return ErrorType.CONFIGURATION
    if (started and not finished) or call.get("transport_error"):
        return ErrorType.PROVIDER_TRANSIENT
    return None


def repeated_outage(calls):
    """Only attribute a deadline to the provider when every observed call confirms it."""
    return len(calls) >= 2 and all(
        (c.get("status") == 429 or 500 <= (c.get("status") or 0) < 600)
        and provider_failure(c) == ErrorType.PROVIDER_TRANSIENT
        for c in calls
    )


def reply_signature(body):
    """Visible text, thinking and tool identities, independent of the response ID."""
    text, thinking, tools = [], [], []
    for event in events(body):
        # Messages streams emit content blocks; Responses supplies the assembled
        # output on its terminal event. Ignore its deltas to avoid double counting.
        if event.get("type") in ("response.completed", "response.incomplete"):
            event = event.get("response") or {}
        for item in event.get("output") or []:
            if item.get("type") == "function_call":
                tools.append(item.get("call_id"))
            for block in item.get("content") or []:
                if block.get("type") == "output_text":
                    text.append(block["text"])
        blocks = event.get("content") or []
        if event.get("type") == "content_block_start":
            blocks = [event["content_block"]]
        elif event.get("type") == "content_block_delta":
            blocks = [event["delta"]]
        for block in blocks if isinstance(blocks, list) else ():
            kind = block.get("type")
            if kind in ("text", "text_delta"):
                text.append(block["text"])
            elif kind in ("thinking", "thinking_delta"):
                thinking.append(block["thinking"])
            elif kind == "tool_use":
                tools.append(block["id"])
        for choice in event.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue
            part = choice.get("delta") or choice.get("message") or {}
            if isinstance(part.get("content"), str):
                text.append(part["content"])
            if value := part.get("reasoning_content") or part.get("reasoning"):
                thinking.append(value)
            tools.extend(t["id"] for t in part.get("tool_calls") or [] if t.get("id"))
    return "".join(text), "".join(thinking), tuple(tools)
