import json

import pytest

from envweaver import AgentOutput, ClaudeCodeAgent
from envweaver.agents.adapters.claude_code.trajectory import convert
from envweaver.errors import ErrorType
from envweaver.trajectory.atif import validate_trajectory


def test_join_provider_calls_and_tool_results(tmp_path):
    meta = {"agent": "claude", "agent_version": "2.0.61", "model": "test"}
    output = AgentOutput(tmp_path, meta)
    request = {"system": "system", "messages": [{"role": "user", "content": "test"}]}
    records = [
        {"id": "aux", "request": {"messages": []}},
        {"id": "main", "request": request},
        {
            "id": "main",
            "status": 200,
            "complete": True,
            "message": {
                "id": "m1",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "thinking", "thinking": "plan"},
                    {
                        "type": "tool_use",
                        "id": "read",
                        "name": "Read",
                        "input": {"file_path": "/tmp/doc"},
                    },
                ],
            },
        },
    ]

    def event(kind, mid, *blocks):
        return {"type": kind, "session_id": "s1", "message": {"id": mid, "content": list(blocks)}}

    events = [
        event("assistant", "m1", {"type": "thinking", "thinking": "plan"}),
        event(
            "assistant",
            "m1",
            {"type": "tool_use", "id": "read", "name": "Read", "input": {"file_path": "/tmp/doc"}},
        ),
        event("user", "", {"type": "tool_result", "tool_use_id": "read", "content": "value"}),
        event("assistant", "m2", {"type": "text", "text": "done"}),
    ]
    trace = convert(events, records, meta, "test", output)
    validate_trajectory(trace)
    _, first, second = trace["steps"]
    assert trace["session_id"] == "s1"
    assert first["extra"]["response_status"] == "completed"
    assert first["extra"]["stop_reason"] == "tool_use"
    assert first["reasoning_content"] == "plan"
    assert (
        first["tool_calls"][0]["tool_call_id"]
        == first["observation"]["results"][0]["source_call_id"]
    )
    assert first["observation"]["results"][0]["content"] == "value"
    assert json.loads((tmp_path / first["extra"]["context"]["path"]).read_text()) == request
    assert second["message"][0]["text"] == "done"
    assert not second["extra"]["context"]["complete"] and not trace["extra"]["capture"]["complete"]
    assert not trace["extra"]["model_calls"][0]["mapped_to_step"]


@pytest.mark.parametrize(
    ("subtype", "expected"),
    [
        ("error_max_turns", ErrorType.STEP_LIMIT),
        ("error_max_budget_usd", ErrorType.COST_LIMIT),
    ],
)
def test_native_limits(subtype, expected):
    agent = ClaudeCodeAgent()
    agent._on_event({"type": "result", "subtype": subtype, "is_error": True})
    assert agent._run_error.type == expected


def test_preflight_and_command():
    agent = ClaudeCodeAgent(model="sonnet", extra_args=["--max-turns", "3"])
    goal = [{"type": "text", "text": "hello"}]
    agent.preflight(goal)
    agent._python, agent._remote = "python3", "/test"
    assert "--permission-mode=bypassPermissions" in agent._command("-hello", [])
    assert agent._command("-hello", [])[-3:] == ["sonnet", "--max-turns", "3"]
    with pytest.raises(ValueError, match="image goals"):
        agent.preflight([{"type": "image", "path": "image.png"}])
    with pytest.raises(ValueError, match="override"):
        ClaudeCodeAgent(extra_args=["--output-format=json"]).preflight(goal)


@pytest.mark.parametrize(
    "args",
    [
        ["--permission-mode", "plan"],
        ["--permission-mode=default"],
        ["--allowedTools=Read"],
        ["--disallowed-tools", "Bash"],
        ["--settings", "custom.json"],
        ["--dangerously-skip-permissions"],
    ],
)
def test_explicit_permissions_override_default(args):
    agent = ClaudeCodeAgent(extra_args=args)
    agent._python, agent._remote = "python3", "/test"
    command = agent._command("hello", [])
    assert "--permission-mode=bypassPermissions" not in command
    assert command[-len(args) :] == args


