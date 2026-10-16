from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class RuntimeRef:
    """可启动的基础环境或已保存环境，不是运行中的实例。

    Attributes:
        backend: 后端名称，例如 `docker`、`qemu`。
        image: Docker 镜像名或 QEMU 磁盘路径。
        config: 后端启动配置；保存后可能包含镜像归档等资产路径。
    """

    backend: str
    image: str
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """返回可写入 JSON 的描述；config 中的值需由调用者保证可序列化。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeRef":
        """从描述字典还原引用，不启动环境。"""
        return cls(**data)


@dataclass(frozen=True)
class Instance:
    """运行中环境的连接描述，由 `Runtime.start` 返回。

    Attributes:
        id: 后端分配的实例标识。
        backend: 管理实例的后端名称。
        root: Runtime 资产存储的绝对路径。
        data: 后端连接信息；调用者通常无需修改。
    """

    id: str
    backend: str
    root: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """返回连接描述；不保存运行状态或复制环境。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Instance":
        """还原连接描述，不检查目标是否仍在运行。"""
        return cls(**data)


@dataclass(frozen=True)
class Task:
    """TaskPool 读取的完整任务描述，材料路径已解析为绝对路径。

    Attributes:
        name: Pool 内唯一的任务名。
        task_ref: 任务描述文件路径。
        runtime: 可用于启动环境的引用。
        goal: 有序文字/图片列表；文字使用 text 字段，图片使用 path 字段。
        verifier: 验证包的 run.py 路径。
        context: 外部文件映射列表，每项包含 source 和 target。
        metadata: Pipeline 自定的 JSON 元数据。
    """

    name: str
    task_ref: str
    runtime: RuntimeRef
    goal: list[dict[str, Any]]
    verifier: str
    context: list[dict[str, str]]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class AgentResult:
    """Agent 的执行结果；任务是否正确应由 Verifier 独立判断。

    Attributes:
        text: Agent 最终回答。
        workspace: 本次使用的 Pipeline 侧工作目录。
        trajectory: 事件轨迹文件路径。
        session: 可交给后续调用继续会话的标识，未提供时为 None。
    """

    text: str
    workspace: str
    trajectory: str
    session: str | None = None
