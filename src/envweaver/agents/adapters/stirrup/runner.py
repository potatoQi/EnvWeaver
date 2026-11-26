"""Run inside the task sandbox, using the task directory as Stirrup's workspace."""

import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path

import httpx
import litellm
from pydantic import BaseModel, Field
from stirrup import (
    Agent,
    ContextOverflowError,
    ImageContentBlock,
    OutputTokenLimitError,
    UserMessage,
)
from stirrup.clients.utils import content_to_openai, to_openai_messages
from stirrup.core import exceptions
from stirrup.core.models import (
    AssistantMessage,
    SummaryMessage,
    Tool,
    ToolMessage,
    ToolResult,
    TurnWarningMessage,
    tool_call_blocks,
)
from stirrup.tools import WebToolProvider
from stirrup.tools.finish import SIMPLE_FINISH_TOOL
from stirrup.utils.logging import AgentLogger
from tenacity import RetryError

CALLS = Path(__file__).with_name("model-calls.jsonl")


class ToolCallIDError(ValueError):
    pass


class RunTrace(AgentLogger):
    def __init__(self, goal):
        super().__init__(show_spinner=False, level=logging.WARNING)
        self.goal = goal
        self.summary_trigger = None
        self.first_id = self.call_id = None
        self.positions = {}

    def trace_summary(self, summarize):
        async def wrapped(*args, **kwargs):
            self.summary_trigger = kwargs.get("trigger", "token_threshold")
            try:
                return await summarize(*args, **kwargs)
            finally:
                self.summary_trigger = None

        return wrapped

    def trace_generate(self, generate):
        async def wrapped(messages, tools):
            self.first_id = None
            self.messages = messages
            try:
                reply = await generate(messages, tools)
            except NotImplementedError as exc:
                if any(
                    not call.has_provider_tool_call_id
                    for message in messages
                    if isinstance(message, AssistantMessage)
                    for call in tool_call_blocks(message.blocks)
                ):
                    raise ToolCallIDError(
                        "The model gateway must return a nonempty ID for each tool call in conversation history"
                    ) from exc
                raise
            calls = tool_call_blocks(reply.blocks)
            positions = {call.tool_call_id: (self.call_id, i) for i, call in enumerate(calls)}
            if len(positions) != len(calls):
                raise ToolCallIDError(
                    "The model gateway returned duplicate tool call IDs in one response"
                )
            if not self.summary_trigger:
                self.positions.update(positions)
            return reply

        return wrapped

    def message_kind(self, index, message):
        if message is self.goal:
            return None
        if isinstance(message, SummaryMessage):
            return "summary_bridge"
        if isinstance(message, TurnWarningMessage):
            return "turn_warning"
        if isinstance(message, ToolMessage):
            return "tool_result"
        if self.summary_trigger and index == len(self.messages) - 1:
            return "summary_prompt"
        if index and isinstance(self.messages[index - 1], SummaryMessage):
            return "summary_ack"
        if isinstance(message.content, list) and any(
            isinstance(c, ImageContentBlock) for c in message.content
        ):
            return "tool_image"
        return "continue"

    def wire_message_kinds(self, messages):
        users = [(str(i), msg) for i, msg in enumerate(messages) if msg["role"] == "user"]
        try:
            projected = [
                (wire, self.message_kind(i, native))
                for i, native in enumerate(self.messages)
                if isinstance(native, (UserMessage, ToolMessage))
                for wire in to_openai_messages([native])
                if wire["role"] == "user"
            ]
        except (ValueError, NotImplementedError):
            projected = []
        # Whole-history transformations can differ from single-message rendering.
        # Only assign origins when the projected user messages match the actual request.
        if [m for _, m in users] != [m for m, _ in projected]:
            return {index: "unattributed" for index, _ in users}
        return {i: kind for (i, _), (_, kind) in zip(users, projected, strict=True) if kind}

    def request_metadata(self, call_id, request):
        self.call_id = call_id
        extra = {}
        if self.summary_trigger:
            extra.update(kind="summary", trigger=self.summary_trigger)
        if self.first_id:
            extra["retry_of"] = self.first_id
        else:
            self.first_id = call_id
        return {"message_kinds": self.wire_message_kinds(request["messages"]), "step_extra": extra}

    def tool_result(self, tool_message):
        call_id, index = self.positions.pop(tool_message.tool_call_id)
        message = {
            "role": "tool",
            "name": tool_message.name,
            "tool_call_id": tool_message.tool_call_id,
            "call_index": index,
            "content": content_to_openai(tool_message.content),
            "extra": {"is_error": not tool_message.success},
        }
        with CALLS.open("a", encoding="utf-8") as stream:
            print(
                json.dumps({"type": "tool_result", "call_id": call_id, "data": message}),
                file=stream,
            )


