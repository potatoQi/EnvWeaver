"""Sandbox entrypoint for the official Harbor package, not an inference implementation."""

import asyncio
import json
import os
import pwd
import shlex
import shutil
import signal
import sys
import time
from pathlib import Path


class TerminalError(RuntimeError):
    pass


class StepLimit(Exception):
    pass


class Environment:
    """Only the local process/file operations consumed by Harbor's TmuxSession."""

    def __init__(self):
        self.terminal_dir = None

    async def configure_terminal(self, root, *, create):
        """Keep the terminal directory owned by the user who actually runs bash."""
        state = root / "terminal.json"
        if create:
            result = await self.exec("mktemp -d /tmp/envweaver-terminal-XXXXXX", timeout_sec=10)
            if result.return_code:
                raise TerminalError(f"Cannot create Terminus terminal directory: {result.stderr.strip()}")
            directory = result.stdout.strip()
            if not directory.startswith("/tmp/envweaver-terminal-") or "\n" in directory:
                raise TerminalError("Invalid Terminus terminal directory")
            self.terminal_dir = Path(directory)
            state.write_text(json.dumps({"directory": directory}))
        else:
            self.terminal_dir = Path(json.loads(state.read_text())["directory"])

    async def exec(self, command, *, user=None, timeout_sec=None, env=None, cwd=None):
        from harbor.environments.base import ExecResult

        process_env = {**os.environ, **(env or {})}
        if self.terminal_dir is not None:
            process_env["TMUX_TMPDIR"] = str(self.terminal_dir)
        args = ["bash", "-c", command]
        if user and pwd.getpwuid(os.geteuid()).pw_name != user:
            args = ["runuser", "-u", str(user), "--"] + args
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            env=process_env,
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout_sec)
        except TimeoutError as error:
            raise TerminalError("Terminal bridge command timed out") from error
        finally:
            if process.returncode is None:
                # These are bridge/install commands, not the Agent's tmux shell jobs.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
        return ExecResult(
            return_code=process.returncode,
            stdout=out.decode(errors="replace"),
            stderr=err.decode(errors="replace"),
        )

    async def is_dir(self, path):
        return Path(path).is_dir()


async def stop_terminal(environment, session, timeout=10):
    """Interrupt foreground work while retaining the terminal's background services."""
    result = await environment.exec(
        shlex.join(["tmux", "list-panes", "-s", "-t", session, "-F", "#{pane_pid}"]),
        timeout_sec=5,
    )
    if result.return_code:
        message = result.stderr.strip()
        if message.startswith(("can't find session:", "no server running on ")) or message.endswith(
            "(No such file or directory)"
        ):
            return
        raise TerminalError(f"Cannot inspect Terminus terminal: {message}")
    shells = [int(pid) for pid in result.stdout.split()]
    deadline = time.monotonic() + timeout
    seen = {}
    while time.monotonic() < deadline:
        groups = set()
        for pid in shells:
            try:
                fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
            except FileNotFoundError:
                continue
            own, foreground = int(fields[2]), int(fields[5])
            if foreground > 0 and foreground != own:
                groups.add(foreground)
        # Ctrl-C also discards pending terminal input, rather than executing queued commands.
        interrupted = await environment.exec(
            shlex.join(["tmux", "send-keys", "-t", session, "C-c"]), timeout_sec=5
        )
        if interrupted.return_code:
            raise TerminalError("Cannot interrupt Terminus terminal")
        if not groups:
            return
        for group in groups:
            age = time.monotonic() - seen.setdefault(group, time.monotonic())
            if age < 1:
                continue
            try:
                os.killpg(group, signal.SIGKILL if age > 2 else signal.SIGTERM)
            except ProcessLookupError:
                pass
        await asyncio.sleep(0.05)
    raise TerminalError("Terminus foreground commands did not stop")


async def prepare_terminal(environment):
    """Install missing APT tmux under Eval's setup deadline, before Harbor starts it."""
    if (await environment.exec("tmux -V", user="root")).return_code == 0:
        return
    if (await environment.exec("command -v apt-get", user="root")).return_code != 0:
        return
    result = await environment.exec(
        "DEBIAN_FRONTEND=noninteractive apt-get update && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y tmux",
        user="root",
    )
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", file=sys.stderr, flush=True)
    if result.return_code:
        raise TerminalError(f"tmux installation failed (exit {result.return_code}); see logs/prepare.log")


