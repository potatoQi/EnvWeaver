"""Execute the optional ALE Claw distribution in its own interpreter."""

import asyncio
import json
import sys
from contextlib import redirect_stdout
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path


def prepare(config):
    from agent.decorators import find_agent_config
    from agent.loops.generic_vlm import GenericVlmConfig
    from ale_claw import RunResult

    if not {"error_type", "status_code"} <= RunResult.__dataclass_fields__.keys():
        raise ValueError(
            "AleClaw must support structured run results; install the documented revision"
        )
    backend = config.tool_loop_override or find_agent_config(config.model)
    if backend is None:
        raise ValueError(f"No CUA backend registered for {config.model}")
    if issubclass(backend.agent_class, GenericVlmConfig):
        from qwen_agent.llm.fncall_prompts.nous_fncall_prompt import NousFnCallPrompt  # noqa: F401
        from qwen_vl_utils import smart_resize  # noqa: F401
    return [version("ale-claw"), config.model]


async def execute(job, config):
    from ale_claw import run
    from cua_bench.computers.remote import RemoteDesktopSession
    from wire import capture

    with capture(Path("model.jsonl")):
        try:
            report = asdict(
                await run(
                    job["prompt"],
                    config=config,
                    sandbox_url=job["url"],
                    os_type=job["os"],
                    output_dir=Path.cwd(),
                )
            )
        except Exception as exc:
            report = {"status": "failed", "error": str(exc)}

    # A tool error can be returned to the model as an ordinary observation. Check
    # desktop health independently before accepting the harness's final outcome.
    session = RemoteDesktopSession(
        api_url=job["url"], os_type=job["os"], ephemeral=False, headless=True
    )
    try:
        healthy = await asyncio.wait_for(session.check_status(), timeout=5)
    except TimeoutError:
        healthy = False
    finally:
        await session.close()
    if not healthy:
        return {
            "status": "failed",
            "error_type": "sandbox",
            "error": "CUA desktop health check failed",
        }
    return report


if __name__ == "__main__":
    job = json.load(sys.stdin)
    with redirect_stdout(sys.stderr):
        from ale_claw import AleClawConfig

        if sys.argv[1:] == ["prepare"]:
            result = prepare(AleClawConfig(**job))
        else:
            result = asyncio.run(execute(job, AleClawConfig(**job["agent"])))
    print(json.dumps(result))
