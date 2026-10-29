"""mini's final native snapshot must preserve tool-result ownership and metadata."""

import json

import pytest

from envweaver import AgentOutput
from envweaver.agents.adapters.mini_swe.trajectory import convert
from envweaver.agents.trajectory import validate_trajectory


@pytest.mark.parametrize("reuse_id", [False, True])
@pytest.mark.parametrize("rewrite_context", [False, True])
@pytest.mark.parametrize("same_command", [False, True])
def test_final_snapshot_keeps_results_on_their_original_step(
    tmp_path, reuse_id, rewrite_context, same_command
):
    output = AgentOutput(tmp_path, {"agent": "mini", "agent_version": "2", "model": "test"})
    events, messages = [], [{"role": "user", "content": "task"}]
    for index in range(2):
        call_id = "reused" if reuse_id else f"tool-{index}"
        events.append(
            {
                "type": "request",
                "call_id": f"model-{index}",
                "data": {
                    "api": "completion",
                    "request": {"messages": list(messages)},
                },
            }
        )
        assistant = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": json.dumps(
                            {"command": f"echo {0 if same_command else index}"}
                        ),
                    },
                }
            ],
        }
        events.append(
            {
                "type": "response",
                "call_id": f"model-{index}",
                "data": {
                    "id": f"response-{index}",
                    "choices": [{"message": assistant}],
                },
            }
        )
        messages.extend(
            [
                assistant,
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": f"result-{index}",
                    "extra": {"returncode": index, "raw_output": str(index)},
                },
            ]
        )
    if rewrite_context:
        events.append(
            {
                "type": "request",
                "call_id": "next-model",
                "data": {
                    "api": "completion",
                    "request": {"messages": [{"role": "system", "content": "updated"}, *messages]},
                },
            }
        )
    doc = convert(events, output, native={"messages": messages})
    validate_trajectory(doc)
    steps = [step for step in doc["steps"] if step["source"] == "agent"]
    for index, step in enumerate(steps[:2]):
        results = step["observation"]["results"]
        assert len(results) == 1
        assert results[0]["content"] == f"result-{index}"
        assert results[0]["extra"] == {"returncode": index, "raw_output": str(index)}


def test_unmatched_native_tool_result_keeps_content_and_metadata(tmp_path):
    output = AgentOutput(tmp_path, {"agent": "mini", "agent_version": "2", "model": "test"})
    doc = convert(
        [],
        output,
        native={
            "messages": [
                {
                    "role": "tool",
                    "tool_call_id": "not-collected-yet",
                    "content": "failed",
                    "extra": {"returncode": 7, "exception_info": "intentional"},
                }
            ]
        },
    )
    validate_trajectory(doc)
    step = doc["steps"][0]
    assert step["extra"]["unresolved_tool_call_id"] == "not-collected-yet"
    assert step["observation"]["results"] == [
        {
            "content": "failed",
            "extra": {"returncode": 7, "exception_info": "intentional"},
        }
    ]