def test_structured_report_is_the_final_output(tmp_path):
    agent = ClaudeCodeAgent()
    agent._output = AgentOutput(tmp_path, agent.metadata)
    agent._on_event(
        {
            "type": "result",
            "subtype": "success",
            "result": "Done.",
            "structured_output": {"tag": "苹果"},
        }
    )
    assert json.loads(agent._output.result["output"][0]["text"]) == {"tag": "苹果"}


def test_synthetic_error_is_not_a_model_answer(tmp_path):
    agent = ClaudeCodeAgent()
    output = AgentOutput(tmp_path, agent.metadata)
    event = {
        "type": "assistant",
        "error": "invalid_request",
        "message": {
            "model": "<synthetic>",
            "content": [{"type": "text", "text": "Prompt is too long"}],
        },
    }
    trace = convert([event], [], agent.metadata, "test", output)
    validate_trajectory(trace)
    user, error = trace["steps"]
    assert user["source"] == "user"
    assert error["source"] == "system" and error["extra"]["cli_error"] == "invalid_request"
    assert error["message"] == event["message"]["content"]


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"terminal_reason": "prompt_too_long", "api_error_status": 400}, ErrorType.CONTEXT_LIMIT),
        ({"api_error_status": 400}, ErrorType.CONFIGURATION),
        ({"api_error_status": 401}, ErrorType.AUTHENTICATION),
        ({"api_error_status": 529}, ErrorType.PROVIDER_TRANSIENT),
        ({"error": "model_not_found"}, ErrorType.CONFIGURATION),
    ],
)
def test_error_classification_without_display_text(fields, reason):
    agent = ClaudeCodeAgent()
    agent._python, agent._remote = "python3", "/test"
    agent._command("test", [])
    agent._on_event(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": "其他语言的提示",
            **fields,
        }
    )
    assert agent._run_error.type == reason


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, ErrorType.UNKNOWN),
        ("test", ErrorType.AUTHENTICATION),
        ([{"type": "text", "text": "test"}], ErrorType.AUTHENTICATION),
    ],
)
def test_primary_request_takes_precedence_over_auxiliary_calls(content, expected):
    agent = ClaudeCodeAgent()
    agent._python, agent._remote = "python3", "/test"
    agent._command("test", [])
    agent._on_event(
        {"type": "result", "subtype": "success", "is_error": True, "result": "未知错误"}
    )
    calls = [
        {"call_id": "a", "response_status": "failed", "http_status": 401},
        {"call_id": "b", "response_status": "completed"},
    ]
    records = [{"id": "a", "request": {"messages": [{"role": "user", "content": content or []}]}}]
    agent._classify_provider_error(records, calls)
    assert agent._run_error.type == expected
    agent._classify_provider_error(records, calls[:1])
    assert agent._run_error.type == ErrorType.AUTHENTICATION


def test_interleaved_child_calls_keep_their_own_observations(tmp_path):
    agent = ClaudeCodeAgent()
    output = AgentOutput(tmp_path, agent.metadata)
    events = [{"type": "system", "subtype": "init", "session_id": "real-session"}]
    for parent in ("child-a", "child-b"):
        events.append(
            {
                "type": "assistant",
                "parent_tool_use_id": parent,
                "message": {
                    "id": "same-id",
                    "content": [
                        {"type": "tool_use", "name": "Read", "id": "same-call", "input": {}}
                    ],
                },
            }
        )
    for parent in ("child-b", "child-a"):
        events.append(
            {
                "type": "user",
                "parent_tool_use_id": parent,
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "same-call", "content": parent}
                    ]
                },
            }
        )
    events.extend(
        [
            {
                "type": "system",
                "subtype": "compact_boundary",
                "compact_metadata": {"trigger": "auto"},
            },
            {
                "type": "assistant",
                "aborted": True,
                "message": {"id": "different", "content": [{"type": "text", "text": "part"}]},
            },
        ]
    )
    trace = convert(events, [], agent.metadata, "test", output)
    validate_trajectory(trace)
    assert trace["session_id"] == "real-session"
    _, boundary, partial = trace["steps"]
    a, b = [child["steps"][0] for child in trace["subagent_trajectories"]]
    for step in [a, b]:
        assert step["observation"]["results"][0]["content"] == step["extra"]["parent_tool_use_id"]
    assert boundary["extra"]["compact_metadata"] == {"trigger": "auto"}
    assert partial["extra"]["response_status"] == "incomplete"


