import base64
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from envweaver import CodexAgent, Instance, MiniSweAgent, PiAgent, Runtime
from envweaver.agents.execution import AgentExecution
from envweaver.errors import AgentError, ErrorType


class LocalTransport(Runtime):
    """Exercise the actual remote supervisor on local OS processes, without a model."""

    def exec(self, instance, command, cwd=None, timeout=300):
        result = subprocess.run(command, cwd=cwd, timeout=timeout, capture_output=True, text=True)
        return result.returncode, result.stdout, result.stderr

    def _open_pipe(self, instance, command):
        from envweaver._io.process import open_pipe

        return open_pipe(command)

    def upload(self, instance, source, target):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if Path(source).is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copyfile(source, target)


@pytest.fixture
def sandbox(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    script = """#!PYTHON
import json, os, subprocess, sys, time
from pathlib import Path
if '--version' in sys.argv:
    print('test-cli 1.0'); raise SystemExit(0)
Path('argv.json').write_text(json.dumps(sys.argv[1:]))
if os.environ.get('EVENTS'):
    for event in json.loads(os.environ['EVENTS']):
        print(json.dumps(event), flush=True)
    raise SystemExit(int(os.environ.get('EXIT_CODE', '0')))
if os.environ.get('SLOW'):
    child = subprocess.Popen([sys.executable, '-c', "import time; from pathlib import Path; time.sleep(1); Path('escaped-child').touch()"], start_new_session=True)
    Path('child.pid').write_text(str(child.pid))
    print(json.dumps({'type':'message', 'text':'partial'}), flush=True)
    time.sleep(30)
    Path('late-cli').touch()
elif Path(sys.argv[0]).name == 'codex':
    print(json.dumps({'type':'item.completed', 'item':{'type':'agent_message','text':'完成'}}), flush=True)
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':3}}), flush=True)
    Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(os.environ.get('FINAL_OUTPUT', '完成'))
else:
    print(json.dumps({'type':'message_end','message':{'role':'assistant','content':[{'type':'text','text':'pi complete'}], 'model':'test-model','usage':{'input':5},'stopReason':'stop'}}), flush=True)
    print(json.dumps({'type':'agent_settled','aborted':False}), flush=True)
""".replace("PYTHON", sys.executable)
    for name in ("codex", "pi"):
        path = binary / name
        path.write_text(script)
        path.chmod(0o755)
    return (
        LocalTransport(),
        Instance("test", "local", data={"python": sys.executable}),
        {"PATH": str(binary) + os.pathsep + os.environ["PATH"]},
    )


@pytest.mark.parametrize("adapter,answer", [(CodexAgent, "完成"), (PiAgent, "pi complete")])
def test_real_process_streams_and_outputs(tmp_path, sandbox, adapter, answer):
    runtime, instance, env = sandbox
    agent = adapter(
        model="test-model", env=env
    )
    agent_execution = AgentExecution(agent)
    directory = tmp_path / "artifacts"
    agent_execution.prepare(runtime=runtime, instance=instance, output_dir=directory, cwd=str(tmp_path))
    try:
        result = agent_execution.run(
            goal=[{"type": "text", "text": "literal $HOME `x` \n second line"}],
            runtime=runtime,
            instance=instance,
            output_dir=directory,
        )
        assert result["output"] == [{"type": "text", "text": answer}]
        assert result == json.loads((directory / "result.json").read_text())
        events = [
            json.loads(line) for line in (directory / "logs/events.jsonl").read_text().splitlines()
        ]
        assert len(events) >= 3
        assert [item["seq"] for item in events] == list(range(1, len(events) + 1))
        assert (directory / "logs/stdout.log").read_text()
        if adapter is CodexAgent:
            argv = json.loads((tmp_path / "argv.json").read_text())
            assert argv[-1] == "literal $HOME `x` \n second line"
    finally:
        agent_execution.stop()
        shutil.rmtree(agent._remote, ignore_errors=True)


@pytest.mark.parametrize("final_output", ["final reply", ""])
def test_codex_final_file_replaces_streamed_output(tmp_path, sandbox, final_output):
    runtime, instance, env = sandbox
    agent = CodexAgent(env={**env, "FINAL_OUTPUT": final_output})
    agent_execution = AgentExecution(agent)
    output_dir = tmp_path / "artifacts"
    agent_execution.prepare(runtime=runtime, instance=instance, output_dir=output_dir, cwd=str(tmp_path))
    try:
        result = agent_execution.run(
            goal=[{"type": "text", "text": "solve"}],
            runtime=runtime, instance=instance, output_dir=output_dir,
        )
        assert result["output"] == (
            [{"type": "text", "text": final_output}] if final_output else []
        )
        assert result == json.loads((output_dir / "result.json").read_text())
    finally:
        agent_execution.stop()
        shutil.rmtree(agent._remote, ignore_errors=True)


def test_stop_ends_cli_and_preserves_background_processes(tmp_path, sandbox):
    runtime, instance, env = sandbox
    agent = CodexAgent(
        env={**env, "SLOW": "1"}
    )
    agent_execution = AgentExecution(agent)
    directory = tmp_path / "artifacts"
    agent_execution.prepare(runtime=runtime, instance=instance, output_dir=directory, cwd=str(tmp_path))
    service = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                agent_execution.run,
                goal=[{"type": "text", "text": "wait"}],
                runtime=runtime,
                instance=instance,
                output_dir=directory,
            )
            deadline = time.monotonic() + 5
            while not (tmp_path / "child.pid").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (tmp_path / "child.pid").exists()
            agent_execution.stop(timeout=3)
            with pytest.raises(InterruptedError):
                future.result(timeout=3)
        time.sleep(1.1)
        assert (tmp_path / "escaped-child").exists()
        assert not (tmp_path / "late-cli").exists()
        assert service.poll() is None
        assert "partial" in (directory / "logs/stdout.log").read_text()
    finally:
        service.kill()
        service.wait()
        agent_execution.stop()
        shutil.rmtree(agent._remote, ignore_errors=True)