async def execute(action, root):
    from model_capture import install
    from monkey_patches import install as install_patches

    install(root / "model-calls.jsonl")
    install_patches()
    from harbor.agents.terminus_2.terminus_2 import Terminus2
    from harbor.agents.terminus_2.tmux_session import TmuxSession
    from harbor.models.agent.context import AgentContext

    config = json.loads((root / "config.json").read_text())
    context = AgentContext()
    state = {"completed": False, "error": None, "usage": {}, "trajectories": {}}
    session = root.name.replace("-", "_")
    environment = Environment()
    environment.session_id = session
    logs = root / "native"
    logs.mkdir(exist_ok=True)

    def snapshot():
        state["usage"] = context.model_dump(mode="json", exclude={"rollout_details"})
        state["trajectories"] = {p.name: json.loads(p.read_text()) for p in logs.glob("*.json")}
        pending = root / "state.tmp"
        pending.write_text(json.dumps(state, ensure_ascii=False, allow_nan=False))
        pending.replace(root / "state.json")

    class RecordedTerminus(Terminus2):
        async def _handle_llm_interaction(self, *args, **kwargs):
            result = await super()._handle_llm_interaction(*args, **kwargs)
            self.confirmed = bool(
                result[1] and self._pending_completion and not (result[2] and "ERROR:" in result[2])
            )
            return result

        def _dump_trajectory_with_continuation_index(self, index):
            super()._dump_trajectory_with_continuation_index(index)
            snapshot()

    try:
        await environment.configure_terminal(root, create=action == "prepare")
        agent = RecordedTerminus(
            logs_dir=logs,
            model_name=config["model"],
            extra_env=config["env"],
            session_id=session,
            record_terminal_session=False,
            **{**config["options"], "trajectory_config": {**config["options"].get("trajectory_config", {}),
                "raw_content": True, "linear_history": True}},
        )
        agent.confirmed = False
        terminal = TmuxSession(
            session_name=session,
            environment=environment,
            logging_path=environment.terminal_dir / "pane.log",
            local_asciinema_recording_path=None,
            remote_asciinema_recording_path=None,
            pane_width=agent.options.tmux_pane_width,
            pane_height=agent.options.tmux_pane_height,
        )
        agent._session = terminal
        if action == "prepare":
            try:
                await prepare_terminal(environment)
                await terminal.start()
                await terminal.send_keys(keys=["cd -- " + shlex.quote(os.getcwd()), "Enter"])
            except Exception as error:
                raise TerminalError(str(error)) from error
        else:
            await agent.run(
                instruction=(root / "goal.txt").read_text(),
                environment=environment,
                context=context,
            )
            if agent._n_episodes >= agent._max_episodes and not agent.confirmed:
                raise StepLimit("Terminus 2 reached max_turns")
            state["completed"] = True
    except BaseException as error:
        state["error"] = {
            "classes": [c.__name__ for c in type(error).__mro__],
            "message": str(error) or type(error).__name__,
        }
        if environment.terminal_dir is not None and shutil.which("tmux"):
            try:
                await stop_terminal(environment, session)
            except Exception as stopping:
                # A failed stop must not be reported as a clean process exit to CliAgent.
                state["stop_failed"] = str(stopping)
                snapshot()
                print(f"Cannot stop Terminus terminal: {stopping}", file=sys.stderr, flush=True)
                while True:
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        pass  # Keep the failure visible until the sandbox is closed.
    finally:
        snapshot()
    return 1 if state["error"] else 0


async def main(action, root):
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(execute(action, root))
    stopping = False

    def cancel():
        nonlocal stopping
        if not stopping:
            stopping = True
            task.cancel()

    loop.add_signal_handler(signal.SIGTERM, cancel)
    loop.add_signal_handler(signal.SIGINT, cancel)
    return await task


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1], Path(sys.argv[2]))))