def test_replayed_user_keeps_role_and_typed_tool_payload(tmp_path):
    output = AgentOutput(tmp_path, ClaudeCodeAgent().metadata)
    reference = {"type": "tool_reference", "tool_name": "Read"}
    events = [
        {"type": "user", "isReplay": True, "message": {"content": "original user turn"}},
        {"type": "user", "isCompactSummary": True, "message": {"content": "summary"}},
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "missing", "content": [reference]}
                ]
            },
        },
    ]
    trace = convert(events, [], output.result["metadata"], "goal", output)
    validate_trajectory(trace)
    _, replay, summary, unlinked = trace["steps"]
    assert (replay["source"], summary["source"]) == ("user", "system")
    assert unlinked["extra"]["unresolved_tool_call_id"] == "missing"
    assert unlinked["observation"]["results"][0]["extra"]["native_content"] == [reference]


@pytest.mark.parametrize("mid", ["", "reused-id"])
def test_wire_calls_define_step_boundaries_and_final_usage(tmp_path, mid):
    from envweaver.agents.adapters.claude_code.capture import MessageStream

    meta = ClaudeCodeAgent().metadata
    out = AgentOutput(tmp_path, meta)
    events, records = [], []
    for i, stop, blocks in [
        (
            1,
            "tool_use",
            [
                {"type": "text", "text": "read"},
                {"type": "tool_use", "id": "t", "name": "Read", "input": {}},
            ],
        ),
        (2, "end_turn", [{"type": "text", "text": "done"}]),
    ]:
        wire = MessageStream()
        wire.accept(
            {
                "type": "message_start",
                "message": {"id": mid, "content": [], "usage": {"output_tokens": 1}},
            }
        )
        for index, block in enumerate(blocks):
            wire.accept({"type": "content_block_start", "index": index, "content_block": block})
            events.append(
                {
                    "type": "assistant",
                    "message": {"id": mid, "content": [block], "usage": {"output_tokens": 1}},
                }
            )
        if i == 1:
            events.append(
                {
                    "type": "user",
                    "message": {
                        "content": [{"type": "tool_result", "tool_use_id": "t", "content": "test"}]
                    },
                }
            )
        wire.accept(
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop},
                "usage": {"output_tokens": 42},
            }
        )
        wire.accept({"type": "message_stop"})
        records.extend(
            [
                {"id": str(i), "request": {"messages": []}},
                {"id": str(i), "status": 200, "message": wire.message, "complete": wire.complete},
            ]
        )
    trace = convert(events, records, meta, "goal", out)
    validate_trajectory(trace)
    _, first, second = trace["steps"]
    assert first["observation"]["results"][0]["source_call_id"] == "t"
    for index, step in enumerate((first, second), 1):
        assert step["extra"]["call_id"] == str(index)
        assert step["extra"]["context"]["complete"]
        assert step["metrics"]["completion_tokens"] == 42


def test_capture_route_preserves_cli_settings_and_provider(tmp_path, monkeypatch):
    import os

    from envweaver.agents.adapters.claude_code import capture

    for key in list(os.environ):
        if key.startswith(("ANTHROPIC_", "CLAUDE_")):
            monkeypatch.delenv(key)
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(capture, "_MANAGED_DIR", managed)
    project = tmp_path / "project"
    work = project / "src"
    work.mkdir(parents=True)
    monkeypatch.chdir(work)
    config = tmp_path / "user"
    config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))

    assert capture.capture_upstream([]) is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert capture.capture_upstream([]) == "https://api.anthropic.com"
    assert capture.capture_upstream(["--settings", "gateway.json"]) is None
    assert capture.capture_upstream(['--settings={"env":{}}']) is None

    (project / ".claude").mkdir()
    for path in (
        config / "settings.json",
        project / ".claude/settings.local.json",
        managed / "managed-settings.json",
    ):
        path.write_text('{"env":{"ANTHROPIC_BASE_URL":"http://gateway.test/v1"}}')
        assert capture.capture_upstream([]) is None
        path.unlink()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://local-gateway/tenant")
    assert capture.capture_upstream([]) == "http://local-gateway/tenant"
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    assert capture.capture_upstream([]) is None
