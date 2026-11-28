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
    from envweaver.agents.adapters.sandbox import _process

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
    delays = []
    if action == "stop":
        agent._cancelled.set()  # Recovery must also work during shutdown.
        monkeypatch.setattr(recovery.time, "sleep", delays.append)
    else:
        monkeypatch.setattr(agent._cancelled, "wait", delays.append)
    result = agent._rpc(action, str(directory), payload)
    assert delays == [0.5, 1.0]
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


@pytest.mark.parametrize(("action", "attempts"), [("poll", 3), ("start", 1)])
def test_persistently_truncated_process_reply(tmp_path, monkeypatch, action, attempts):
    from envweaver.errors import AgentError, ErrorType

    agent = PiAgent()
    agent._runtime, agent._instance = LocalTransport(), Instance("test", "local")
    agent._python, agent._remote = sys.executable, str(tmp_path)
    replies = []

    def execute(**kwargs):
        replies.append(kwargs["command"])
        return 0, '{"status":', ""

    monkeypatch.setattr(agent._runtime, "exec", execute)
    monkeypatch.setattr(agent._cancelled, "wait", lambda delay: False)
    with pytest.raises(AgentError) as failure:
        agent._rpc(action, str(tmp_path))
    assert failure.value.type is ErrorType.TRANSPORT
    assert len(replies) == attempts and all(command == replies[0] for command in replies)


def test_truncated_replies_preserve_running_agent_and_final_atif(tmp_path, sandbox, monkeypatch):
    from envweaver.agents.capture import _SCAN
    from envweaver.runtime.docker import DockerBackend
    from envweaver.trajectory.atif import validate_trajectory

    runtime, _, env = sandbox
    instance = Instance("test", "docker", data={"python": sys.executable, "host_ssh": None})
    agent = PiAgent(env=env)
    run = AgentExecution(agent)
    output = tmp_path / "artifacts"
    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.03)
    run.prepare(runtime=runtime, instance=instance, output_dir=output, cwd=str(tmp_path))

    user = {"role": "user", "content": "solve"}
    answer = {"role": "assistant", "content": [{"type": "text", "text": "done"}], "stopReason": "stop"}
    events = [
        {"type": "message", "call_id": None, "data": user},
        {"type": "request", "call_id": "turn1", "data": {"payload": {"messages": [user]}}},
        {"type": "message", "call_id": "turn1", "data": answer},
    ]
    (tmp_path / "events").write_text("".join(json.dumps(event) + "\n" for event in events))
    (tmp_path / "bin/pi").write_text(f"""#!{sys.executable}
import json, sys, time
from pathlib import Path
trace = Path(sys.argv[sys.argv.index('--session') + 1]).with_name('events.jsonl')
trace.parent.mkdir(exist_ok=True)
first, *rest = Path('events').read_text().splitlines(keepends=True)
trace.write_text(first)
for _ in range(1000):
    if Path('continue').exists():
        break
    time.sleep(0.01)
else:
    raise RuntimeError('live capture never recovered')
with trace.open('a') as f:
    f.writelines(rest)
Path('answer.txt').write_text('done')
print(json.dumps({{'type': 'message_end', 'message': json.loads(rest[-1])['data']}}))
print(json.dumps({{'type': 'agent_settled'}}))
""")
    backend = DockerBackend()
    local_exec = runtime.exec
    launches, polls, scans = [], [], []
    finished = False

    def docker_exec(action, cid, python, flag, script, payload, **kwargs):
        nonlocal finished
        command = json.loads(payload)["command"]
        code, stdout, stderr = local_exec(instance, command)
        if command[1].endswith("/process.py"):
            if command[2] == "start":
                launches.append(command)
            if command[2] == "poll":
                polls.append(command)
                if len(polls) == 1:
                    return '{"exit_code": 0, "stdout": "'  # Outer Docker exec envelope.
                finished = json.loads(stdout)["status"] is not None
        if command[1:3] == ["-c", _SCAN] and (finished or json.loads(stdout)):
            if not scans or finished != scans[-1][0]:
                stdout = stdout[:len(stdout) // 2]  # Inner scanner reply.
            elif not finished:
                (tmp_path / "continue").touch()
            scans.append((finished, len(polls)))
        return json.dumps({"exit_code": code, "stdout": stdout, "stderr": stderr})

    monkeypatch.setattr(backend, "_docker", docker_exec)
    monkeypatch.setattr(runtime, "exec", backend.exec)
    try:
        result = run.run(goal=[{"type": "text", "text": "solve"}], runtime=runtime,
                         instance=instance, output_dir=output)
        assert len(launches) == 1 and polls[0] == polls[1]
        assert not scans[1][0] and scans[1][1] > scans[0][1]  # Next live refresh.
        assert scans[-2:] == [(True, len(polls))] * 2  # Retry the final read.
        assert result["output"] == [{"type": "text", "text": "done"}]
        assert (tmp_path / "answer.txt").read_text() == "done"
        native = (output / "logs/native/pi/events.jsonl").read_text()
        assert [json.loads(line) for line in native.splitlines()] == events
        doc = json.loads((output / "trajectory.json").read_text())
        validate_trajectory(doc)
        question, reply = doc["steps"]
        assert question["source"] == "user" and question["message"] == "solve"
        assert reply["source"] == "agent" and reply["extra"]["native_message"] == answer
        context = reply["extra"]["context"]
        assert context["complete"] and json.loads((output / context["path"]).read_text()) == {"messages": [user]}
    finally:
        run.stop()
        shutil.rmtree(agent._remote)
