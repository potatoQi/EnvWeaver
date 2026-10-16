import uuid
from dataclasses import dataclass
from typing import Callable

from . import runtime
from ._guest.client import GuestClient
from .models import Instance


@dataclass
class Tool:
    """供 Agent 适配器调用的工具，当前由 pi 支持。

    Attributes:
        name: 工具名。
        description: 给模型阅读的用途说明。
        parameters: 描述输入参数的 JSON Schema。
        execute: 接受工具参数的回调，返回 content 列表及可选 isError。
            content 支持 text/text，以及 image/data/mimeType（data 为 Base64）。
        close: 可选资源清理回调。自定义 Tool 由调用者清理；名称方式创建的工具由适配器清理。
    """

    name: str
    description: str
    parameters: dict
    execute: Callable
    close: Callable | None = None

    def definition(self) -> dict:
        """返回给模型的名称、描述和参数定义，不包含 Python 回调。"""
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def shell(instance: Instance) -> Tool:
    """创建绑定目标实例的命令工具。

    Args:
        instance: 要操作的目标实例。

    Returns:
        接受 command、cwd、timeout 的 Tool，输出退出码与文本；非零退出标记 isError。
    """

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


def ipython(instance: Instance, *, session: str | None = None, timeout: float = 300) -> Tool:
    """创建目标实例内的持久化 IPython 工具，可用于桌面操作与截图。

    Args:
        instance: 已运行目标端连接服务的实例；桌面操作还需可访问的图形会话。
        session: 会话标识；省略时自动生成。相同标识会连接同一个 kernel。
        timeout: 每次 cell 的最长执行秒数，目标服务要求不超过 86400。

    Returns:
        接受 code 字符串的 Tool；display(image) 以图片内容返回，不仅是路径。

    cell 超时后重启 kernel，原有变量丢失。工具本身允许任意 Python，
    并不强制只能使用鼠标键盘。自行创建时使用完应调用 tool.close()。
    """
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
