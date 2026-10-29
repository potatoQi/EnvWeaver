import base64
import importlib
import json
import shutil
import sys

import pytest

from envweaver import CodexAgent, Instance, MiniSweAgent, PiAgent
from envweaver.agents.execution import AgentExecution
from tests.agents.test_cli_agents import LocalTransport
from tests.agents.test_cli_agents import sandbox as sandbox_fixture

sandbox = sandbox_fixture
recovery = importlib.import_module("envweaver._io.retry")


@pytest.mark.parametrize("adapter", [CodexAgent, PiAgent, MiniSweAgent])
@pytest.mark.parametrize("action", ["poll", "read", "stop"])
def test_agent_rpc_recovers_after_lost_reply(tmp_path, monkeypatch, adapter, action):
    from envweaver.agents.adapters import _process

    agent = adapter()
    runtime = LocalTransport()
    instance = Instance("same-sandbox", "local")
    agent._runtime, agent._instance = runtime, instance
    agent._python = sys.executable
    agent._remote = str(tmp_path)
    shutil.copyfile(_process.__file__, tmp_path / "process.py")
    directory = tmp_path / "run"
    directory.mkdir()
    (directory / "stdout").write_bytes("开头\n回复\n".encode())
    payload = {"stdout": len("开头\n".encode())} if action == "poll" else {
        "path": str(directory / "stdout"),
    }
    original = runtime.exec
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        response = original(**kwargs)
        if len(calls) < 3:
            raise ConnectionResetError("SSH response lost")
        return response

    monkeypatch.setattr(runtime, "exec", execute)
    monkeypatch.setattr(recovery.time, "sleep", lambda delay: None)
    if action == "stop":
        agent._cancelled.set()  # Recovery must also work during shutdown.
    result = agent._rpc(action, str(directory), payload)
    assert calls[0] == calls[1] == calls[2]
    assert all(call["instance"] is instance for call in calls)
    if action == "poll":
        assert base64.b64decode(result["streams"]["stdout"]["data"]) == "回复\n".encode()
    elif action == "read":
        assert base64.b64decode(result["data"]) == (directory / "stdout").read_bytes()
    else:
        assert (directory / "cancel").exists()


@pytest.mark.parametrize("adapter", [CodexAgent, PiAgent, MiniSweAgent])
def test_agent_start_does_not_relaunch_after_ambiguous_failure(tmp_path, monkeypatch, adapter):
    agent = adapter()
    agent._python, agent._remote = sys.executable, str(tmp_path)
    agent._instance = Instance("same", "test")
    agent._runtime = LocalTransport()
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        raise ConnectionResetError("reply lost; process may have started")

    monkeypatch.setattr(agent._runtime, "exec", execute)
    from envweaver.errors import AgentError
    with pytest.raises(AgentError):
        agent._rpc("start", str(tmp_path), {"command": ["may-have-started"]})
    assert len(calls) == 1


@pytest.mark.parametrize("adapter", [CodexAgent, PiAgent])
def test_cli_recovers_without_duplicate_events_or_launches(tmp_path, sandbox, monkeypatch, adapter):
    runtime, instance, env = sandbox
    agent = adapter(env=env)
    agent_execution = AgentExecution(agent)
    directory = tmp_path / "artifacts"
    agent_execution.prepare(runtime=runtime, instance=instance, output_dir=directory, cwd=str(tmp_path))
    original = runtime.exec
    launches = []
    failures = []

    def execute(**kwargs):
        command = kwargs["command"]
        response = original(**kwargs)
        if len(command) > 2 and command[1].endswith("process.py"):
            action = command[2]
            if action == "start":
                launches.append(command)
            if action in {"poll", "read"} and action not in failures:
                failures.append(action)
                raise ConnectionResetError("lost response")
        return response

    monkeypatch.setattr(runtime, "exec", execute)
    monkeypatch.setattr(recovery.time, "sleep", lambda delay: None)
    try:
        result = agent_execution.run(goal=[{"type": "text", "text": "solve"}], runtime=runtime,
                           instance=instance, output_dir=directory)
        assert result["output"]
        assert len(launches) == 1
        assert "poll" in failures
        events = [json.loads(line) for line in (directory / "logs/events.jsonl").read_text().splitlines()]
        types = [event["type"] for event in events]
        terminal = "codex.turn.completed" if adapter is CodexAgent else "pi.agent_settled"
        assert types.count(terminal) == 1
    finally:
        agent_execution.stop()
        shutil.rmtree(agent._remote)
