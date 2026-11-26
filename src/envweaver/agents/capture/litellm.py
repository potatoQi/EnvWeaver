"""Convert captured LiteLLM calls and native tool observations to ATIF."""

import json
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone

from ...trajectory.atif import add_step, context, document
from . import Media


def convert(events, output, *, session_id, read_remote=None, native_messages=None):
    doc = document(output.result["metadata"], session_id)
    harness = doc["agent"]["name"]
    media = Media(output, read_remote)
    steps = {}
    unresolved = set()
    previous = []

    def content(value):
        if isinstance(value, str) or value is None:
            return value or ""
        parts = []
        for part in value:
            if part["type"] == "text":
                parts.append({"type": "text", "text": part["text"]})
            elif part["type"] == "image_url":
                parts.append(media.image(part["image_url"]["url"]))
        return parts or ""

    def tool_key(calls):
        return json.dumps(
            [
                (call["id"], call["function"]["name"], call["function"]["arguments"])
                for call in calls
            ],
            sort_keys=True,
        )

    def collect_results(messages):
        if not any(message.get("role") == "tool" for message in messages):
            return
        occurrences, candidates = defaultdict(deque), defaultdict(list)
        for step in steps.values():
            response = step["extra"].get("native_response") or {}
            choices = response.get("choices") or [{}]
            calls = (choices[0].get("message") or {}).get("tool_calls") or []
            if calls:
                occurrences[tool_key(calls)].append(step)
            for call in calls:
                candidates[call["id"]].append(step)
        owners = {}
        for message in messages:
            if message.get("role") == "assistant":
                calls = message.get("tool_calls") or []
                matches = occurrences[tool_key(calls)]
                step = matches.popleft() if matches else None
                owners = {call["id"]: step for call in calls}
            call_id = message.get("tool_call_id")
            if message.get("role") != "tool":
                continue
            step = owners.get(call_id)
            if step is None and len(candidates[call_id]) == 1:
                step = candidates[call_id][0]
            if step is None or call_id in step["extra"].get("duplicate_tool_call_ids", ()):
                marker = json.dumps([call_id, message.get("content")], sort_keys=True)
                if marker not in unresolved:
                    add_step(
                        doc,
                        "system",
                        "",
                        observation={
                            "results": [
                                {
                                    "content": content(message.get("content")),
                                    **({"extra": message["extra"]} if message.get("extra") else {}),
                                }
                            ]
                        },
                        extra={"unresolved_tool_call_id": call_id},
                    )
                    unresolved.add(marker)
                continue
            results = step.setdefault("observation", {"results": []})["results"]
            result = next((item for item in results if item.get("source_call_id") == call_id), None)
            if result is None:
                result = {"source_call_id": call_id, "content": content(message.get("content"))}
                results.append(result)
            if message.get("extra"):
                result["extra"] = message["extra"]

    for event in events:
        data = event["data"]
        if event["type"] == "request":
            request = data["request"]
            request_messages = request.get("messages", [])
            common = 0
            for left, right in zip(previous, request_messages):
                if left != right:
                    break
                common += 1
            for index, message in enumerate(request_messages[common:], start=common):
                role = message.get("role")
                if role in ("system", "user", "developer"):
                    kind = data.get("message_kinds", {}).get(str(index))
                    add_step(
                        doc,
                        "user" if role == "user" and kind is None else "system",
                        content(message.get("content")),
                        extra={"role": role, **({"kind": kind} if kind else {})},
                    )
            if native_messages is not None:
                collect_results(request_messages)
            previous = request_messages
            extra = context(output, request, source=f"{harness}.litellm.{data['api']}")
            extra.update(
                data.get("step_extra", {}), call_id=event["call_id"], response_status="pending"
            )
            steps[event["call_id"]] = add_step(
                doc,
                "agent",
                "",
                model_name=request.get("model"),
                timestamp=datetime.fromtimestamp(event["time"], timezone.utc).isoformat()
                if event.get("time") is not None
                else None,
                extra=extra,
            )
            if request.get("tools"):
                doc["agent"]["tool_definitions"] = request["tools"]
        elif event["type"] == "response" and event["call_id"] in steps:
            step = steps[event["call_id"]]
            step["extra"]["response_status"] = "completed"
            if "choices" not in data:
                step["extra"]["response_conversion_missing"] = True
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            step["message"] = content(message.get("content"))
            step["extra"]["native_response"] = data
            if message.get("reasoning_content"):
                step["reasoning_content"] = message["reasoning_content"]
            tools = message.get("tool_calls") or []
            ids = Counter(tool["id"] for tool in tools if tool.get("id"))
            duplicates = [key for key, count in ids.items() if count > 1]
            if duplicates:
                step["extra"]["duplicate_tool_call_ids"] = duplicates
            for index, tool in enumerate(tools):
                raw = tool["function"]["arguments"]
                try:
                    arguments = json.loads(raw)
                except (ValueError, TypeError):
                    arguments = None
                provider_id = tool.get("id")
                call_id = provider_id
                call_extra = {"raw_arguments": raw}
                if not provider_id or ids[provider_id] > 1:
                    call_id = f"{event['call_id']}:{index}"
                    while call_id in ids:
                        call_id += ":"
                    if provider_id:
                        call_extra["provider_tool_call_id"] = provider_id
                call = {
                    "tool_call_id": call_id,
                    "function_name": tool["function"]["name"],
                    "arguments": arguments if isinstance(arguments, dict) else {},
                    "extra": call_extra,
                }
                step.setdefault("tool_calls", []).append(call)
            usage = data.get("usage") or {}
            step["metrics"] = {
                k: usage[k] for k in ("prompt_tokens", "completion_tokens") if k in usage
            }
        elif event["type"] == "error" and event["call_id"] in steps:
            steps[event["call_id"]]["extra"].update(response_status="failed", failure=data)

        elif event["type"] == "tool_result":
            step = steps[event["call_id"]]
            # Stirrup assigns its own ID when the provider omits one.
            call = step["tool_calls"][data["call_index"]]
            if "provider_tool_call_id" not in call["extra"]:
                call["tool_call_id"] = data["tool_call_id"]
            step.setdefault("observation", {"results": []})["results"].append(
                {
                    "source_call_id": call["tool_call_id"],
                    "content": content(data["content"]),
                    "extra": data["extra"],
                }
            )

    if native_messages:
        collect_results(native_messages)
    doc["extra"] = {"media_missing": media.missing}
    return doc if doc["steps"] else None