def test_image_input_uploaded_before_codex_invocation(tmp_path, sandbox):
    runtime, instance, env = sandbox
    image = tmp_path / "image.png"
    image.write_bytes(b"image fixture")
    goal = [{"type": "image", "path": str(image)}]
    agent = CodexAgent(env=env)
    agent_execution = AgentExecution(agent)
    directory = tmp_path / "artifacts"
    agent_execution.prepare(runtime=runtime, instance=instance, output_dir=directory, cwd=str(tmp_path))
    try:
        agent_execution.run(goal=goal, runtime=runtime, instance=instance, output_dir=directory)
        argv = json.loads((tmp_path / "argv.json").read_text())
        remote_image = Path(argv[argv.index("--image") + 1])
        assert remote_image.read_bytes() == image.read_bytes()
        assert goal[0]["path"] == str(image)
    finally:
        agent_execution.stop()
        shutil.rmtree(agent._remote, ignore_errors=True)


@pytest.mark.parametrize("status,reason", [
    ("Submitted", None), ("LimitsExceeded", "other_limit"),
    ("TimeExceeded", "timeout"), ("ContextWindowExceededError", "context_limit"),
    ("AuthenticationError", "error"),
])
def test_mini_reads_incremental_native_snapshots(tmp_path, sandbox, status, reason):
    runtime, instance, env = sandbox
    module = tmp_path / "modules/minisweagent/run"
    module.mkdir(parents=True)
    (tmp_path / "modules/mcp").mkdir()
    (tmp_path / "modules/mcp/types.py").write_text("CacheableResult = object\n")
    (module / "mini.py").write_text("""
import json,os,sys,time
from pathlib import Path
path = Path(sys.argv[sys.argv.index('--output')+1])
messages = [{'role':'assistant','content':'first'}]
path.write_text(json.dumps({'info':{}, 'messages':messages}))
time.sleep(0.15)
messages.append({'role':'exit','extra':{'submission':'fixed'}})
path.write_text(json.dumps({'info':{'exit_status':os.environ['EXIT_STATUS'],'submission':'fixed','config':{'secret':'hidden'},'model_stats':{'api_calls':1}},'messages':messages}))
""")
    (tmp_path / "modules/litellm.py").write_text("# No model call in this lifecycle fixture.\n")
    metadata = tmp_path / "modules/mini_swe_agent-2.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: mini-swe-agent\nVersion: 2.0\n"
    )
    agent = MiniSweAgent(
        env={**env, "PYTHONPATH": str(tmp_path / "modules"), "EXIT_STATUS": status},
    )
    agent_execution = AgentExecution(agent)
    directory = tmp_path / "artifacts"
    agent_execution.prepare(runtime=runtime, instance=instance, output_dir=directory, cwd=str(tmp_path))
    try:
        def run():
            return agent_execution.run(
                goal=[{"type": "text", "text": "fix"}], runtime=runtime,
                instance=instance, output_dir=directory,
            )

        if reason:
            expected = {"error": RuntimeError, "cancelled": InterruptedError}.get(reason, AgentError)
            with pytest.raises(expected) as caught:
                run()
            if isinstance(caught.value, AgentError):
                if reason == "error":
                    assert not caught.value.type.is_limit
                else:
                    assert caught.value.type is ErrorType(reason)
            result = json.loads((directory / "result.json").read_text())
        else:
            result = run()
        assert result["output"] == [{"type": "text", "text": "fixed"}]
        events = [
            json.loads(line) for line in (directory / "logs/events.jsonl").read_text().splitlines()
        ]
        assert len([event for event in events if event["type"] == "mini.message"]) == 2
        assert "hidden" not in (directory / "logs/mini.traj.json").read_text()
    finally:
        agent_execution.stop()
        shutil.rmtree(agent._remote, ignore_errors=True)


