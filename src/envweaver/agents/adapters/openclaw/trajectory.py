"""Join the native OpenClaw transcript to captured provider requests."""

import json
from collections import Counter, defaultdict

from ....trajectory.atif import add_step, document
from ....trajectory.pi_messages import add_message
from ...capture import Media
from .._provider_http import events, reply_signature, response_id


def _matches(messages, calls, output, *, match_content):
    # Prefer a unique provider ID. A content match must be unique on both sides.
    ids = Counter(m.get("responseId") for m in messages if m.get("role") == "assistant")
    if not calls or not ids:
        return {}
    by_id, by_reply = defaultdict(list), defaultdict(list)
    for index, call in enumerate(calls):
        body = call.get("body", "")
        by_id[response_id(events(body))].append(index)
    unique_ids = {
        key: indices[0] for key, indices in by_id.items() if key and ids[key] == len(indices) == 1
    }
    if match_content and any(key not in unique_ids for key in ids):
        reserved = set(unique_ids.values())
        for index, call in enumerate(calls):
            if index in reserved or call.get("status") != 200:
                continue
            body = call.get("body", "")
            try:
                text, thinking, tools = reply_signature(events(body))
            except (AttributeError, KeyError, TypeError):
                # Unknown provider payloads stay archived without a content match.
                continue
            if thinking:
                by_reply[text, thinking, tools].append(index)
            # OpenClaw can omit thinking from the transcript while the provider returns it.
            if text or tools:
                by_reply[text, "", tools].append(index)
    matches = {}
    for i, m in enumerate(messages):
        if m.get("role") != "assistant":
            continue
        identity = m.get("responseId")
        if identity in unique_ids:
            matches[i] = unique_ids[identity]
            continue
        parts = m.get("content") or []
        if not by_reply or not isinstance(parts, list):
            continue
        signature = (
            "".join(p["text"] for p in parts if p["type"] == "text"),
            "".join(p["thinking"] for p in parts if p["type"] == "thinking"),
            tuple(p["id"] for p in parts if p["type"] == "toolCall"),
        )
        candidates = by_reply.get(signature, [])
        if identity:
            permitted = by_id.get(identity) or by_id.get(None, [])
            candidates = [c for c in candidates if c in permitted]
        if len(candidates) > 1 and i:
            candidates = [c for c in candidates if _follows(calls[c], messages[i - 1], output)]
        if len(candidates) == 1:
            matches[i] = candidates[0]
    uses = Counter(matches.values())
    return {i: calls[c] for i, c in matches.items() if uses[c] == 1}


def _follows(call, previous, output):
    # Ties can be resolved when the actual request ends at a distinct tool result or user turn.
    path = call["context"].get("path")
    if not path:
        return False
    history = json.loads((output.directory / path).read_text()).get("messages") or []
    if not history:
        return False
    last = history[-1]
    if previous.get("role") == "toolResult" and last.get("role") == "tool":
        return bool(last.get("tool_call_id")) and last["tool_call_id"] == previous.get("toolCallId")
    return (
        previous.get("role") == last.get("role") == "user"
        and bool(last.get("content"))
        and last["content"] == previous.get("content")
    )


def convert(session, messages, calls, envelope, output, *, multiple_sessions=False, match_content=True):
    doc = document(output.result["metadata"], session)
    media = Media(output)
    matches = _matches(messages, calls, output, match_content=match_content and not multiple_sessions)
    tools = {}
    for i, message in enumerate(messages):
        step = None
        failed = message.get("role") == "assistant" and message.get("stopReason") in (
            "error",
            "aborted",
        )
        if failed and not message.get("content"):
            add_step(
                doc,
                "system",
                message.get("errorMessage") or "OpenClaw request failed",
                extra={"native_message": message},
            )
            continue
        if message.get("role") == "assistant":
            call = matches.get(i)
            extra = (
                {k: call[k] for k in ("call_id", "context")}
                if call
                else {"context": {"complete": False, "missing": ["provider_request"]}}
            )
            step = add_step(doc, "agent", "", model_name=message.get("model"), extra=extra)
        add_message(doc, message, media, tools, step)
        if failed:
            step["extra"]["response_status"] = "incomplete"
    if not doc["steps"] and not calls and not envelope.get("final"):
        return None
    doc["extra"] = {"model_calls": calls}
    if not any(step["source"] == "agent" for step in doc["steps"]):
        # The CLI may stop before committing its first response. Preserve the
        # submitted goal and any observed requests without fabricating a response.
        if not doc["steps"]:
            initial = output.directory / "trajectory.json"
            if initial.exists():
                doc["steps"] = json.loads(initial.read_text())["steps"][:1]
        if envelope.get("ok") is True and envelope.get("final"):
            add_step(
                doc,
                "agent",
                envelope["final"],
                extra={"context": {"complete": False, "missing": ["native_messages"]}},
            )
        if not messages:
            doc["extra"]["capture"] = {"complete": False, "missing": ["native_messages"]}
    if multiple_sessions or any(
        t["function_name"] == "sessions_spawn"
        or (
            t["function_name"] == "dispatch_action"
            and t["arguments"].get("id") in ("sessions_spawn", "openclaw:core:sessions_spawn")
        )
        for step in doc["steps"]
        for t in step.get("tool_calls", [])
    ):
        capture = doc["extra"].setdefault("capture", {"complete": False, "missing": []})
        capture["missing"].append("subagent_sessions")
    return doc if doc["steps"] else None
