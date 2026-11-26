"""ATIF list statistics, also evaluated on storage hosts without dependencies."""

import json
from functools import cache
from pathlib import Path


def main_agent_steps(document, resolve):
    """Yield recorded model steps in the main session, following continuations once."""
    seen = set()
    while document is not None and id(document) not in seen:
        seen.add(id(document))
        yield from (
            step
            for step in document["steps"]
            if _model_step(step) and not step.get("is_copied_context")
        )
        reference = document.get("continued_trajectory_ref")
        document = resolve(reference) if reference else None


def _model_step(step):
    return step["source"] == "agent" and step.get("llm_call_count") != 0


def _count(value):
    return type(value) is int and value >= 0


def trajectory_stats(directory, trajectory):
    """Read each ATIF file once. Missing or incomplete measurements stay unknown."""
    stats = dict.fromkeys(("input_tokens", "output_tokens", "trajectory_length", "step_count"))
    if not trajectory:
        return stats
    root = Path(directory).resolve()

    @cache
    def read(relative):
        path = root / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in str(relative)
            or any(p.is_symlink() for p in [path, *path.parents] if p != root.parent)
            or not path.resolve().is_relative_to(root)
        ):
            raise ValueError("Unsafe trajectory reference")
        return json.loads(path.read_text())

    def resolve(reference):
        return read(Path(reference))

    try:
        document = resolve(trajectory)
        steps = 0
        last = None
        for step in main_agent_steps(document, resolve):
            steps += 1
            last = step
        stats["step_count"] = steps
        if last is not None and last.get("llm_call_count") in (None, 1):
            metrics = last.get("metrics") or {}
            prompt, completion = metrics.get("prompt_tokens"), metrics.get("completion_tokens")
            if _count(prompt) and _count(completion):
                stats["trajectory_length"] = prompt + completion
        stats.update(_token_totals(document, resolve))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return stats


def _token_totals(document, resolve):
    unknown = {"input_tokens": None, "output_tokens": None}
    seen, documents = set(), []
    pending = [document]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            value = resolve(value)
        if id(value) in seen:
            continue
        seen.add(id(value))
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

    totals = {"input_tokens": 0, "output_tokens": 0}
    for doc in documents:
        metrics = []
        copied = False
        for step in doc["steps"]:
            if _model_step(step):
                if step.get("is_copied_context"):
                    copied = True
                else:
                    metrics.append(step.get("metrics") or {})
        for output, metric in (
            ("input_tokens", "prompt_tokens"),
            ("output_tokens", "completion_tokens"),
        ):
            if totals[output] is None:
                continue
            values = [entry.get(metric) for entry in metrics]
            final = (doc.get("final_metrics") or {}).get("total_" + metric)
            if not copied and _count(final):
                totals[output] += final
            elif all(_count(value) for value in values):
                totals[output] += sum(values)
            else:
                totals[output] = None
    return totals
