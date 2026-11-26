import json
from copy import deepcopy

import pytest

from envweaver.trajectory.usage import trajectory_stats


def doc(input_tokens=10, output_tokens=3, **fields):
    return {
        "trajectory_id": "root",
        "steps": [
            {
                "source": "agent",
                "metrics": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                },
            }
        ],
        **fields,
    }


def save(root, name, value):
    (root / name).write_text(json.dumps(value))
    return name


def test_totals_prefer_reported_aggregate_without_adding_steps(tmp_path):
    value = doc(final_metrics={"total_prompt_tokens": 100, "total_completion_tokens": 20})
    assert trajectory_stats(tmp_path, save(tmp_path, "main.json", value)) == {
        "input_tokens": 100,
        "output_tokens": 20,
        "trajectory_length": 13,
        "step_count": 1,
    }


@pytest.mark.parametrize("missing", [None, -1, True, 1.5, "10"])
def test_unknown_input_does_not_zero_or_hide_known_output(tmp_path, missing):
    assert trajectory_stats(tmp_path, save(tmp_path, "main.json", doc(missing))) == {
        "input_tokens": None,
        "output_tokens": 3,
        "trajectory_length": None,
        "step_count": 1,
    }


def test_zero_is_known_and_missing_step_invalidates_total(tmp_path):
    value = doc(0, 0)
    path = save(tmp_path, "main.json", value)
    assert trajectory_stats(tmp_path, path) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "trajectory_length": 0,
        "step_count": 1,
    }
    value["steps"].append({"source": "agent"})
    save(tmp_path, path, value)
    assert trajectory_stats(tmp_path, path) == {
        "input_tokens": None,
        "output_tokens": None,
        "trajectory_length": None,
        "step_count": 2,
    }


def test_aggregated_step_counts_once_but_has_no_single_request_length(tmp_path):
    value = doc()
    value["steps"][0]["llm_call_count"] = 2
    stats = trajectory_stats(tmp_path, save(tmp_path, "main.json", value))
    assert stats == {
        "input_tokens": 10,
        "output_tokens": 3,
        "trajectory_length": None,
        "step_count": 1,
    }


def test_copied_context_and_non_model_steps_do_not_count(tmp_path):
    value = doc(final_metrics={"total_prompt_tokens": 999, "total_completion_tokens": 999})
    copied = deepcopy(value["steps"][0])
    copied["is_copied_context"] = True
    value["steps"] += [copied, {"source": "user"}, {"source": "agent", "llm_call_count": 0}]
    assert trajectory_stats(tmp_path, save(tmp_path, "main.json", value)) == {
        "input_tokens": 10,
        "output_tokens": 3,
        "trajectory_length": 13,
        "step_count": 1,
    }
    value["steps"][0]["metrics"] = {}
    save(tmp_path, "main.json", value)
    assert trajectory_stats(tmp_path, "main.json") == {
        "input_tokens": None,
        "output_tokens": None,
        "trajectory_length": None,
        "step_count": 1,
    }


def test_reference_graph_scopes_embedded_ids_and_visits_external_files_once(tmp_path):
    first = doc(20, 5, trajectory_id="child", session_id="same")
    first["subagent_trajectories"] = [doc(30, 7, trajectory_id="child", session_id="same")]
    root = doc(subagent_trajectories=[first], continued_trajectory_ref="continued.json")
    root["steps"][0]["observation"] = {
        "results": [
            {
                "subagent_trajectory_ref": [
                    {"trajectory_id": "child", "trajectory_path": "must-not-read.json"},
                    {"trajectory_path": "external.json"},
                    {"trajectory_path": "external.json"},
                ]
            }
        ]
    }
    continued = doc(40, 9, continued_trajectory_ref="main.json")
    copied = deepcopy(root["steps"][0])
    copied["is_copied_context"] = True
    copied.pop("observation")
    continued["steps"].append(copied)
    save(tmp_path, "continued.json", continued)
    save(tmp_path, "external.json", doc(50, 11))
    assert trajectory_stats(tmp_path, save(tmp_path, "main.json", root)) == {
        "input_tokens": 150,
        "output_tokens": 35,
        "trajectory_length": 49,
        "step_count": 2,
    }


@pytest.mark.parametrize(
    "reference",
    ["missing.json", "../escape.json", "/absolute.json", "invalid.json", "symlink.json"],
)
def test_bad_reference_never_returns_partial_total(tmp_path, reference):
    (tmp_path / "invalid.json").write_text("not JSON")
    save(tmp_path, "target.json", doc())
    (tmp_path / "symlink.json").symlink_to(tmp_path / "target.json")
    root = doc(continued_trajectory_ref=reference)
    assert trajectory_stats(tmp_path, save(tmp_path, "main.json", root)) == {
        "input_tokens": None,
        "output_tokens": None,
        "trajectory_length": None,
        "step_count": None,
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"capture": {"complete": False}},
        {"model_calls": [{"call_id": "retry", "mapped_to_step": False}]},
        {"model_calls": [{"call_id": "unmatched"}]},
    ],
)
def test_unmeasured_requests_make_totals_unknown(tmp_path, extra):
    value = doc(extra=extra, final_metrics={"total_prompt_tokens": 10})
    assert trajectory_stats(tmp_path, save(tmp_path, "main.json", value)) == {
        "input_tokens": None,
        "output_tokens": None,
        "trajectory_length": 13,
        "step_count": 1,
    }


def test_mapped_requests_are_not_counted_twice(tmp_path):
    value = doc(
        extra={
            "model_calls": [
                {"call_id": "request"},
                {"call_id": "transport", "agent_call_id": "request"},
            ]
        }
    )
    value["steps"][0]["extra"] = {"call_id": "request"}
    assert trajectory_stats(tmp_path, save(tmp_path, "main.json", value)) == {
        "input_tokens": 10,
        "output_tokens": 3,
        "trajectory_length": 13,
        "step_count": 1,
    }


def test_no_trajectory_is_unknown_and_empty_trajectory_has_no_calls(tmp_path):
    assert trajectory_stats(tmp_path, None) == {
        "input_tokens": None,
        "output_tokens": None,
        "trajectory_length": None,
        "step_count": None,
    }
    assert trajectory_stats(tmp_path, save(tmp_path, "empty.json", {"steps": []})) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "trajectory_length": None,
        "step_count": 0,
    }


@pytest.mark.parametrize(
    ("value", "steps"),
    [
        ({"steps": [None]}, None),
        ({"steps": [{"source": "agent", "metrics": "invalid"}]}, 1),
        ({"steps": [], "extra": "invalid"}, 0),
        ({"steps": [], "subagent_trajectories": [None]}, 0),
    ],
)
def test_malformed_trajectory_does_not_break_listing(tmp_path, value, steps):
    assert trajectory_stats(tmp_path, save(tmp_path, "main.json", value)) == {
        "input_tokens": None,
        "output_tokens": None,
        "trajectory_length": None,
        "step_count": steps,
    }
