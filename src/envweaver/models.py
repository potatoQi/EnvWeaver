from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .verifier import Verifier


@dataclass(frozen=True)
class RuntimeRef:
    """Runtime.create 或 Runtime.save 返回的镜像和配置，可传给 Runtime.start。

    Attributes:
        backend: 后端名称，例如 `docker`、`qemu`。
        image: Docker 镜像名或 QEMU 磁盘文件路径。
        config: 后端配置，见[后端配置](index.md#backend-config)。
        connection: SSH 用户名、Python 路径或 VNC 密码环境变量名，见[连接配置](index.md#connection-config)。
    """

    backend: str
    image: str
    config: dict[str, Any] = field(default_factory=dict)
    connection: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。

        Returns:
            包含 backend、image、config、connection 的字典。
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeRef":
        """从字典创建 RuntimeRef。

        Args:
            data: 包含 backend、image 和可选 config、connection 的字典。

        Returns:
            RuntimeRef 对象。

        Raises:
            TypeError: 缺少必要字段或包含未知字段。
        """
        return cls(**data)


@dataclass(frozen=True)
class Instance:
    """`Runtime.start` 或 `Runtime.connect` 返回的对象，代表已启动的容器或虚拟机。

    Attributes:
        id: 容器或虚拟机的 ID。
        backend: 管理实例的后端名称。
        root: 环境记录的存储目录。
        host: 实例所在的服务器地址；None 表示本地。
        data: 自动记录的实例信息，如镜像配置、磁盘路径和连接地址，无需手动填写。
    """

    id: str
    backend: str
    root: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    host: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。

        Returns:
            包含 id、backend、root、data、host 的字典。
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Instance":
        """从字典创建 Instance。

        Args:
            data: to_dict 返回的字典。

        Returns:
            Instance 对象。

        Raises:
            TypeError: 缺少必要字段或包含未知字段。
        """
        return cls(**data)


@dataclass(frozen=True)
class Task:
    """TaskPool.load 返回的任务。

    Attributes:
        name: 任务名称。
        task_ref: task.json 或 ZIP 的本地路径或 SSH 地址。
        goal: 有序文字/图片列表；文字使用 text 字段，图片使用 path 字段。
        metadata: 自定义任务信息，如来源、标签、软件名称。
        task_runtime_ref: 任务环境的镜像引用。
        context: 任务输入文件列表；source 是源文件，target 是环境内的目标路径。
        setup: 复制 context 后执行的准备命令和超时；可传给 Runtime.start。
        verifier_runtime_ref: 验证环境的镜像引用。
        verifier: 任务验证器。
    """

    name: str
    task_ref: str
    goal: list[dict[str, Any]]
    metadata: dict[str, Any]
    task_runtime_ref: RuntimeRef
    context: list[dict[str, str]]
    setup: dict[str, Any] | None = field(default=None, kw_only=True)
    verifier_runtime_ref: RuntimeRef
    verifier: "Verifier"
