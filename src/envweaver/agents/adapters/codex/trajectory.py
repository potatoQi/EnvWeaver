"""Convert Codex rollout traces v1–v3; each inference keeps its actual request."""

import json
from datetime import datetime, timezone
from pathlib import PurePosixPath

from ....trajectory.atif import add_step, context, document
from ...capture import Media, json_lines
from .payloads import read_payload, relative_path


def convert(files, output, read_remote=None):
    media = Media(output, read_remote)
    documents = {}
    requests = {}
    active_inferences, tool_owners = {}, {}
    results = []
    root_id = None
    missing = set()

    def content(value):
        if isinstance(value, str):
            return value
        parts = []
        for item in value or []:
            kind = item.get("type")
            if kind in ("text", "input_text", "output_text"):
                parts.append({"type": "text", "text": item["text"]})
            elif kind in ("image", "input_image"):
                parts.append(media.image(item.get("image_url") or item.get("url")))
        return parts or ""

    for name, text in files.items():
        if not name.endswith("/trace.jsonl"):
            continue
        base = str(PurePosixPath(name).parent)

        def payload(ref):
            return read_payload(files, base, ref, missing)

        for event in json_lines(text):
            if event.get("schema_version") not in (1, 2, 3):
                raise ValueError(f"Unsupported Codex rollout trace schema {event.get('schema_version')!r}")
            data = event["payload"]
            kind = data["type"]
            if kind == "rollout_started":
                root_id = root_id or data["root_thread_id"]
            tid = event.get("thread_id") or data.get("thread_id")
            if tid:
                if tid not in documents:
                    documents[tid] = document(output.result["metadata"], tid)
                doc = documents[tid]
            if kind == "thread_started":
                doc["extra"] = {"codex": payload(data.get("metadata_payload"))}
            elif kind == "inference_started":
                active_inferences[tid] = data["inference_call_id"]
                request_gaps = set()
                request = read_payload(files, base, data["request_payload"], request_gaps)
                if request_gaps:
                    missing.update(request_gaps)
                    step = add_step(doc, "agent", "", model_name=data.get("model"), extra={
                        "inference_call_id": data["inference_call_id"],
                        "response_status": "pending",
                        "context": {"complete": False, "missing": sorted(request_gaps)},
                    })
                    requests[data["inference_call_id"]] = step
                    continue
                gaps = []
                if request.get("previous_response_id"):
                    gaps.append("server_side_previous_response")
                if any(
                    i.get("type") in ("compaction", "context_compaction")
                    for i in request.get("input", [])
                ):
                    gaps.append("encrypted_compaction")
                extra = context(
                    output, request, source="codex.rollout.inference_request", missing=gaps
                )
                extra["inference_call_id"] = data["inference_call_id"]
                extra["response_status"] = "pending"
                if not doc["steps"]:
                    if request.get("instructions"):
                        add_step(doc, "system", request["instructions"])
                    for item in request.get("input", []):
                        role = item.get("role")
                        if role in ("system", "developer", "user"):
                            add_step(
                                doc,
                                "user" if role == "user" else "system",
                                content(item.get("content")),
                                extra={"role": role},
                            )
                    if request.get("tools"):
                        doc["agent"]["tool_definitions"] = request["tools"]
                step = add_step(doc, "agent", "", model_name=request.get("model"), extra=extra)
                if event.get("wall_time_unix_ms") is not None:
                    step["timestamp"] = datetime.fromtimestamp(
                        event["wall_time_unix_ms"] / 1000, timezone.utc
                    ).isoformat()
                requests[data["inference_call_id"]] = step
            elif kind in ("inference_completed", "inference_failed"):
                step = requests.get(data["inference_call_id"])
                if step is None:
                    continue
                failed = kind == "inference_failed"
                if failed:
                    step["extra"].update(
                        response_status="failed", failure=data, response_incomplete=True
                    )
                ref = data.get("partial_response_payload" if failed else "response_payload")
                response = payload(ref)
                if response is None:
                    step["extra"]["response_incomplete"] = True
                    continue
                if not failed:
                    step["extra"]["response_status"] = "completed"
                step["extra"]["native_response"] = (
                    output.save_file(json.dumps(response, ensure_ascii=False).encode(), ".json")
                    if ref.get("parts") else f"logs/native/{base}/{relative_path(ref)}"
                )
                messages, reasoning, tools = [], [], []
                for item in response.get("output_items", []):
                    kind = item.get("type")
                    if kind == "message":
                        value = content(item.get("content"))
                        messages.extend(
                            value if isinstance(value, list) else [{"type": "text", "text": value}]
                        )
                    elif kind == "reasoning":
                        visible = item.get("content") or item.get("summary") or []
                        reasoning.extend(part["text"] for part in visible if "text" in part)
                        if item.get("encrypted_content"):
                            step["extra"]["encrypted_reasoning"] = True
                    elif kind in ("function_call", "custom_tool_call"):
                        raw = item.get("arguments", item.get("input", ""))
                        try:
                            args = json.loads(raw)
                        except (ValueError, TypeError):
                            args = None
                        tool = {
                            "tool_call_id": item["call_id"],
                            "function_name": ".".join(
                                filter(None, [item.get("namespace"), item["name"]])
                            ),
                            "arguments": args if isinstance(args, dict) else {},
                            "extra": {"raw_arguments": raw, "type": kind},
                        }
                        tools.append(tool)
                    else:
                        step["extra"].setdefault("unmapped_output_types", []).append(kind)
                step["message"] = messages or ""
                if reasoning:
                    step["reasoning_content"] = "\n".join(reasoning)
                if tools:
                    step["tool_calls"] = tools
                usage = response.get("token_usage") or {}
                step["metrics"] = {
                    target: usage[key]
                    for key, target in (
                        ("input_tokens", "prompt_tokens"),
                        ("output_tokens", "completion_tokens"),
                        ("cached_input_tokens", "cached_tokens"),
                    )
                    if key in usage
                }
            elif kind == "tool_call_started":
                tool_owners[(tid, data["tool_call_id"])] = active_inferences.get(tid)
            elif kind == "tool_call_ended":
                result = payload(data.get("result_payload"))
                if result is not None:
                    owner = tool_owners.get((tid, data["tool_call_id"]), active_inferences.get(tid))
                    results.append((owner, data["tool_call_id"], result))
    # Streaming tools may finish before the inference-completed event is written.
    for owner, call_id, result in results:
        step = requests.get(owner)
        if step is None or not any(
            c["tool_call_id"] == call_id for c in step.get("tool_calls", [])
        ):
            continue
        response = result.get("response_item", {})
        observation = {
            "source_call_id": call_id,
            "content": content(response.get("output", "")),
            "extra": {"native_result": result},
        }
        # Only explicit spawn IDs establish a child relationship; never flatten child text.
        try:
            result_data = json.loads(response.get("output", ""))
        except (ValueError, TypeError):
            result_data = None
        if isinstance(result_data, dict) and result_data.get("agent_id"):
            observation["extra"]["child_thread_id"] = result_data["agent_id"]
        step.setdefault("observation", {"results": []})["results"].append(observation)
    if not documents:
        return None
    root = documents.get(root_id) or next(iter(documents.values()))
    if not root["steps"]:
        return None
    pending_threads = [key for key, child in documents.items() if not child["steps"]]
    documents = {key: child for key, child in documents.items() if child["steps"]}
    root.setdefault("extra", {})["pending_thread_ids"] = pending_threads
    if missing:
        root["extra"]["capture"] = {"complete": False, "missing": sorted(missing)}
    for child in documents.values():
        child["session_id"] = root["session_id"]
        if child is root:
            continue
        source = (child.get("extra", {}).get("codex") or {}).get("session_source")
        parent_id = (
            source.get("subagent", {}).get("thread_spawn", {}).get("parent_thread_id")
            if isinstance(source, dict)
            else None
        )
        parent = documents.get(parent_id, root)
        parent.setdefault("subagent_trajectories", []).append(child)
    for doc in documents.values():
        doc["extra"] = {**doc.get("extra", {}), "media_missing": media.missing}
        for step in doc["steps"]:
            for observation in (step.get("observation") or {}).get("results", []):
                child = observation.get("extra", {}).get("child_thread_id")
                if child in documents and documents[child] is not root:
                    observation["subagent_trajectory_ref"] = [{"trajectory_id": child}]
    return root
