"""Adapter contracts without a model, Docker, or a host Harbor installation."""

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from envweaver import AgentOutput, ErrorType, Instance, Terminus2Agent
from envweaver.agents.adapters._cli import CliAgent
from tests.agents.test_cli_agents import LocalTransport


def test_cache_disabled_does_not_start_cache_service(tmp_path, monkeypatch):
    agent = Terminus2Agent(model="openai/test", cache=False)
    output = AgentOutput(tmp_path / "output", agent.metadata)
    replies = iter([(1, ""), (0, ""), (0, "2.0.0")])
    monkeypatch.setattr(agent, "_execute", lambda *a, **kw: next(replies))
    monkeypatch.setattr(agent, "_ensure_dependencies", lambda: None)
    monkeypatch.setattr(
        "envweaver.agents.adapters._cli.PreparationDownloads",
        lambda *a, **kw: pytest.fail("cache=False must bypass the cache bridge"),
    )
    try:
        CliAgent.prepare(
            agent, runtime=LocalTransport(),
            instance=Instance("local", "fake", None, {"python": sys.executable}),
            output=output, cwd=str(tmp_path),
        )
        assert not (Path(agent._remote) / "_uv_install.py").exists()
        assert agent._agent_python.endswith("/venv/bin/python")
        assert output.result["metadata"]["agent_version"] == "2.0.0"
    finally:
        if agent._remote:
            shutil.rmtree(agent._remote)


@pytest.mark.parametrize(
    "options",
    [
        {"model": ""},
        {"model": None},
        {"cache": "true"},
        {"env": {"KEY": 1}},
        {"env": {"bad=name": "a"}},
        {"options": []},
        {"options": {"logs_dir": "/tmp"}},
        {"options": {"record_terminal_session": True}},
        {"options": {"extra_env": {}}},
        {"options": {"episodes": 1}},
        {"options": {"max_episodes": 1}},
        {"options": {"temperature": float("nan")}},
    ],
)
def test_invalid_configuration(options):
    with pytest.raises((ValueError, TypeError)):
        Terminus2Agent(**{"model": "openai/test", **options})


def test_clone_is_independent_and_no_harbor_needed():
    agent = Terminus2Agent(model="openai/test", env={"KEY": "secret"}, options={"max_turns": 2})
    clone = agent.new()
    assert clone.config == agent.config
    assert clone.config["cache"] is True
    assert Terminus2Agent(model="openai/test", cache=False).new().config["cache"] is False
    assert clone._cancelled is not agent._cancelled
    clone._cancelled.set()
    clone.config["options"]["max_turns"] = 99
    assert not agent._cancelled.is_set()
    assert agent.config["options"]["max_turns"] == 2
    agent.preflight([{"type": "text", "text": "hello"}])
    with pytest.raises(ValueError, match="image"):
        agent.preflight([{"type": "image", "path": "/tmp/photo.png"}])


@pytest.fixture
def adapter(tmp_path):
    agent = Terminus2Agent(
        model="openai/test",
        env={"API_KEY": 's"e\ncret'},
        options={"llm_kwargs": {"api_key": "option-key"}},
    )
    agent._native = SimpleNamespace(poll=lambda sources: False, files={})
    agent._remote = "/sandbox/agent"
    agent._output = AgentOutput(tmp_path, agent.metadata)
    return agent


def snapshot(**overrides):
    return {"completed": False, "error": None, "usage": {}, "trajectories": {}, **overrides}


def feed(agent, state):
    for trajectory in state.get("trajectories", {}).values():
        trajectory.setdefault("agent", {"name":"terminus-2", "version":"2.0.0"})
        for step in trajectory.get("steps", []):
            step.setdefault("source", "agent")
            step.setdefault("message", "")
    agent._read_remote = lambda path: json.dumps(state).encode()
    agent._poll_native()


def test_snapshots_are_incremental_and_secrets_are_redacted(adapter):
    state = snapshot(
        trajectories={"trajectory.json": {"steps": [{"step_id": 1, "message": 's"e\ncret'}]}}
    )
    feed(adapter, state)
    feed(adapter, state)
    state["trajectories"]["trajectory.json"]["steps"].append(
        {"step_id": 2, "message": "option-key"}
    )
    state["usage"] = {"n_input_tokens": 42}
    state["completed"] = True
    feed(adapter, state)
    events = [
        json.loads(line)
        for line in (adapter._output.directory / "logs/events.jsonl").read_text().splitlines()
    ]
    assert [e["seq"] for e in events] == [1, 2]
    assert all(e["data"]["message"] == "[REDACTED]" for e in events)
    assert adapter._completed
    assert adapter._output.result["metadata"]["usage"] == {"n_input_tokens": 42}
    assert adapter._output.result["artifacts"]["logs"] == ["logs/events.jsonl", "logs/terminus/trajectory.json"]
    assert "[REDACTED]" in (adapter._output.directory / "logs/terminus/trajectory.json").read_text()


