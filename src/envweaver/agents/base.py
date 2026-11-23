"""Configuration and public adapter contract for custom Agents."""

import json
from abc import ABC, abstractmethod
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..models import Instance
from ..runtime import Runtime
from .output import AgentOutput


class Agent(ABC):
    """供 Eval 调用的 Agent 适配接口；run 必须实现，其余方法按需实现。配置通过 self.config 读取。

    Args:
        **config: Agent 配置，支持 JSON 类型及 Path。name 默认为类名，model、version 默认为 None。

    Raises:
        TypeError: 配置无法保存为 JSON。
        ValueError: 名称或版本字段无效，配置包含非有限数字或循环引用。
    """

    def __init__(self, **config: Any):
        def encode(value):
            if isinstance(value, Path):
                return str(value)
            raise TypeError(
                "Agent configuration must be JSON serializable or Path; "
                "create runtime objects in prepare()"
            )

        json.dumps(config, sort_keys=True, allow_nan=False, default=encode)
        name, model, version = (config.get(key) for key in ("name", "model", "version"))
        name = type(self).__name__ if name is None else name
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a nonempty string")
        if any(value is not None and not isinstance(value, str) for value in (model, version)):
            raise ValueError("model and version must be strings or None")
        self._config = deepcopy(config)
        self.metadata = {"agent": name, "agent_version": version, "model": model}

    @property
    def config(self) -> dict[str, Any]:
        """
        Returns:
            构造配置的独立字典副本。
        """
        return deepcopy(self._config)

    def new(self) -> "Agent":
        """
        Returns:
            配置相同、尚未准备的独立 Agent 对象。Eval 为每道题和每次重试自动调用；原对象可用于多个批次。
        """
        return type(self)(**self.config)

    def _identity(self):
        """Describe the adapter and its complete constructor configuration."""
        return {
            "class": f"{type(self).__module__}.{type(self).__qualname__}",
            "config": self.config,
        }

    def preflight(self, goal: list[dict]) -> None:
        """检查 Agent 特有的输入要求；不需要额外检查时可省略。

        Args:
            goal: 题目的文字和图片列表。

        Returns:
            None。

        Raises:
            ValueError: Agent 不支持该输入或配置无效。
        """

    def prepare(
        self, *, runtime: Runtime, instance: Instance, output: AgentOutput,
        cwd: str,
    ) -> None:
        """
        Args:
            runtime: Eval 传入的 Runtime。用它启动的辅助实例由本次尝试回收，也可用 close 提前关闭。
            instance: 已完成 context 上传和 task.setup 的做题实例。
            output: 本次产物写入器，output.directory 是当前机器上的产物目录。
            cwd: 沙箱内已存在的工作目录绝对路径。

        Returns:
            None。

        Raises:
            OSError: 准备文件无法读写。
            envweaver.errors.AgentError: Agent 准备失败，type 标明问题类型。
        """

    @abstractmethod
    def run(
        self, *, goal: list[dict], runtime: Runtime, instance: Instance,
        output: AgentOutput, cwd: str,
    ) -> dict:
        """
        Args:
            goal: 题目的文字和图片列表；图片 path 已转换为当前机器上的路径。
            runtime: 与 prepare 相同的 Runtime。
            instance: 做题实例。
            output: 与 prepare 相同的产物写入器。
            cwd: 沙箱内已存在的工作目录绝对路径。

        Returns:
            包含 output、artifacts、metadata 的 JSON 对象；可通过 output.finish(回复列表) 保存并返回。

        Raises:
            envweaver.errors.AgentError: Agent 执行失败或达到限制，type 标明问题类型。
            OSError: 文件无法读写。
            InterruptedError: 执行被 stop 中断。
        """

    def stop(self, timeout: float = 30) -> None:
        """
        Args:
            timeout: 停止本次 Agent 的最长等待秒数。可与 prepare 或 run 并发调用；保留任务沙箱和题目服务。

        Returns:
            None。

        Raises:
            TimeoutError: 未能在指定时间内停止。
            envweaver.errors.AgentError: 停止失败，type 标明问题类型。
        """
