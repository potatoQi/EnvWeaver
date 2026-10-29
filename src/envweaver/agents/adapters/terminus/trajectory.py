"""Keep Harbor's native ATIF and attach captured LiteLLM request contexts."""

from copy import deepcopy

from ...trajectory import ATIF_VERSION, context


def convert(trajectories, events, output):
    if "trajectory.json" not in trajectories:
        return None
    requests, responses = {}, {}
    outcomes = {}
    for event in events:
        if event["type"] == "request":
            requests[event["call_id"]] = context(
                output, event["data"]["request"], source="terminus.litellm." + event["data"]["api"]
            )
        elif event["type"] == "response":
            outcomes[event["call_id"]] = {
                "response_status": "completed",
                "native_response": event["data"],
            }
            for choice in event["data"].get("choices", []):
                message = choice.get("message") or {}
                responses[event["call_id"]] = message.get("content") or ""
        elif event["type"] == "error":
            outcomes[event["call_id"]] = {"response_status": "failed", "error": event["data"]}
    documents = {}
    used = set()
    for name, native in trajectories.items():
        if (
            not name.startswith("trajectory")
            or not isinstance(native, dict)
            or "steps" not in native
        ):
            continue
        doc = deepcopy(native)
        doc["schema_version"] = ATIF_VERSION
        doc["trajectory_id"] = name
        for step in doc["steps"]:
            if step["source"] == "agent" and step.get("llm_call_count") != 0:
                matching = [
                    key
                    for key, response in responses.items()
                    if response == step["message"]
                ]
                if len(matching) == 1:
                    key = matching[0]
                    used.add(key)
                    extra = {**requests[key], "call_id": key}
                else:
                    extra = {
                        "context": {
                            "complete": False,
                            "missing": [
                                "ambiguous_provider_request"
                                if matching
                                else "unmatched_provider_request"
                            ],
                        }
                    }
                step["extra"] = {**(step.get("extra") or {}), **extra}
        documents[name] = doc
    root = documents["trajectory.json"]
    root["extra"] = {
        **(root.get("extra") or {}),
        "model_calls": [
            {
                "call_id": key,
                **value,
                **outcomes.get(key, {"response_status": "pending"}),
                "mapped_to_step": key in used,
            }
            for key, value in requests.items()
        ],
    }
    return documents
