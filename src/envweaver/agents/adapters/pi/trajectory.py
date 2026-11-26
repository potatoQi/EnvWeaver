"""Pi provider-boundary capture and native message events."""

from ....trajectory.atif import add_step, context, document
from ...capture import Media
from ...capture.pi_messages import add_message


def convert(events, output):
    doc = document(output.result["metadata"], "pi")
    media = Media(output)
    pending, calls = {}, {}
    compactions = []
    compact_steps = {}
    transport_calls = {}
    active_compaction = None

    for event in events:
        data, kind = event["data"], event["type"]
        if kind == "http_request":
            transport_calls[event["call_id"]] = {
                "call_id": event["call_id"],
                "agent_call_id": data.get("agent_call_id"),
                "phase": data.get("phase"),
                **context(output, data["payload"], source="pi.fetch"),
                "response_status": "pending",
            }
        elif event["call_id"] in transport_calls:
            call = transport_calls[event["call_id"]]
            if kind == "http_response":
                call.update(
                    response_status="completed" if 200 <= data["status"] < 300 else "failed",
                    native_response=data,
                )
            elif kind in ("http_error", "capture_error"):
                call.update(response_status="failed", failure=data)
            if event["call_id"] in compact_steps:
                compact_steps[event["call_id"]]["extra"].update(
                    {
                        key: value
                        for key, value in call.items()
                        if key in ("response_status", "failure")
                    }
                )
        if kind == "compaction_start":
            active_compaction = document(
                output.result["metadata"], f"pi-compaction-{len(compactions) + 1}"
            )
            compactions.append(active_compaction)
        elif kind == "http_request" and data.get("phase") == "compaction":
            if active_compaction is None:
                continue
            extra = {
                "context": {
                    **transport_calls[event["call_id"]]["context"],
                    "source": "pi.fetch.compaction",
                },
                "call_id": event["call_id"],
                "response_status": "pending",
            }
            compact_steps[event["call_id"]] = add_step(active_compaction, "agent", "", extra=extra)
        elif kind == "http_response" and event["call_id"] in compact_steps:
            step = compact_steps[event["call_id"]]
            text, reasoning, recognized = _compaction_response(data)
            step["message"] = text
            if reasoning:
                step["reasoning_content"] = reasoning
            step["extra"]["native_response"] = data
            if not recognized:
                step["extra"]["response_conversion_missing"] = True
        elif kind == "http_request" and data.get("agent_call_id") in pending:
            # Prefer the HTTP payload over the earlier hook, which other extensions may modify.
            pending[data["agent_call_id"]]["extra"]["context"] = transport_calls[
                event["call_id"]
            ]["context"]
        elif kind == "request":
            payload = data["payload"]
            if (
                doc["steps"]
                and doc["steps"][0]["source"] == "system"
                and not doc["steps"][0]["message"]
            ):
                messages = payload.get("messages", [])
                if messages and messages[0].get("role") == "system":
                    doc["steps"][0]["message"] = messages[0].get("content") or ""
            extra = context(output, payload, source="pi.before_provider_request")
            extra.update(call_id=event["call_id"], response_status="pending")
            model = data.get("model") or {}
            pending[event["call_id"]] = add_step(
                doc,
                "agent",
                "",
                model_name=model.get("id"),
                timestamp=event.get("time"),
                extra=extra,
            )
        elif kind == "message":
            add_message(doc, data, media, calls, pending.get(event["call_id"]))
        elif kind == "compaction":
            extra = {"pi_compaction": data}
            if active_compaction is not None and active_compaction["steps"]:
                add_step(
                    doc,
                    "system",
                    "",
                    extra=extra,
                    observation={
                        "results": [
                            {
                                "content": data.get("compactionEntry", {}).get("summary", ""),
                                "subagent_trajectory_ref": [
                                    {"trajectory_id": active_compaction["trajectory_id"]}
                                ],
                            }
                        ]
                    },
                )
            else:
                extra["context"] = {"complete": False, "missing": ["compaction_provider_request"]}
                add_step(doc, "system", "", extra=extra)
            active_compaction = None
    children = [child for child in compactions if child["steps"]]
    if children:
        doc["subagent_trajectories"] = children
    doc["extra"] = {"media_missing": media.missing, "model_calls": list(transport_calls.values())}
    return doc if doc["steps"] else None


def _compaction_response(data):
    """Decode model text from Pi's native HTTP response; keep unknown bodies verbatim."""
    import json

    body = data.get("body", "")
    try:
        chunks = [json.loads(body)]
    except ValueError:
        chunks = []
        for line in body.split("\n"):
            if line.startswith("data:") and line[5:].strip() != "[DONE]":
                try:
                    chunks.append(json.loads(line[5:]))
                except ValueError:
                    pass
    text, reasoning = [], []
    recognized = False
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        if "choices" in chunk:
            recognized = True
            for choice in chunk["choices"]:
                message = choice.get("delta") or choice.get("message") or {}
                if isinstance(message.get("content"), str):
                    text.append(message["content"])
                if message.get("reasoning_content"):
                    reasoning.append(message["reasoning_content"])
        elif chunk.get("type") == "content_block_delta":
            recognized = True
            delta = chunk["delta"]
            if delta.get("text"):
                text.append(delta["text"])
            if delta.get("thinking"):
                reasoning.append(delta["thinking"])
        elif chunk.get("type") == "response.output_text.delta":
            recognized = True
            text.append(chunk["delta"])
        elif "candidates" in chunk:
            recognized = True
            for candidate in chunk["candidates"]:
                for part in candidate.get("content", {}).get("parts", []):
                    if "text" in part:
                        (reasoning if part.get("thought") else text).append(part["text"])
    return "".join(text), "".join(reasoning), recognized