def test_continuations_and_missing_snapshot(adapter):
    adapter._read_remote = lambda path: None
    adapter._poll_native()
    feed(
        adapter,
        snapshot(
            trajectories={
                "trajectory.json": {"steps": [{"step_id": 1}]},
                "trajectory.cont-1.json": {"steps": [{"step_id": 1}]},
            }
        ),
    )
    assert len(adapter._output.result["artifacts"]["logs"]) == 3
    assert "trajectory.cont-1.json" in adapter._output.result["artifacts"]["files"]
    with pytest.raises(RuntimeError, match="reset"):
        feed(adapter, snapshot(trajectories={"trajectory.cont-1.json": {"steps": []}}))


@pytest.mark.parametrize(
    "name,kind",
    [
        ("ContextLengthExceededError", ErrorType.CONTEXT_LIMIT),
        ("ContextWindowExceededError", ErrorType.CONTEXT_LIMIT),
        ("OutputLengthExceededError", ErrorType.TOKEN_LIMIT),
        ("StepLimit", ErrorType.STEP_LIMIT),
        ("AuthenticationError", ErrorType.AUTHENTICATION),
        ("PermissionDeniedError", ErrorType.AUTHENTICATION),
        ("APIConnectionError", ErrorType.PROVIDER_TRANSIENT),
        ("Timeout", ErrorType.PROVIDER_TRANSIENT),
        ("RateLimitError", ErrorType.PROVIDER_TRANSIENT),
        ("InternalServerError", ErrorType.PROVIDER_TRANSIENT),
        ("BadRequestError", ErrorType.CONFIGURATION),
        ("ValidationError", ErrorType.CONFIGURATION),
        ("TerminalError", ErrorType.SANDBOX),
        ("OSError", ErrorType.SANDBOX),
        ("UnrecognizedError", ErrorType.UNKNOWN),
    ],
)
def test_typed_errors_not_text_guesses(adapter, name, kind):
    feed(
        adapter,
        snapshot(
            error={
                "classes": [name, "Exception"],
                "message": "A misleading context exceeded message with option-key",
            }
        ),
    )
    assert adapter._run_error.type == kind
    assert "option-key" not in str(adapter._run_error)


def test_error_subclass_uses_mro(adapter):
    feed(
        adapter,
        snapshot(
            error={
                "classes": ["CustomRateLimit", "RateLimitError", "Exception"],
                "message": "upstream",
            }
        ),
    )
    assert adapter._run_error.type == ErrorType.PROVIDER_TRANSIENT


def test_artifact_path_cannot_escape(adapter):
    with pytest.raises(ValueError, match="filename"):
        feed(adapter, snapshot(trajectories={"../escape.json": {"steps": []}}))


def test_install_uses_private_python_and_pinned_harbor(adapter, tmp_path):
    adapter._python = "/usr/bin/python3"
    adapter._runtime = LocalTransport()
    adapter._instance = Instance("local", "fake")
    adapter._remote = str(tmp_path)
    command = adapter._install_command(None)
    assert command == ["/usr/bin/python3", str(tmp_path / "terminus-install.py"), str(tmp_path)]
    assert "harbor==0.25.0" in (tmp_path / "terminus-install.py").read_text()
    assert adapter._agent_python == str(tmp_path / "venv/bin/python")
    assert adapter._dependency_env["UV_CACHE_DIR"] == str(tmp_path / "uv-cache")
    assert adapter._version_command()[0] == adapter._agent_python


def test_stop_reuses_cli_transport():
    from envweaver.agents.adapters._cli import CliAgent

    assert Terminus2Agent.stop is CliAgent.stop
    assert Terminus2Agent._execute is CliAgent._execute
    assert not Path("src/envweaver/agents/_terminus_stop.py").exists()


@pytest.fixture
def local_environment(monkeypatch):
    import sys
    from types import SimpleNamespace

    from envweaver.agents.adapters.terminus.runner import Environment

    # The bridge's contract needs only this result object, not Harbor on the host.
    monkeypatch.setitem(
        sys.modules, "harbor.environments.base", SimpleNamespace(ExecResult=SimpleNamespace)
    )
    return Environment()


