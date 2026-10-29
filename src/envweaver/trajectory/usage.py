"""Token totals from ATIF fields only; usable on storage hosts without dependencies."""

import json
from pathlib import Path


def token_usage(directory, trajectory):
    """Sum recorded model usage, excluding copied context; unknown totals stay None.

    Paths are relative to the Agent directory. Files are visited once, embedded IDs
    are scoped to their parent, and a ref resolving to an embedded child does not
    additionally count its file representation. Missing or unsafe references make
    both totals unknown instead of returning an apparently complete partial sum.
    """
    try:
        return _token_usage(directory, trajectory)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"input_tokens": None, "output_tokens": None}


def _token_usage(directory, trajectory):
    unknown = {"input_tokens": None, "output_tokens": None}
    if not trajectory:
        return unknown
    root = Path(directory).resolve()
    files, documents = set(), []
    pending = [trajectory]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            relative = Path(value)
            if relative.is_absolute() or ".." in relative.parts or "\\" in value:
                return unknown
            path = root / relative
            if not path.resolve().is_relative_to(root) or any(
                part.is_symlink() for part in [path, *path.parents] if part != root.parent
            ):
                return unknown
            path = path.resolve()
            if path in files:
                continue
            files.add(path)
            value = json.loads(path.read_text())
        if not isinstance(value, dict) or not isinstance(value.get("steps"), list):
            return unknown
        documents.append(value)
        children = value.get("subagent_trajectories") or []
        pending.extend(children)
        ids = {child["trajectory_id"] for child in children}
        if value.get("continued_trajectory_ref"):
            pending.append(value["continued_trajectory_ref"])
        for step in value["steps"]:
            for result in (step.get("observation") or {}).get("results", []):
                for ref in result.get("subagent_trajectory_ref") or []:
                    if ref.get("trajectory_id") not in ids:
                        if not ref.get("trajectory_path"):
                            return unknown
                        pending.append(ref["trajectory_path"])

    # Captured calls not represented in steps may have consumed unreported tokens.
    call_ids = {
        step.get("extra", {}).get("call_id") for doc in documents for step in doc["steps"]
    } - {None}
    for doc in documents:
        extra = doc.get("extra") or {}
        if (extra.get("capture") or {}).get("complete") is False:
            return unknown
        for call in extra.get("model_calls") or []:
            if call.get("mapped_to_step") is False or (
                call.get("mapped_to_step") is not True
                and call.get("call_id") not in call_ids
                and call.get("agent_call_id") not in call_ids
            ):
                return unknown

    def count(value):
        return type(value) is int and value >= 0

    totals = {}
    for output, metric in (
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
    ):
        total = 0
        for doc in documents:
            steps = [
                s for s in doc["steps"] if s["source"] == "agent" and s.get("llm_call_count") != 0
            ]
            copied = any(s.get("is_copied_context") for s in steps)
            values = [
                (s.get("metrics") or {}).get(metric)
                for s in steps
                if not s.get("is_copied_context")
            ]
            final = (doc.get("final_metrics") or {}).get("total_" + metric)
            if not copied and count(final):
                total += final
            elif values and all(count(value) for value in values):
                total += sum(values)
            elif values:
                total = None
                break
        totals[output] = total
    return totals
