"""ATIF storage rules shared by adapters; native event decoding stays in adapters."""

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

ATIF_VERSION = "ATIF-v1.8"


@lru_cache(maxsize=1)
def _validator():
    schema = json.loads(Path(__file__).with_name("atif-v1.8.schema.json").read_text())
    return Draft202012Validator(schema)


def validate_trajectory(value):
    """Validate the pinned Harbor schema and cross-step identities."""
    if not isinstance(value, dict):
        raise ValueError("ATIF trajectory must be an object")
    json.dumps(value, allow_nan=False)
    if value.get("schema_version") != ATIF_VERSION:
        raise ValueError(f"Expected {ATIF_VERSION}")
    errors = sorted(_validator().iter_errors(value), key=lambda e: str(e.path))
    if errors:
        raise ValueError(f"Invalid ATIF trajectory at {list(errors[0].path)}: {errors[0].message}")

    def check(document):
        steps = document["steps"]
        if [s["step_id"] for s in steps] != list(range(1, len(steps) + 1)):
            raise ValueError("ATIF step_id must be consecutive, starting at 1")
        children = document.get("subagent_trajectories") or []
        ids = [child.get("trajectory_id") for child in children]
        if any(not i for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("Embedded trajectories require unique trajectory_id values")

        def check_content(content):
            if not isinstance(content, list):
                return
            for part in content:
                kind = part["type"]
                if kind == "text":
                    if part.get("text") is None or part.get("source") is not None:
                        raise ValueError("ATIF text requires text and cannot have a media source")
                elif part.get("source") is None or part.get("text") is not None:
                    raise ValueError("ATIF media requires source and cannot have text")
                elif not part["source"]["media_type"].startswith(kind + "/"):
                    raise ValueError("ATIF media type does not match its content type")

        for step in steps:
            check_content(step["message"])
            if step.get("timestamp"):
                datetime.fromisoformat(step["timestamp"].replace("Z", "+00:00"))
            if step["source"] != "agent" and any(
                step.get(key) is not None
                for key in (
                    "model_name",
                    "reasoning_effort",
                    "reasoning_content",
                    "tool_calls",
                    "metrics",
                )
            ):
                raise ValueError("Model fields require an agent step")
            step_calls = {call["tool_call_id"] for call in step.get("tool_calls") or []}
            if len(step_calls) != len(step.get("tool_calls") or []):
                raise ValueError("Duplicate ATIF tool_call_id within one step")
            for result in (step.get("observation") or {}).get("results", []):
                if result.get("source_call_id") and result["source_call_id"] not in step_calls:
                    raise ValueError("Observation references an unknown tool call")
                check_content(result.get("content"))
                for ref in result.get("subagent_trajectory_ref") or []:
                    if not ref.get("trajectory_path") and ref.get("trajectory_id") not in ids:
                        raise ValueError("Unresolved embedded subagent reference")
            if step.get("llm_call_count") == 0 and (
                step.get("metrics") is not None or step.get("reasoning_content") is not None
            ):
                raise ValueError("Non-LLM steps cannot contain model metrics or reasoning")
        for child in children:
            check(child)

    check(value)


def document(metadata, identity):
    return {
        "schema_version": ATIF_VERSION,
        "trajectory_id": str(identity),
        "session_id": str(identity),
        "agent": {
            "name": metadata["agent"],
            "version": metadata.get("agent_version") or "unknown",
            **({"model_name": metadata["model"]} if metadata.get("model") else {}),
        },
        "steps": [],
    }


def add_step(trajectory, source, message, **fields):
    step = {"step_id": len(trajectory["steps"]) + 1, "source": source, "message": message}
    step.update({k: v for k, v in fields.items() if v is not None})
    trajectory["steps"].append(step)
    return step


def context(output, request, *, source, missing=()):
    """Snapshot the input at the model boundary, without rewriting provider messages."""
    missing = list(missing)

    def inspect(value):
        if isinstance(value, dict):
            if "envweaver_unserializable_type" in value:
                missing.append("unserializable:" + value["envweaver_unserializable_type"])
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(request)
    data = json.dumps(request, ensure_ascii=False, allow_nan=False).encode()
    path = output.save_file(data, ".json")
    return {
        "context": {
            "path": path,
            "source": source,
            "complete": not missing,
            "missing": list(missing),
        }
    }
