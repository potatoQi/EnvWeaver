"""CLI transport follows adapter capabilities, independently of executable names."""

import json
from unittest.mock import Mock

import pytest

from envweaver import AgentOutput, CodexAgent, MiniSweAgent, PiAgent


@pytest.mark.parametrize("adapter,kind", [
    (CodexAgent, "node"), (PiAgent, "node"), (MiniSweAgent, "pip"),
])
def test_dependency_selection_does_not_depend_on_executable(adapter, kind):
    agent = adapter()
    agent.executable = "renamed-cli"
    agent._remote = "/agent"
    agent._python = "python3"
    agent._runtime = Mock()
    agent._instance = object()
    agent._sandbox_path = "/usr/bin"
    agent._sandbox_pythonpath = "/existing/python"
    agent._execute = Mock(return_value=(0, ""))

    agent._ensure_dependencies()

    agent._execute.assert_called_once_with(
        ["python3", "/agent/dependencies.py", kind, "/agent/dependencies"], preparing=True,
    )
    if kind == "pip":
        assert agent._process_env()["PYTHONPATH"].split(":") == [
            "/agent/packages", "/agent/dependencies/pip", "/existing/python",
        ]
        assert "PATH" not in agent._dependency_env
    else:
        assert agent._process_env()["PATH"].split(":") == [
            "/agent/dependencies/node/bin", "/agent/npm/node_modules/.bin", "/usr/bin",
        ]
        assert "PYTHONPATH" not in agent._dependency_env


@pytest.mark.parametrize("adapter", [CodexAgent, PiAgent, MiniSweAgent])
def test_stdout_event_selection_does_not_depend_on_executable(tmp_path, adapter):
    agent = adapter()
    agent.executable = "mini" if adapter is not MiniSweAgent else "renamed-cli"
    agent._output = AgentOutput(tmp_path / "output", agent.metadata)
    agent._on_event = Mock()
    event = {"type": "test", "text": "文字"}
    text = json.dumps(event, ensure_ascii=False) + "\n"
    agent._consume("stdout", text[:7], preparing=False)
    agent._consume("stdout", text[7:], preparing=False)

    assert (agent._output.directory / "logs/stdout.log").read_text() == text
    if adapter is MiniSweAgent:
        agent._on_event.assert_not_called()
        assert agent._output.result["artifacts"]["trajectory"] is None
    else:
        agent._on_event.assert_called_once_with(event)


def test_codex_does_not_interpret_other_agents_image_fields(tmp_path):
    agent = CodexAgent()
    agent._output = AgentOutput(tmp_path / "output", agent.metadata)
    agent._read_remote = Mock(side_effect=AssertionError("must not infer an image path"))
    event = {"type": "item.completed", "item": {
        "type": "image", "path": "/workspace/arbitrary-file", "url": "data:image/png;base64,invalid",
    }}

    agent._event_line(json.dumps(event))

    assert agent._output.result["artifacts"]["files"] == []
    recorded = json.loads((agent._output.directory / "logs/events.jsonl").read_text())
    assert recorded["data"] == event


@pytest.mark.parametrize("image", [
    {"type": "image", "path": "/workspace/image.png"},
    {"image_url": "data:image/png;base64,invalid"},
    {"type": "output_image", "data": "invalid"},
])
def test_pi_ignores_non_native_image_shapes(tmp_path, image):
    agent = PiAgent()
    agent._output = AgentOutput(tmp_path / "output", agent.metadata)
    agent._read_remote = Mock(side_effect=AssertionError("must not infer an image path"))

    agent._archive_media(image)

    assert agent._output.result["artifacts"]["files"] == []


@pytest.mark.parametrize("mime", [None, "application/json", 1])
def test_pi_requires_native_image_mime_type(tmp_path, mime):
    agent = PiAgent()
    agent._output = AgentOutput(tmp_path / "output", agent.metadata)
    with pytest.raises(ValueError, match="mimeType"):
        agent._image_file({"type": "image", "data": "cG5n", "mimeType": mime})
    assert agent._output.result["artifacts"]["files"] == []
