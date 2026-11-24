"""Export the main native ALE transcript. Provider calls are recorded independently."""

import json
from collections import Counter, defaultdict

from ....trajectory.atif import add_step, document
from ...capture import Media, json_lines
from .._provider_http import events, model_calls, reply_signature, response_id


def _link_calls(calls, steps):
    by_id, by_signature = defaultdict(list), defaultdict(list)
    for call in calls:
        if not (200 <= (call.get("status") or 0) < 300) or not call.get("body"):
            continue
        response_events = list(events(call["body"]))
        by_id[response_id(response_events)].append(call)
        try:
            text, _, ids = reply_signature(response_events)
        except (AttributeError, KeyError, TypeError):
            continue
        if ids or text:
            by_signature[(text, ids)].append(call)
        for event in response_events:
            response = event.get("response") or event
            if isinstance(response, dict) and isinstance(usage := response.get("usage"), dict):
                call["usage"] = usage
    matches = []
    for msg, step in steps:
        ids = tuple(t["tool_call_id"] for t in step.get("tool_calls", ()))
        identity = msg.get("responseId")
        candidates = by_id.get(identity, []) if identity else []
        if len(candidates) != 1:
            candidates = by_signature.get((step["message"], ids), [])
            if identity:
                permitted = by_id.get(identity) or by_id.get(None, [])
                candidates = [c for c in candidates if c in permitted]
        if len(candidates) == 1:
            matches.append((step, candidates[0]))
    counts = Counter(call["call_id"] for _, call in matches)
    for step, call in matches:
        if counts[call["call_id"]] == 1:
            step["extra"].update(context=call["context"], call_id=call["call_id"])


def read_calls(root, output, clean):
    path = root / "model.jsonl"
    rows = clean(json_lines(path.read_text())) if path.exists() else []
    return model_calls(rows, output, source="ale_claw.http")


def export(root, prompt, output, clean, requests):
    files = list(root.glob("openclaw_sessions/*/transcript.jsonl"))
    path = next(iter(files), None)
    if len(files) > 1:
        raise ValueError("Ambiguous ALE main session")
    doc = document(output.result["metadata"], path.parent.name if path else root.name)
    add_step(doc, "user", clean(prompt))
    calls = {}
    steps = []
    media = Media(output)
    final = ""
    missing = set() if path else {"transcript"}

    def image(source, mime):
        candidate = (root / source).resolve()
        if candidate.is_relative_to((root / "trajectories").resolve()) and candidate.is_file():
            return {
                "type": "image",
                "source": {
                    "path": output.save_file(
                        candidate.read_bytes(), ".png" if mime == "image/png" else ".img"
                    ),
                    "media_type": mime,
                },
            }
        missing.add("image")
        return {"type": "text", "text": "[Image unavailable]"}

    for record in json_lines(path.read_text()) if path else ():
        entry = clean(record)
        if entry["type"] == "compaction":
            add_step(doc, "system", entry.get("summary", ""), extra={"compaction": entry})
        msg = entry.get("message") or {}
        blocks = msg.get("content", [])
        if msg.get("role") == "user":
            add_step(
                doc,
                "user",
                "\n".join(b["text"] for b in blocks if b["type"] == "text"),
                timestamp=entry.get("timestamp"),
            )
        elif msg.get("role") == "assistant":
            final = "\n".join(b["text"] for b in blocks if b["type"] == "text")
            thinking = "\n".join(b["thinking"] for b in blocks if b["type"] == "thinking")
            step = add_step(
                doc,
                "agent",
                final,
                timestamp=entry.get("timestamp"),
                reasoning_content=thinking or None,
                extra={"context": {"complete": False, "missing": ["request_step_link"]}},
            )
            if msg.get("stopReason"):
                step["extra"]["stop_reason"] = msg["stopReason"]
            if msg.get("usage"):
                step["metrics"] = {
                    key: msg["usage"][field]
                    for key, field in (("prompt_tokens", "input"), ("completion_tokens", "output"))
                    if isinstance(msg["usage"].get(field), int)
                }
            for b in blocks:
                if b["type"] not in ("function_call", "computer_call"):
                    continue
                args = b.get("arguments", b.get("action", b.get("actions", {})))
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        pass
                step.setdefault("tool_calls", []).append(
                    {
                        "tool_call_id": b["id"],
                        "function_name": b.get("name", "computer"),
                        "arguments": args if isinstance(args, dict) else {"value": args},
                    }
                )
                calls[b["id"]] = step
            steps.append((msg, step))
        elif msg.get("role") == "tool":
            for b in blocks:
                call_id = b.get("tool_use_id")
                if call_id not in calls:
                    missing.add("tool_call")
                    continue
                value = b.get("content", "")
                parts = [
                    {"type": "text", "text": value if isinstance(value, str) else json.dumps(value)}
                ]
                images = b.get("images", [])
                # The native transcript can supply both an exact image and a
                # best-effort path to the last screenshot. Prefer the exact image.
                screenshot = isinstance(value, str) and (
                    value.endswith("_screenshot_after.png") or value == "image:trajectory"
                )
                if screenshot:
                    parts = [] if images else [image(value, "image/png")]
                for item in images:
                    mime = item["media_type"]
                    if item.get("data"):
                        parts.append(media.image(f"data:{mime};base64,{item['data']}"))
                    elif item.get("path"):
                        parts.append(image(item["path"], mime))
                calls[call_id].setdefault("observation", {"results": []})["results"].append(
                    {
                        "source_call_id": call_id,
                        "content": parts,
                    }
                )
    _link_calls(requests, steps)
    if path and (path.parent / "subagents").is_dir():
        missing.add("subagent_sessions")
    doc["extra"] = {"model_calls": requests, "missing": sorted(missing)}
    output.save_trajectory(doc)
    return final
