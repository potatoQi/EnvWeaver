"""Join Claude stream-json events to captured Messages requests by provider message ID."""

import json
from collections import defaultdict

from ....trajectory.atif import add_step, context, document
from ...capture import Media


def _blocks(value):
    return [{"type": "text", "text": value}] if isinstance(value, str) else value or []


def convert(events, records, metadata, prompt, output):
    doc = document(
        metadata, next((e["session_id"] for e in events if e.get("session_id")), "claude")
    )
    media = Media(output)
    calls, responses, requests = {}, {}, {}
    by_id, by_block = defaultdict(set), defaultdict(set)
    docs = {None: doc}
    parents, steps, owners = {}, {}, {}
    last_child_step = {}
    missing_finals = set()

    def signature(block):
        kind = block["type"]
        # The CLI sometimes omits signatures, usage and partial JSON metadata.
        field = {"text": "text", "thinking": "thinking", "tool_use": "id"}.get(kind)
        return (kind, block[field]) if field and field in block else None

    for record in records:
        key = record["id"]
        if "request" in record:
            request = requests[key] = record["request"]
            calls[key] = {
                "call_id": key,
                **context(output, request, source="claude.messages"),
                "response_status": "pending",
                "mapped_to_step": False,
            }
            continue
        if key not in calls:
            continue
        call = calls[key]
        status = record.get("status")
        if status:
            call["http_status"] = status
        if record.get("error") or (status is not None and status >= 400):
            call["response_status"] = "failed"
            call["error"] = record.get("error") or {}
        else:
            call["response_status"] = "completed" if record["complete"] else "incomplete"
        if msg := record.get("message"):
            responses[key] = msg
            by_id[msg.get("id") or None].add(key)
            for block in _blocks(msg.get("content")):
                if (sig := signature(block)) is not None:
                    by_block[sig].add(key)
            if stop := msg.get("stop_reason"):
                call["stop_reason"] = stop

    def match(msg):
        mid = msg.get("id")
        candidates = by_id.get(mid)
        if mid and candidates is None:
            candidates = by_id.get(None, set())
        if mid and mid in by_id and len(candidates) == 1:
            return next(iter(candidates))
        for block in _blocks(msg.get("content")):
            keys = by_block.get(signature(block))
            if keys:
                candidates = keys if candidates is None else candidates & keys
        if candidates and len(candidates) == 1:
            return next(iter(candidates))
        return None

    def content(blocks):
        if isinstance(blocks, str):
            return blocks
        parts = []
        for block in blocks or []:
            if block["type"] == "text":
                parts.append({"type": "text", "text": block["text"]})
            elif block["type"] == "image":
                source = block["source"]
                if source.get("type") == "base64":
                    parts.append(
                        media.image(f"data:{source['media_type']};base64,{source['data']}")
                    )
                elif source.get("type") == "url":
                    parts.append(media.image(source["url"]))
        return parts or ""

    add_step(doc, "user", prompt)

    def emit(event):
        msg = event.get("message") or {}
        parent = event.get("parent_tool_use_id")
        if parent not in docs:
            docs[parent] = document(metadata, parent)
        current = docs[parent]
        blocks = _blocks(msg.get("content"))
        if event.get("type") == "system" and event.get("subtype") == "compact_boundary":
            add_step(
                current,
                "system",
                "",
                extra={
                    "subtype": "compact_boundary",
                    "compact_metadata": event.get("compact_metadata", {}),
                },
            )
            return
        if event["type"] == "assistant":
            if (
                event.get("is_api_error_message")
                or event.get("error")
                or msg.get("model") == "<synthetic>"
            ):
                add_step(
                    current,
                    "system",
                    content(blocks),
                    extra={"cli_error": event["error"]} if event.get("error") else None,
                )
                return
            key = event.get("wire_call") or match(msg)
            if key is not None:
                if calls[key]["mapped_to_step"]:
                    return
                msg = responses[key]
                blocks = _blocks(msg.get("content"))
            mid = msg.get("id")
            step = steps.get((parent, mid)) if mid and not key else None
            if step is None:
                extra = {"context": {"complete": False, "missing": ["provider_request"]}}
                if key:
                    call = calls[key]
                    call["mapped_to_step"] = True
                    extra = {
                        k: call[k]
                        for k in ("context", "call_id", "response_status", "stop_reason")
                        if k in call
                    }
                if mid:
                    extra["message_id"] = mid
                if parent:
                    extra["parent_tool_use_id"] = parent
                step = add_step(current, "agent", "", model_name=msg.get("model"), extra=extra)
                if mid and not key:
                    steps[parent, mid] = step
            if parent:
                last_child_step[parent] = step
            if parts := content(blocks):
                step["message"] = [*(step["message"] or []), *parts]
            if msg.get("stop_reason"):
                step["extra"].setdefault("stop_reason", msg["stop_reason"])
            if event.get("aborted"):
                step["extra"]["response_status"] = "incomplete"
            for block in blocks:
                if block["type"] == "thinking":
                    step["reasoning_content"] = (
                        step.get("reasoning_content", "") + block["thinking"]
                    )
                elif block["type"] == "tool_use":
                    step.setdefault("tool_calls", []).append(
                        {
                            "tool_call_id": block["id"],
                            "function_name": block["name"],
                            "arguments": block["input"],
                        }
                    )
                    owners[parent, block["id"]] = step
                    parents[block["id"]] = parent
                elif block["type"] not in ("text", "image"):
                    step["extra"].setdefault("native_content", []).append(block)
            if usage := msg.get("usage"):
                metrics = step.setdefault("metrics", {})
                if "input_tokens" in usage:
                    metrics["prompt_tokens"] = sum(
                        usage.get(key) or 0
                        for key in (
                            "input_tokens",
                            "cache_read_input_tokens",
                            "cache_creation_input_tokens",
                        )
                    )
                if (
                    key
                    and calls[key]["response_status"] == "completed"
                    and usage.get("output_tokens") is not None
                ):
                    metrics["completion_tokens"] = usage["output_tokens"]
        elif event["type"] == "user":
            parts = content(blocks)
            extra = {"parent_tool_use_id": parent} if parent else {}
            other = [b for b in blocks if b["type"] not in ("text", "image", "tool_result")]
            if other:
                extra["native_content"] = other
            if parts or other:
                source = (
                    "system"
                    if event.get("isSynthetic") or event.get("isCompactSummary")
                    else "user"
                )
                add_step(current, source, parts, extra=extra or None)
            for block in blocks:
                if block["type"] != "tool_result":
                    continue
                key = block["tool_use_id"]
                result = {
                    "content": content(block.get("content")),
                    "extra": {"is_error": block.get("is_error", False)},
                }
                other = [
                    b for b in _blocks(block.get("content")) if b["type"] not in ("text", "image")
                ]
                if other:
                    result["extra"]["native_content"] = other
                native_result = event.get("tool_use_result")
                if (
                    isinstance(native_result, dict)
                    and native_result.get("agentId")
                    and native_result.get("status") == "completed"
                ):
                    result["extra"]["agent_id"] = native_result["agentId"]
                    # Some CLI builds forward only child tool activity. Recover the
                    # final response only when both its output and call lineage agree.
                    returned_blocks = _blocks(native_result.get("content"))
                    prior_ids = {
                        tool["tool_call_id"]
                        for step in docs.get(key, {}).get("steps", [])
                        for tool in step.get("tool_calls", [])
                    }
                    candidates = []
                    for cid, reply in responses.items():
                        answer_blocks = [
                            b for b in _blocks(reply.get("content")) if b.get("type") == "text"
                        ]
                        if (
                            calls[cid]["mapped_to_step"]
                            or not answer_blocks
                            or returned_blocks[-len(answer_blocks) :] != answer_blocks
                        ):
                            continue
                        history = requests[cid].get("messages", [])
                        if any(
                            b.get("tool_use_id") in prior_ids
                            for m in history
                            for b in _blocks(m.get("content"))
                        ):
                            candidates.append(cid)
                    if len(candidates) == 1:
                        emit(
                            {
                                "type": "assistant",
                                "parent_tool_use_id": key,
                                "wire_call": candidates[0],
                            }
                        )
                    child = last_child_step.get(key)
                    answer = child["message"] if child else None
                    returned = content(native_result.get("content"))
                    if (
                        not answer
                        or child.get("tool_calls")
                        or child["extra"].get("response_status") == "incomplete"
                        or not isinstance(returned, list)
                        or returned[-len(answer) :] != answer
                    ):
                        missing_finals.add(key)
                owner = owners.pop((parent, key), None)
                if owner is None:
                    add_step(
                        current,
                        "system",
                        "",
                        observation={"results": [result]},
                        extra={"unresolved_tool_call_id": key},
                    )
                else:
                    result["source_call_id"] = key
                    owner.setdefault("observation", {"results": []})["results"].append(result)

    for event in events:
        emit(event)
    for key, child in docs.items():
        if key is None:
            continue
        owner_doc = docs.get(parents.get(key), doc)
        owner_doc.setdefault("subagent_trajectories", []).append(child)
        for step in owner_doc["steps"]:
            for observation in step.get("observation", {}).get("results", []):
                if observation.get("source_call_id") == key:
                    observation["subagent_trajectory_ref"] = [{"trajectory_id": key}]
    for key, reply in responses.items():
        if not calls[key]["mapped_to_step"] or calls[key]["response_status"] != "completed":
            calls[key]["response_path"] = output.save_file(
                json.dumps(reply, ensure_ascii=False).encode(), ".json"
            )
    doc["extra"] = {"model_calls": list(calls.values()), "media_missing": media.missing}
    for current in docs.values():
        extra = current.setdefault("extra", {})
        missing = []
        if not calls or any(
            s["source"] == "agent" and not s["extra"]["context"]["complete"]
            for s in current["steps"]
        ):
            missing.append("provider_request")
        if missing_finals and (current is doc or current["trajectory_id"] in missing_finals):
            missing.append("subagent_final_message")
        if missing:
            extra["capture"] = {"complete": False, "missing": missing}
    if missing_finals:
        doc["extra"]["missing_subagent_finals"] = sorted(missing_finals)
    return doc
