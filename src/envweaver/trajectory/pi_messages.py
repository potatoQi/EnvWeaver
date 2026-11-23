"""Native Pi message format shared by Pi and OpenClaw."""

from .atif import add_step


def content(value, media):
    if isinstance(value, str):
        return value
    parts = []
    for block in value or []:
        if block["type"] == "text":
            parts.append({"type": "text", "text": block["text"]})
        elif block["type"] == "image":
            parts.append(media.image("data:" + block["mimeType"] + ";base64," + block["data"]))
    return parts or ""


def add_message(doc, data, media, calls, step=None):
    role = data.get("role")
    if role in ("user", "system"):
        if role == "user" and (data.get("provenance") or {}).get("kind") in (
            "internal_system",
            "inter_session",
        ):
            role = "system"
        add_step(doc, role, content(data.get("content"), media), extra={"native_message": data})
    elif role == "assistant":
        if step is None:
            step = add_step(
                doc,
                "agent",
                "",
                extra={"context": {"complete": False, "missing": ["provider_request"]}},
            )
        step["message"] = content(data.get("content"), media)
        step["extra"].update(response_status=data.get("stopReason"), native_message=data)
        thinking = []
        for block in data.get("content") or []:
            if block.get("type") == "thinking":
                thinking.append(block.get("thinking", ""))
            elif block.get("type") == "toolCall":
                step.setdefault("tool_calls", []).append(
                    {
                        "tool_call_id": block["id"],
                        "function_name": block["name"],
                        "arguments": block["arguments"],
                    }
                )
                calls[block["id"]] = step
        if thinking:
            step["reasoning_content"] = "\n".join(thinking)
        usage = data.get("usage") or {}
        if usage and (usage.get("usageReport") or {}).get("state") != "unavailable":
            step["metrics"] = {
                "prompt_tokens": sum(usage.get(k, 0) for k in ("input", "cacheRead", "cacheWrite")),
                "completion_tokens": usage.get("output", 0),
                "cached_tokens": usage.get("cacheRead", 0),
            }
    elif role == "toolResult":
        call_id = data.get("toolCallId")
        result = {
            "content": content(data.get("content"), media),
            "extra": {"is_error": data.get("isError", False)},
        }
        if call_id in calls:
            result["source_call_id"] = call_id
            calls[call_id].setdefault("observation", {"results": []})["results"].append(result)
        else:
            add_step(
                doc,
                "system",
                "",
                observation={"results": [result]},
                extra={"unresolved_tool_call_id": call_id},
            )