def test_cancel_before_process_launch_has_terminal_status(tmp_path):
    from envweaver.agents.adapters import _process

    directory = tmp_path / "process"
    _process.main("stop", directory, {})
    assert _process.main("start", directory, {"command": ["must-not-run"]}) == {"started": False}
    assert _process.main("poll", directory, {})["status"]["code"] == -1


@pytest.mark.parametrize("offsets", [{}, {"stdout": 2}, {"stdout": 2, "stderr": 0}])
def test_process_poll_reads_only_requested_streams(tmp_path, monkeypatch, offsets):
    from envweaver.agents.adapters import _process

    contents = {"stdout": b"output", "stderr": b"error"}
    for name, data in contents.items():
        (tmp_path / name).write_bytes(data)
    status = {"code": 0, "offsets": {name: len(data) for name, data in contents.items()}}
    (tmp_path / "status.json").write_text(json.dumps(status))
    original_open = Path.open

    def open_path(path, *args, **kwargs):
        if path.name in contents:
            assert path.name in offsets, "Status polling must not read unused log streams"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_path)
    result = _process.main("poll", tmp_path, offsets)
    assert result["status"] == status
    assert result["streams"] == {
        name: {
            "offset": len(contents[name]),
            "data": base64.b64encode(contents[name][offset:]).decode(),
        }
        for name, offset in offsets.items()
    }


def test_pi_image_output_is_archived(tmp_path, sandbox):
    runtime, instance, env = sandbox
    agent = PiAgent(env=env)
    agent_execution = AgentExecution(agent)
    output_dir = tmp_path / "result"
    agent_execution.prepare(runtime=runtime, instance=instance, output_dir=output_dir, cwd=str(tmp_path))
    try:
        event = {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "image",
                        "mimeType": "image/png",
                        "data": base64.b64encode(b"png fixture").decode(),
                    }
                ],
            },
        }
        agent._event_line(json.dumps(event))
        result = json.loads((output_dir / "result.json").read_text())
        image_path = result["output"][0]["path"]
        assert image_path in result["artifacts"]["files"]
        assert (output_dir / image_path).read_bytes() == b"png fixture"
    finally:
        agent_execution.stop()
        shutil.rmtree(agent._remote, ignore_errors=True)


