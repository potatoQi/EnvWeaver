"""Sandbox entry point. Claw-Eval owns the model and tool loop, EnvWeaver owns its lifecycle."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from claw_eval.config import MediaConfig, ModelConfig, PromptConfig
from claw_eval.models.task import TaskDefinition
from claw_eval.runner.loop import run_task
from claw_eval.runner.providers.openai_compat import OpenAICompatProvider
from claw_eval.runner.sandbox_runner import SandboxRunner
from openai import APIConnectionError, APIStatusError, AuthenticationError, PermissionDeniedError

ROOT = Path(__file__).resolve().parent
TOOLS_URL = Path("/tmp/envweaver-claw-tools.url")


def record(kind, **data):
    with (ROOT / "calls.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(dict(kind=kind, at=datetime.now(timezone.utc).isoformat(), **data)) + "\n"
        )


class Provider(OpenAICompatProvider):
    """Pair the actual request body with the response interpreted by the upstream harness."""

    def chat(self, messages, tools=None):
        def capture(request):
            record("request", body=json.loads(request.content))

        hooks = self.client._client.event_hooks["request"]
        hooks.append(capture)
        try:
            message, usage = super().chat(messages, tools=tools)
            record("response", message=message.model_dump(), usage=usage.model_dump())
            return message, usage
        finally:
            hooks.remove(capture)


def prepare(url):
    TOOLS_URL.write_text(url)
    task = TaskDefinition.from_yaml("task.yaml")
    if task.user_agent.enabled:
        raise ValueError("ClawEvalAgent does not support tasks requiring a simulated user")
    expected = len(task.sandbox_files or task.environment.fixtures)
    count = SandboxRunner.inject_files(
        SimpleNamespace(sandbox_url=url), task, task_dir=str(Path.cwd())
    )
    if count != expected:
        raise ValueError(f"Only {count} of {expected} Claw tool files were injected")


def execute(inputs):
    model = ModelConfig.model_validate(inputs["model"])
    task = TaskDefinition.from_yaml("task.yaml")
    if Path(task.task_id).name != task.task_id or task.task_id in ("", ".", ".."):
        raise ValueError("task_id must be a file name")
    task.prompt.text = inputs["prompt"]
    task.prompt.attachments.extend(inputs["images"])
    provider = Provider(
        model_id=model.model_id,
        api_key=model.api_key,
        base_url=model.base_url,
        temperature=model.temperature,
        extra_body=model.extra_body,
        reasoning_effort=model.reasoning_effort,
        reasoning_field=model.reasoning_field,
    )
    trace = run_task(
        task,
        provider,
        ROOT / "traces",
        sandbox_tools=True,
        sandbox_url=TOOLS_URL.read_text().strip(),
        prompt_cfg=PromptConfig(),
        model_cfg=model,
        media_cfg=MediaConfig(),
    )
    # Upstream returns normally on time/turn exhaustion, after a user/tool message.
    with trace.open() as stream:
        last_role = None
        for line in stream:
            event = json.loads(line)
            if event["type"] == "message":
                last_role = event["message"]["role"]
            elif event["type"] == "trace_end" and last_role != "assistant":
                return (
                    "step_limit"
                    if event["total_turns"] >= task.environment.max_turns
                    else "timeout"
                ), "Claw-Eval reached its task limit"
    return "completed", ""


def _error_reason(exc):
    # Claw wraps provider errors for multimodal inputs, preserving the original cause.
    if isinstance(exc, RuntimeError) and isinstance(
        exc.__cause__, (APIStatusError, APIConnectionError)
    ):
        exc = exc.__cause__
    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return "authentication"
    if isinstance(exc, APIConnectionError):
        return "provider_transient"
    if isinstance(exc, APIStatusError):
        if exc.status_code == 429 or exc.status_code >= 500:
            return "provider_transient"
        if exc.code == "context_length_exceeded":
            return "context_limit"
        if exc.status_code == 400:
            message = exc.body.get("message") if isinstance(exc.body, dict) else exc.body
            if isinstance(message, str) and message.lower().startswith(
                (
                    "this model's maximum context length is ",
                    "context length exceeded",
                    "prompt is too long",
                )
            ):
                return "context_limit"
        return "configuration"
    if isinstance(exc, (ValueError, FileNotFoundError)):
        return "configuration"
    return "unknown"


def main():
    try:
        reason, detail = execute(json.loads(Path(sys.argv[1]).read_text()))
    except Exception as exc:
        reason = _error_reason(exc)
        detail = f"{type(exc).__name__}: {exc}"
    path = ROOT / "status.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(dict(reason=reason, detail=detail)))
    temporary.replace(path)
    return 0 if reason in {"completed", "timeout", "step_limit"} else 1


if __name__ == "__main__":
    if sys.argv[1] == "--prepare":
        prepare(sys.argv[2])
    else:
        sys.exit(main())
