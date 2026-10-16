import uuid
from dataclasses import dataclass
from typing import Callable

from . import runtime
from .transport import GuestClient


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    execute: Callable
    close: Callable | None = None

    def definition(self):
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def shell(instance):
    def execute(command, cwd=None, timeout=300):
        code, stdout, stderr = runtime.exec(instance, command, cwd, timeout)
        return {
            "content": [
                {"type": "text", "text": f"exit_code: {code}\nstdout:\n{stdout}\nstderr:\n{stderr}"}
            ],
            "isError": code != 0,
        }

    return Tool(
        "shell",
        "Execute a shell command in the target Runtime (not the Pipeline workspace).",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        execute,
    )


def ipython(instance, *, session=None, timeout=300):
    client = GuestClient(instance)
    session = session or uuid.uuid4().hex
    return Tool(
        "ipython",
        "Run Python/IPython in a persistent session inside the target Runtime. Use display(image) to show images; PyAutoGUI can operate its desktop.",
        {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
            "additionalProperties": False,
        },
        lambda code: client.ipython(code, session, timeout),
        lambda: client.close_session(session),
    )
