import json
import os
import subprocess
import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path

from .. import tools as tool_factory
from .._util import run_process, write_json
from ..errors import AgentError
from ..models import AgentResult
from ..tools import Tool
from ._bridge import ToolBridge


class PiBackend:
    def run(self, instruction, workspace, tools=None, instance=None, config=None):
        config = dict(config or {})
        workspace = Path(workspace).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        run_dir = workspace / ".envweaver" / uuid.uuid4().hex
        run_dir.mkdir(parents=True)
        selected = (
            tools
            if tools is not None
            else (["shell"] if instance is not None else ["read", "bash", "edit", "write"])
        )
        if not isinstance(selected, (list, tuple)):
            raise TypeError("tools must be a list of tool names or Tool objects")
        native, bound = [], []
        with ExitStack() as stack:
            for tool in selected:
                if isinstance(tool, Tool):
                    bound.append(tool)
                elif instance is not None:
                    if tool not in ("shell", "ipython"):
                        raise ValueError(
                            f"Unknown target tool: {tool}; use shell, ipython, or a Tool object"
                        )
                    created = getattr(tool_factory, tool)(instance)
                    bound.append(created)
                    if created.close:
                        stack.callback(created.close)
                else:
                    native.append(tool)
            env = {**os.environ, **config.get("env", {})}
            temporary = Path(
                stack.enter_context(tempfile.TemporaryDirectory(prefix="envweaver-pi-"))
            )
            agent_dir = Path(config.get("agent_dir", temporary / "pi"))
            env["PI_CODING_AGENT_DIR"] = str(agent_dir)
            if config.get("base_url"):
                if "agent_dir" in config:
                    raise ValueError(
                        "Use either base_url configuration or a preconfigured agent_dir"
                    )
                if not config.get("model_name"):
                    raise ValueError("model_name is required with base_url")
                env["ENVWEAVER_PI_API_KEY"] = config.get("api_key", "local")
                write_json(
                    agent_dir / "models.json",
                    {
                        "providers": {
                            "envweaver": {
                                "baseUrl": config["base_url"],
                                "api": "openai-completions",
                                "apiKey": "${ENVWEAVER_PI_API_KEY}",
                                "models": [
                                    {
                                        "id": config["model_name"],
                                        "input": config.get("input", ["text", "image"]),
                                        "reasoning": config.get("reasoning", False),
                                        "contextWindow": config.get("context_window", 32768),
                                        "maxTokens": config.get("max_tokens", 4096),
                                    }
                                ],
                            }
                        }
                    },
                    private=True,
                )
            elif config.get("api_key"):
                if "agent_dir" in config:
                    raise ValueError(
                        "Use provider environment credentials with an existing agent_dir"
                    )
                provider = config.get("provider")
                if not provider:
                    raise ValueError("provider is required for api_key without base_url")
                write_json(
                    agent_dir / "auth.json",
                    {provider: {"type": "api_key", "key": config["api_key"]}},
                    private=True,
                )
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
            if bound:
                bridge = stack.enter_context(ToolBridge(bound))
                env["ENVWEAVER_TOOL_URL"] = bridge.url
                env["ENVWEAVER_TOOL_TOKEN"] = bridge.token
                command += ["--extension", str(Path(__file__).with_name("pi_extension.ts"))]
            names = native + [tool.name for tool in bound]
            command += ["--tools", ",".join(names)] if names else ["--no-tools"]
            if config.get("base_url"):
                command += ["--provider", "envweaver", "--model", config["model_name"]]
            else:
                for key, flag in [("model_name", "--model"), ("provider", "--provider")]:
                    if config.get(key):
                        command += [flag, config[key]]
            for key, flag in [
                ("session", "--session"),
                ("system_prompt", "--system-prompt"),
                ("thinking", "--thinking"),
            ]:
                if config.get(key) is not None:
                    command += [flag, str(config[key])]
            command += config.get("args", [])
            # stdin avoids CLI flag/@file interpretation of user instructions.
            trajectory = run_dir / "events.jsonl"
            diagnostics = run_dir / "stderr.log"
            try:
                import io

                with (
                    tempfile.TemporaryFile() as prompt,
                    trajectory.open("wb") as output,
                    diagnostics.open("wb") as errors,
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
            last = None
            for line in io.BytesIO(trajectory.read_bytes()):
                try:
                    event = json.loads(line)
                except ValueError as error:
                    raise AgentError(f"Invalid pi JSON stream; see {trajectory}") from error
                if (
                    event.get("type") == "message_end"
                    and event.get("message", {}).get("role") == "assistant"
                ):
                    last = event["message"]
                if event.get("type") == "agent_settled" and event.get("aborted"):
                    raise AgentError(f"pi run was aborted; see {trajectory}")
            if last is None or last.get("stopReason") in ("error", "aborted", "toolUse"):
                raise AgentError(f"pi did not complete successfully; see {trajectory}")
            text = "".join(
                block.get("text", "")
                for block in last.get("content", [])
                if block.get("type") == "text"
            )
            sessions = sorted((run_dir / "sessions").glob("**/*.jsonl"))
            return AgentResult(
                text,
                str(workspace),
                str(trajectory),
                str(sessions[-1]) if sessions else config.get("session"),
            )
