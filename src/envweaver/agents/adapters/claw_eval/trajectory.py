"""Normalize Claw messages, actual model inputs and tool results into ATIF."""

import base64
import mimetypes
from datetime import datetime

from ....trajectory.atif import add_step, context, document
from ...capture import Media


def convert(events, calls, output):
    start = events[0]
    doc = document(dict(output.result["metadata"], model=start["model"]), start["trace_id"])
    media = Media(output)
    owners = {}
    active = None
    reply = []
    first = next((c["body"] for c in calls if c["kind"] == "request"), {})
    if first.get("tools"):
        doc["agent"]["tool_definitions"] = first["tools"]
    for msg in first.get("messages", []):
        if msg["role"] == "system":
            add_step(doc, "system", msg["content"])

    def content(blocks):
        parts = []
        for block in blocks:
            kind = block["type"]
            if kind == "text":
                parts.append({"type": "text", "text": block["text"]})
            elif kind == "image":
                parts.append(media.image(f"data:{block['mime_type']};base64,{block['data']}"))
            elif kind in ("audio", "video"):
                mime = block["mime_type"]
                mime = {"audio/x-wav": "audio/wav", "audio/mp3": "audio/mpeg"}.get(mime, mime)
                path = output.save_file(
                    base64.b64decode(block["data"]), mimetypes.guess_extension(mime) or ".bin"
                )
                if kind == "audio" and mime in {
                    "audio/wav",
                    "audio/mpeg",
                    "audio/mp4",
                    "audio/aac",
                    "audio/ogg",
                    "audio/flac",
                    "audio/webm",
                    "audio/aiff",
                }:
                    parts.append({"type": "audio", "source": {"path": path, "media_type": mime}})
                else:
                    # ATIF 1.8 has no video content part.
                    parts.append({"type": "text", "text": f"[{mime}: {path}]"})
        return parts or ""

    # The upstream trace includes user/observer messages but omits model inputs
    # and auxiliary calls used for context compression.
    timeline = [
        (e["timestamp"], e["type"], e) for e in events if e["type"] in ("message", "trace_end")
    ]
    timeline.extend((c["at"], c["kind"], c) for c in calls)
    for at, kind, event in sorted(timeline, key=lambda item: datetime.fromisoformat(item[0])):
        if kind == "request":
            req = event["body"]
            if active:
                active["extra"]["response_status"] = "retried"
            active = add_step(
                doc,
                "agent",
                "",
                timestamp=at,
                model_name=req["model"],
                extra=context(output, req, source="claw.openai.chat.completions"),
            )
            active["extra"]["response_status"] = "pending"
        elif kind == "response" and active:
            msg = event["message"]
            active["message"] = content(msg["content"])
            if msg.get("reasoning_content"):
                active["reasoning_content"] = msg["reasoning_content"]
            for block in msg["content"]:
                if block["type"] == "tool_use":
                    active.setdefault("tool_calls", []).append(
                        {
                            "tool_call_id": block["id"],
                            "function_name": block["name"],
                            "arguments": block["input"],
                        }
                    )
                    owners[block["id"]] = active
            usage = event["usage"]
            active["metrics"] = {
                "prompt_tokens": usage["input_tokens"],
                "completion_tokens": usage["output_tokens"],
            }
            active["extra"]["response_status"] = "completed"
            active = None
        elif kind == "message":
            msg = event["message"]
            if msg["role"] == "assistant":
                # Only main-loop replies are returned to the user.
                reply = (
                    []
                    if any(b["type"] == "tool_use" for b in msg["content"])
                    else [b for b in msg["content"] if b["type"] == "text"]
                )
                continue
            for block in msg["content"]:
                if block["type"] == "tool_result":
                    owner = owners.get(block["tool_use_id"])
                    if owner is not None:
                        owner.setdefault("observation", {"results": []})["results"].append(
                            {
                                "source_call_id": block["tool_use_id"],
                                "content": content(block["content"]),
                                "extra": {"is_error": block["is_error"]},
                            }
                        )
            if parts := content(msg["content"]):
                add_step(doc, msg["role"], parts, timestamp=at)
        elif kind == "trace_end":
            doc["final_metrics"] = {
                "total_prompt_tokens": event["model_input_tokens"],
                "total_completion_tokens": event["model_output_tokens"],
            }
            if active and event.get("failure_modes"):
                active["extra"]["response_status"] = "failed"
    return doc, reply
