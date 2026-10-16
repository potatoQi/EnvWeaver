from pathlib import Path
from typing import Protocol

from ..models import AgentResult, Instance
from ..tools import Tool


class AgentBackend(Protocol):
    """自定义 Agent 适配器的接口，无需继承此 Protocol。"""

    def run(
        self, instruction, workspace, tools=None, instance=None, config=None
    ) -> AgentResult: ...


_backends = {}


def register(name: str, backend: AgentBackend) -> None:
    """在当前进程注册或替换 Agent 后端。

    Args:
        name: agent.run 使用的 backend 名称。
        backend: 实现 run 方法并返回 AgentResult 的对象。
    """
    _backends[name] = backend


def run(
    backend: str,
    instruction: str,
    workspace: str | Path,
    tools: list[str | Tool] | tuple[str | Tool, ...] | None = None,
    instance: Instance | None = None,
    config: dict | None = None,
) -> AgentResult:
    """在 Pipeline 的执行环境中调用 Agent，并可把工具绑定到目标实例。

    Args:
        backend: 后端名；目前内置 pi，也可通过 register 注册其他实现。
        instruction: 本次任务指令。任务分解和生成策略由调用方决定。
        workspace: Pipeline 侧工作目录，pi 的轨迹与会话存放在其 .envweaver 子目录。
        tools: 工具名称或 Tool 对象。pi 传入 instance 时默认 shell，可显式选 ipython；
            不传 instance 时默认 pi 自带的 read/bash/edit/write，操作 Pipeline 侧文件。
        instance: 可选目标实例。传入后 shell/IPython 在该实例内执行，pi 仍在 Pipeline 侧。
        config: 后端配置。pi 支持 base_url、model_name、api_key，以及 command、timeout、
            session 等；也可用 agent_dir 指向已有 pi 配置，不与 base_url/api_key 混用。

    Returns:
        AgentResult，包含回答、轨迹和可选会话标识；不是任务分数。

    Raises:
        ValueError: 后端或 pi 配置、工具选择不受支持。
        AgentError: pi 启动失败、超时、异常退出或未正常结束；诊断文件会保留。

    不自动重试或续接会话。pi 是单独安装的外部程序；config.timeout 默认不限时。
    """
    if backend not in _backends:
        if backend == "pi":
            from .pi import PiBackend

            _backends[backend] = PiBackend()
        else:
            raise ValueError(f"Unknown Agent backend: {backend}")
    return _backends[backend].run(instruction, workspace, tools, instance, config)