@pytest.mark.parametrize("available,version", [(True, None), (False, None), (True, "1.2.3")])
def test_prepare_reuses_or_installs_cli(tmp_path, sandbox, monkeypatch, available, version):
    runtime, instance, env = sandbox
    agent = CodexAgent(env=env, version=version)
    agent_execution = AgentExecution(agent)
    checks = iter([(0 if available else 1, "existing"), (0, "installed")])
    installations = []
    dependencies = []

    def execute(command, *, preparing=False, capture_stdout=False):
        assert preparing
        if command == ["codex", "--version"]:
            assert capture_stdout
            return next(checks)
        assert not capture_stdout
        installations.append(command)
        return 0, ""

    monkeypatch.setattr(agent, "_execute", execute)
    monkeypatch.setattr(agent, "_ensure_dependencies", lambda: dependencies.append(True))
    try:
        agent_execution.prepare(runtime=runtime, instance=instance, output_dir=tmp_path / "result", cwd=str(tmp_path))
        should_install = not available or version is not None
        assert len(installations) == len(dependencies) == int(should_install)
        if should_install:
            assert installations[0][-1] == "@openai/codex" + (f"@{version}" if version else "")
        result = json.loads((tmp_path / "result/result.json").read_text())
        assert result["metadata"]["agent_version"] == ("installed" if should_install else "existing")
    finally:
        agent_execution.stop()
        shutil.rmtree(agent._remote, ignore_errors=True)


def test_stop_interrupts_dependency_installation(tmp_path, sandbox):
    runtime, instance, env = sandbox
    marker = tmp_path / "installing"

    class InstallingAgent(CodexAgent):
        def _version_command(self):
            return ["nonexistent-envweaver-cli"]

        def _ensure_dependencies(self):
            self._execute(
                [
                    self._python,
                    "-c",
                    "from pathlib import Path; import time; "
                    f'Path({str(marker)!r}).touch(); print("installing", flush=True); time.sleep(30)',
                ],
                preparing=True,
            )

    agent = InstallingAgent(env=env)
    agent_execution = AgentExecution(agent)
    directory = tmp_path / "result"
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                agent_execution.prepare, runtime=runtime, instance=instance,
                output_dir=directory, cwd=str(tmp_path),
            )
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists()
            agent_execution.stop(timeout=3)
            with pytest.raises(InterruptedError):
                future.result(timeout=3)
        assert "installing" in (directory / "logs/prepare.log").read_text()
    finally:
        agent_execution.stop()
        shutil.rmtree(agent._remote, ignore_errors=True)


@pytest.mark.parametrize("adapter", [CodexAgent, PiAgent])
def test_prepare_cwd_controls_cli_process(tmp_path, sandbox, monkeypatch, adapter):
    runtime, instance, env = sandbox
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    agent = adapter(env=env)
    agent_execution = AgentExecution(agent)
    directory = tmp_path / "artifacts"
    agent_execution.prepare(
        runtime=runtime, instance=instance, output_dir=directory,
        cwd=str(task_dir),
    )
    try:
        agent_execution.run(
            goal=[{"type": "text", "text": "solve"}], runtime=runtime,
            instance=instance, output_dir=directory,
        )
        assert (task_dir / "argv.json").is_file()
        assert not (tmp_path / "argv.json").exists()
    finally:
        agent_execution.stop()
        shutil.rmtree(agent._remote, ignore_errors=True)


@pytest.mark.parametrize("is_file", [False, True])
def test_prepare_rejects_unusable_cwd(tmp_path, sandbox, is_file):
    runtime, instance, env = sandbox
    directory = tmp_path / "unusable"
    if is_file:
        directory.write_text("not a directory")
    agent = CodexAgent(env=env)
    agent_execution = AgentExecution(agent)
    with pytest.raises(RuntimeError, match="Cannot enter Agent working directory"):
        agent_execution.prepare(
            runtime=runtime, instance=instance, output_dir=tmp_path / "artifacts",
            cwd=str(directory),
        )
    assert agent._remote is None
    agent_execution.stop()


