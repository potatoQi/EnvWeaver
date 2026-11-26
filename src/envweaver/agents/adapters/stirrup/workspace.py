"""Stirrup workspace inside the existing Linux task sandbox."""

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field
from stirrup import ImageContentBlock
from stirrup.tools.code_backends.base import CodeExecToolProvider, CommandResult
from task_command import execute


class ShellCommand(BaseModel):
    cmd: str = Field(
        description="Bash command in the task sandbox. Relative paths start at the task working directory."
    )


class TaskWorkspace(CodeExecToolProvider):
    async def __aenter__(self):
        tool = self.get_code_exec_tool()
        tool.parameters = ShellCommand
        return tool

    async def __aexit__(self, *exc):
        pass

    async def run_command(self, cmd, *, timeout=None):
        result = await asyncio.to_thread(
            execute,
            ["bash", "-c", cmd],
            timeout=self._shell_timeout if timeout is None else timeout,
        )
        if result.get("error") == "timeout":
            return CommandResult(
                exit_code=124, stdout="", stderr=result["message"], error_kind="timeout"
            )
        return CommandResult(**result)

    async def read_file_bytes(self, path):
        return Path(path).read_bytes()

    async def write_file_bytes(self, path, content):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    async def file_exists(self, path):
        return Path(path).is_file()

    async def is_directory(self, path):
        return Path(path).is_dir()

    async def list_files(self, path):
        return sorted(
            str(item.relative_to(path)) for item in Path(path).rglob("*") if item.is_file()
        )

    async def view_image(self, path):
        return ImageContentBlock(data=await self.read_file_bytes(path))
