from pathlib import Path
from typing import Protocol

from ..models import Instance, RuntimeRef


class RuntimeBackend(Protocol):
    """Runtime 后端需要提供的方法，无需继承。

    - 构造函数须接受 `root`、`host`、`ssh` 参数；未配置 SSH 时传入 None。
      返回的 `Instance` 填入对应的 `root` 和 `host`，后续操作使用 `Instance.host`。
      `host` 的地址格式和远程连接方式由后端决定。
    - `Instance.backend` 和 `RuntimeRef.backend` 填 `register` 注册的名称。
      `RuntimeRef` 保存镜像、启动配置和可随环境保存的连接设置。
      Runtime 在调用 start 前合并 ref.connection 与本次 connection。
      后端的 save 将实际使用的用户名、Python 路径等写入 RuntimeRef.connection，
      不保存密码、私钥路径或服务器地址。
    - `exec` 的字符串通过 Shell 执行，列表按程序和参数直接执行。
      Verifier 使用 `Instance.data["python"]` 指定的解释器，省略时为 `python3`。
    - `root` 保存环境记录，支持本地目录或 SSH 目录地址；镜像和磁盘由后端存储。
    - 操作失败抛异常；`exec` 返回退出码、stdout、stderr，超时抛 `TimeoutError`。
    """

    def __init__(
        self, root: str | Path | None = None, *, host: str | None = None, ssh: dict | None = None
    ): ...

    def create(
        self, image: str, config: dict | None = None, connection: dict | None = None
    ) -> RuntimeRef:
        """检查目标服务器上的镜像，返回 RuntimeRef，不启动环境。"""
        ...

    def start(self, ref: RuntimeRef, connection: dict | None = None) -> Instance:
        """启动独立实例，返回 Instance；不修改基础镜像。

        connection 接收 Runtime 合并后的配置；None 使用 ref.connection。
        """
        ...

    def connect(self, record: dict) -> Instance:
        """读取 describe 生成的记录，连接已有实例。字段与 describe 的输出一致。"""
        ...

    def describe(self, instance: Instance) -> dict:
        """返回 connect 所需的 JSON 字段；version、backend、host 由 Runtime 填写。"""
        ...

    def screenshot(self, instance: Instance) -> bytes:
        """返回 PNG 图片字节；不支持截图时抛 NotImplementedError。"""
        ...

    def exec(
        self,
        instance: Instance,
        command: str | list[str],
        cwd: str | None = None,
        timeout: float | None = 300,
    ) -> tuple[int, str, str]:
        """返回退出码、stdout、stderr；超时抛 TimeoutError，timeout=None 表示不限时。"""
        ...

    def upload(self, instance: Instance, source: str | Path, target: str) -> None:
        """source 是本地路径，target 是实例内路径；目录合并、同名覆盖。

        Runtime 会先将 SSH 来源下载到本地，后端无需处理 SSH 文件地址。
        """
        ...

    def download(self, instance: Instance, source: str, target: str | Path) -> None:
        """source 是实例内路径，target 是本地路径；目录合并、同名覆盖。

        Runtime 会将下载结果传到 SSH 目的地，后端无需处理 SSH 文件地址。
        """
        ...

    def save(self, instance: Instance) -> RuntimeRef:
        """保存环境，返回 RuntimeRef，保留启动引用的 metadata。"""
        ...

    def close(self, instance: Instance) -> None:
        """关闭并删除实例，保留已保存环境。"""
        ...
