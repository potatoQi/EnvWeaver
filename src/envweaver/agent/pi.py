import json
import os
import subprocess
import tempfile
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .._util import run_process, write_json
from ..errors import AgentError
from ..models import AgentResult
from ..tools import Tool, ipython, shell
from ._bridge import ToolBridge


@contextmanager
def _toolset(selected, instance):
    """Bind target tools and close only the sessions we create."""
    if selected is None:
        selected = ["shell"] if instance is not None else ["read", "bash", "edit", "write"]
    if not isinstance(selected, (list, tuple)):
        raise TypeError("tools must be a list of tool names or Tool objects")
    native, bound = [], []
    factories = {"shell": shell, "ipython": ipython}
    with ExitStack() as cleanup:
        for tool in selected:
            if isinstance(tool, Tool):
                bound.append(tool)
            elif not isinstance(tool, str):
                raise TypeError("tools must contain names or Tool objects")
            elif instance is None:
                native.append(tool)
            else:
                if tool not in factories:
                    raise ValueError(
                        f"Unknown target tool: {tool}; use shell, ipython, or a Tool object"
                    )
                created = factories[tool](instance)
                bound.append(created)
                if created.close:
                    cleanup.callback(created.close)
        yield native, bound


def _environment(config, directory):
    """Translate credentials without modifying a user's existing pi directory."""
    env = {**os.environ, **config.get("env", {})}
    agent_dir = Path(config.get("agent_dir", directory / "pi")).expanduser().resolve()
    env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    if "agent_dir" in config and (config.get("base_url") or config.get("api_key")):
        raise ValueError("Use either an existing agent_dir or base_url/api_key configuration")

    if config.get("base_url"):
        if not config.get("model_name"):
            raise ValueError("model_name is required with base_url")
        env["ENVWEAVER_PI_API_KEY"] = config.get("api_key", "local")
        model = {
            "id": config["model_name"],
            "input": config.get("input", ["text", "image"]),
            "reasoning": config.get("reasoning", False),
            "contextWindow": config.get("context_window", 32768),
            "maxTokens": config.get("max_tokens", 4096),
        }
        provider = {
            "baseUrl": config["base_url"],
            "api": "openai-completions",
            "apiKey": "${ENVWEAVER_PI_API_KEY}",
            "models": [model],
        }
        write_json(agent_dir / "models.json", {"providers": {"envweaver": provider}}, private=True)
    elif config.get("api_key"):
        if not config.get("provider"):
            raise ValueError("provider is required for api_key without base_url")
        credential = {"type": "api_key", "key": config["api_key"]}
        write_json(agent_dir / "auth.json", {config["provider"]: credential}, private=True)
    return env


def _command(config, run_dir, names, has_bound_tools):
    executable = config.get("command", ["pi"])
    if isinstance(executable, str):
        executable = [executable]
    command = [
        *executable,
        "--mode",
        "json",
        "--print",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-mcp",
        "--no-approve",
        "--session-dir",
        str(run_dir / "sessions"),
    ]
    if has_bound_tools:
        command += ["--extension", str(Path(__file__).with_name("pi_extension.ts"))]
    command += ["--tools", ",".join(names)] if names else ["--no-tools"]
    options = {
        "--provider": "envweaver" if config.get("base_url") else config.get("provider"),
        "--model": config.get("model_name"),
        "--session": config.get("session"),
        "--system-prompt": config.get("system_prompt"),
        "--thinking": config.get("thinking"),
    }
    for flag, value in options.items():
        if value is not None:
            command += [flag, str(value)]
    return command + config.get("args", [])


def _read_result(run_dir, workspace, previous_session):
    trajectory = run_dir / "events.jsonl"
    last = None
    # Read JSONL directly from disk; trajectories may contain many large screenshots.
    with trajectory.open("rb") as records:
        for line in records:
            try:
                event = json.loads(line)
                message = event.get("message", {})
            except (ValueError, AttributeError) as error:
                raise AgentError(f"Invalid pi JSON stream; see {trajectory}") from error
            if event.get("type") == "message_end" and message.get("role") == "assistant":
                last = message
            if event.get("type") == "agent_settled" and event.get("aborted"):
                raise AgentError(f"pi run was aborted; see {trajectory}")
    if last is None or last.get("stopReason") in ("error", "aborted", "toolUse"):
        raise AgentError(f"pi did not complete successfully; see {trajectory}")
    text = "".join(
        block.get("text", "") for block in last.get("content", []) if block.get("type") == "text"
    )
    sessions = sorted((run_dir / "sessions").glob("**/*.jsonl"))
    return AgentResult(
        text=text,
        workspace=str(workspace),
        trajectory=str(trajectory),
        session=str(sessions[-1]) if sessions else previous_session,
    )


class PiBackend:
    def run(self, instruction, workspace, tools=None, instance=None, config=None):
        config = dict(config or {})
        workspace = Path(workspace).expanduser().resolve()
        run_dir = workspace / ".envweaver" / uuid.uuid4().hex
        run_dir.mkdir(parents=True)
        with _toolset(tools, instance) as (native, bound), ExitStack() as resources:
            directory = resources.enter_context(tempfile.TemporaryDirectory(prefix="envweaver-pi-"))
            env = _environment(config, Path(directory))
            if bound:
                bridge = resources.enter_context(ToolBridge(bound))
                env.update(ENVWEAVER_TOOL_URL=bridge.url, ENVWEAVER_TOOL_TOKEN=bridge.token)
            names = native + [tool.name for tool in bound]
            command = _command(config, run_dir, names, bool(bound))
            # stdin avoids CLI flag/@file interpretation of user instructions.
            try:
                with (
                    tempfile.TemporaryFile() as prompt,
                    (run_dir / "events.jsonl").open("wb") as output,
                    (run_dir / "stderr.log").open("wb") as errors,
                ):
                    prompt.write(instruction.encode())
                    prompt.seek(0)
                    process = run_process(
                        command,
                        stdin=prompt,
                        stdout=output,
                        stderr=errors,
                        cwd=workspace,
                        env=env,
                        timeout=config.get("timeout"),
                    )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise AgentError(
                    f"Could not complete pi run: {error}; artifacts: {run_dir}"
                ) from error
            if process.returncode:
                raise AgentError(
                    f"pi exited with {process.returncode}; see {run_dir / 'stderr.log'}"
                )
            return _read_result(run_dir, workspace, config.get("session"))