class AbandonParams(BaseModel):
    reason: str = Field(min_length=1, description="Why the task cannot be completed.")


def finish_tools(submission_dir, allow_abandon):
    finish = SIMPLE_FINISH_TOOL
    if submission_dir is not None:
        root = Path(submission_dir).resolve()

        async def submit(params):
            result = await SIMPLE_FINISH_TOOL.executor(params)
            if not result.success or not params.paths:
                return result
            sources = [Path(path).resolve() for path in dict.fromkeys(params.paths)]
            if any(not source.is_relative_to(root) for source in sources):
                return ToolResult(content=f"Submit files from {root}.", success=False)
            destination = CALLS.with_name("submitted")
            destination.mkdir(exist_ok=True)
            for i, source in enumerate(sources):
                shutil.copyfile(source, destination / str(i))
            return result

        finish = finish.model_copy(update={"executor": submit})
    tools = [finish]
    if allow_abandon:
        tools.append(
            Tool(
                name="abandon_task_finish",
                description="End the task without submitting files when it cannot be completed.",
                parameters=AbandonParams,
                executor=lambda params: ToolResult(content=params.reason),
            )
        )
    return tools


async def run():
    from model_capture import install
    from workspace import TaskWorkspace

    settings = json.loads(Path(sys.argv[1]).read_text())
    prompt = sys.stdin.read()
    images = [ImageContentBlock(data=Path(path).read_bytes()) for path in sys.argv[2:]]
    goal = UserMessage(content=[prompt, *images] if images else prompt)
    trace = RunTrace(goal)
    # Install before Stirrup imports acompletion by name.
    install(CALLS, request_metadata=trace.request_metadata)
    from stirrup.clients.litellm_client import LiteLLMClient

    client = LiteLLMClient(**settings["client"])
    client.generate = trace.trace_generate(client.generate)
    workspace = TaskWorkspace(shell_timeout=settings["command_timeout"])
    tools = [workspace]
    if settings["view_image"]:
        tools.append(workspace.get_view_image_tool())
    if settings["web"]:
        tools.append(WebToolProvider())
    agent = Agent(
        client=client,
        name="stirrup",
        tools=tools,
        logger=trace,
        finish_tool=finish_tools(settings["submission_dir"], settings["allow_abandon"]),
        **settings["agent"],
    )
    async with agent.session(cache_on_interrupt=False) as session:
        if "full_system_prompt" in settings:
            # Stirrup 0.2.x assembles its system message here for every new run.
            session._build_system_prompt = lambda: settings["full_system_prompt"]
        session.summarize_messages = trace.trace_summary(session.summarize_messages)
        finish, _, _ = await session.run([goal])
    if finish is None:
        turns = settings["agent"]["max_turns"]
        return {"error": {"type": "step_limit", "message": f"Stirrup reached {turns} turns"}}
    abandoned = isinstance(finish, AbandonParams)
    return {
        "answer": finish.reason,
        "paths": [] if abandoned else finish.paths,
        "abandoned": abandoned,
    }


def failure(exc):
    if isinstance(exc, RetryError):
        exc = exc.last_attempt.exception() or exc
    kind = "unknown"
    if isinstance(exc, ContextOverflowError) or isinstance(
        exc, getattr(exceptions, "CompactionRequiredError", ())
    ):
        kind = "context_limit"
    elif isinstance(exc, ToolCallIDError):
        kind = "configuration"
    elif isinstance(exc, OutputTokenLimitError):
        kind = "token_limit"
    elif isinstance(exc, (litellm.APIConnectionError, litellm.Timeout)) or (
        isinstance(exc, exceptions.IncompleteResponseError)
        and isinstance(
            exc.__cause__ or exc.__context__,
            (httpx.TransportError, litellm.APIConnectionError, TimeoutError, ConnectionError),
        )
    ):
        kind = "provider_transient"
    return {
        "type": kind,
        "message": str(exc),
        "status": getattr(exc, "status_code", None),
    }


if __name__ == "__main__":
    try:
        result = asyncio.run(run())
    except Exception as exc:
        result = {"error": failure(exc)}
    Path(__file__).with_name("outcome.json").write_text(json.dumps(result), encoding="utf-8")