@pytest.mark.parametrize("codes,installed", [([0], False), ([1, 1], False), ([1, 0, 0], True)])
def test_prepare_terminal_uses_setup_deadline(codes, installed, capsys):
    import asyncio
    from types import SimpleNamespace

    from envweaver.agents.adapters.terminus.runner import prepare_terminal

    calls = []
    results = iter(codes)

    async def execute(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(return_code=next(results), stdout="APT output\n", stderr="")

    asyncio.run(prepare_terminal(SimpleNamespace(exec=execute)))
    assert len(calls) == len(codes)
    assert all(options == {"user": "root"} for _, options in calls)
    if installed:
        assert "apt-get install -y tmux" in calls[-1][0]
        assert "APT output" in capsys.readouterr().out


def test_prepare_terminal_reports_install_failure(capsys):
    import asyncio
    from types import SimpleNamespace

    from envweaver.agents.adapters.terminus.runner import TerminalError, prepare_terminal

    results = iter([1, 0, 100])

    async def execute(command, **kwargs):
        return SimpleNamespace(return_code=next(results), stdout="", stderr="mirror unavailable\n")

    with pytest.raises(TerminalError, match="exit 100"):
        asyncio.run(prepare_terminal(SimpleNamespace(exec=execute)))
    assert "mirror unavailable" in capsys.readouterr().err


def test_environment_exec_preserves_cwd_env_unicode_and_exit(local_environment, tmp_path):
    import asyncio
    import os

    before = os.environ.get("TERMINUS_BRIDGE_TEST")
    result = asyncio.run(
        local_environment.exec(
            'printf "%s" "$TERMINUS_BRIDGE_TEST"; pwd >&2; exit 7',
            cwd=str(tmp_path),
            env={"TERMINUS_BRIDGE_TEST": "中文 value"},
        )
    )
    assert result.return_code == 7
    assert result.stdout == "中文 value"
    assert result.stderr.strip() == str(tmp_path)
    assert os.environ.get("TERMINUS_BRIDGE_TEST") == before


@pytest.mark.parametrize("cancel", [False, True])
def test_environment_timeout_or_cancel_reaps_only_its_command(local_environment, tmp_path, cancel):
    import asyncio
    import os
    import shlex
    import sys

    from envweaver.agents.adapters.terminus.runner import TerminalError

    pidfile = tmp_path / "pid"
    source = f"import os,time;from pathlib import Path;Path({str(pidfile)!r}).write_text(str(os.getpid()));time.sleep(60)"
    command = "exec " + shlex.join([sys.executable, "-c", source])

    async def run():
        if not cancel:
            with pytest.raises(TerminalError, match="timed out"):
                await local_environment.exec(command, timeout_sec=0.5)
            return
        task = asyncio.create_task(local_environment.exec(command))
        for _ in range(100):
            if pidfile.exists():
                break
            await asyncio.sleep(0.01)
        assert pidfile.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.parametrize(
    "message",
    [
        "can't find session: gone",
        "no server running on /tmp/tmux/default",
        "error connecting to /tmp/tmux/default (No such file or directory)",
    ],
)
def test_missing_terminal_is_already_stopped(message):
    import asyncio
    from types import SimpleNamespace

    from envweaver.agents.adapters.terminus import runner

    async def execute(command, **kwargs):
        assert "list-panes" in command
        return SimpleNamespace(return_code=1, stderr=message)

    asyncio.run(runner.stop_terminal(SimpleNamespace(exec=execute), "gone"))


def test_inspection_failure_is_not_mistaken_for_successful_stop():
    import asyncio
    from types import SimpleNamespace

    from envweaver.agents.adapters.terminus import runner

    async def execute(command, **kwargs):
        return SimpleNamespace(return_code=1, stderr="Permission denied")

    with pytest.raises(runner.TerminalError, match="Cannot inspect"):
        asyncio.run(runner.stop_terminal(SimpleNamespace(exec=execute), "alive"))


def test_terminal_directory_is_private_reused_and_scoped(local_environment, tmp_path, monkeypatch):
    import asyncio
    import os
    import stat

    from envweaver.agents.adapters.terminus.runner import Environment

    monkeypatch.setenv("TMUX_TMPDIR", "/original")

    async def run():
        await local_environment.configure_terminal(tmp_path, create=True)
        directory = local_environment.terminal_dir
        try:
            assert directory.stat().st_uid == os.geteuid()
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
            resumed = Environment()
            await resumed.configure_terminal(tmp_path, create=False)
            assert resumed.terminal_dir == directory
            result = await resumed.exec('printf %s "$TMUX_TMPDIR"', env={"TMUX_TMPDIR": "/override"})
            assert result.return_code == 0 and result.stdout == str(directory)
            assert os.environ["TMUX_TMPDIR"] == "/original"
        finally:
            shutil.rmtree(directory)

    asyncio.run(run())


def test_terminal_directory_creation_failure_is_reported(local_environment, tmp_path, monkeypatch):
    import asyncio

    from envweaver.agents.adapters.terminus.runner import TerminalError

    async def execute(command, **kwargs):
        return SimpleNamespace(return_code=1, stderr="No space left on device")

    monkeypatch.setattr(local_environment, "exec", execute)
    with pytest.raises(TerminalError, match="No space left"):
        asyncio.run(local_environment.configure_terminal(tmp_path, create=True))
    assert local_environment.terminal_dir is None
    assert not (tmp_path / "terminal.json").exists()


def test_failed_stop_is_reported_before_waiting_for_process_exit(adapter):
    from envweaver.errors import AgentError

    with pytest.raises(AgentError, match="could not inspect") as caught:
        feed(adapter, snapshot(stop_failed="could not inspect"))
    assert caught.value.type == ErrorType.SANDBOX