@pytest.mark.parametrize("adapter,events,code,reason", [
    (CodexAgent, [{"type": "turn.failed", "error": {"message": "Your input exceeds the context window of this model"}}], 1, "context_limit"),
    (CodexAgent, [{"type": "turn.failed", "error": {"message": "401 Invalid API key"}}], 1, "error"),
    (PiAgent, [{"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "errorMessage": "context_length_exceeded"}}], 0, "context_limit"),
    (PiAgent, [{"type": "message_end", "message": {"role": "assistant", "stopReason": "length"}}], 0, "token_limit"),
    (PiAgent, [{"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "errorMessage": "Rate limit: too many tokens"}}], 0, "error"),
    (PiAgent, [{"type": "agent_settled", "aborted": True}], 0, "cancelled"),
    (PiAgent, [
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "errorMessage": "prompt is too long"}},
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "recovered"}]}},
        {"type": "agent_end"},
    ], 0, None),
])
def test_cli_terminal_limits_and_failures(tmp_path, sandbox, adapter, events, code, reason):
    runtime, instance, env = sandbox
    agent = adapter(env={**env, "EVENTS": json.dumps(events), "EXIT_CODE": str(code)})
    agent_execution = AgentExecution(agent)
    directory = tmp_path / "result"
    try:
        agent_execution.prepare(runtime=runtime, instance=instance, output_dir=directory, cwd=str(tmp_path))

        def run():
            return agent_execution.run(
                goal=[{"type": "text", "text": "solve"}], runtime=runtime,
                instance=instance, output_dir=directory,
            )

        if reason is None:
            assert run()["output"][0]["text"] == "recovered"
        else:
            expected = {"error": RuntimeError, "cancelled": InterruptedError}.get(reason, AgentError)
            with pytest.raises(expected) as caught:
                run()
            if isinstance(caught.value, AgentError):
                if reason == "error":
                    assert not caught.value.type.is_limit
                else:
                    assert caught.value.type is ErrorType(reason)
        assert (directory / "result.json").exists()
        assert agent._process_dir is None
    finally:
        agent_execution.stop()
        if agent._remote:
            shutil.rmtree(agent._remote, ignore_errors=True)


@pytest.mark.parametrize("adapter,events", [
    (CodexAgent, [{"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}}]),
    (PiAgent, [{"type": "message_end", "message": {"role": "assistant", "stopReason": "stop", "content": []}}]),
])
def test_zero_exit_without_terminal_event_is_not_success(tmp_path, sandbox, monkeypatch, adapter, events):
    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.01)
    runtime, instance, env = sandbox
    agent = adapter(env={**env, "EVENTS": json.dumps(events)})
    agent_execution = AgentExecution(agent)
    directory = tmp_path / "result"
    try:
        agent_execution.prepare(runtime=runtime, instance=instance, output_dir=directory, cwd=str(tmp_path))
        with pytest.raises(AgentError) as caught:
            agent_execution.run(goal=[{"type": "text", "text": "solve"}], runtime=runtime,
                      instance=instance, output_dir=directory)
        assert caught.value.type is ErrorType.PROVIDER_TRANSIENT
        assert (directory / "result.json").exists()
        assert agent._process_dir is None
    finally:
        agent_execution.stop()
        if agent._remote:
            shutil.rmtree(agent._remote, ignore_errors=True)


@pytest.mark.parametrize("kind", [CodexAgent, PiAgent, MiniSweAgent])
def test_new_preserves_cli_config_and_isolates_mutable_options(kind):
    agent = kind(model="test-model", version="2.4.6", env={"API_KEY": "secret"}, extra_args=["--option"])
    identity = agent._identity()
    agent.metadata["agent_version"] = "discovered version"
    agent._cancelled.set()
    first, second = agent.new(), agent.new()
    assert first._identity() == second._identity() == identity
    assert first.metadata["agent_version"] == "2.4.6"
    first._env["API_KEY"] = "changed"
    first._extra_args.append("--another")
    assert first._identity() == identity
    assert first.new().config == agent.config
    assert second._env == agent._env == {"API_KEY": "secret"}
    assert second._extra_args == agent._extra_args == ["--option"]
    assert not first._cancelled.is_set() and first._output is None
    assert first._process_lock is not second._process_lock
    assert first._process_dir is None and first._remote is None
