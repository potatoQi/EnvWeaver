from typing import Protocol

from ..models import Instance, RuntimeRef


class RuntimeBackend(Protocol):
    """Runtime 后端需要提供的方法，无需继承。

    方法语义与 Runtime 的公开接口一致；close 成功时直接返回，失败时抛异常，
    由 Runtime.close 转换成 (success, error)。
    """

    def start(self, ref: RuntimeRef) -> Instance: ...
    def exec(
        self, instance: Instance, command: str, cwd: str | None, timeout: float
    ) -> tuple[int, str, str]: ...
    def upload(self, instance: Instance, local_path: str, remote_path: str) -> None: ...
    def download(self, instance: Instance, remote_path: str, local_path: str) -> None: ...
    def save(self, instance: Instance, name: str) -> RuntimeRef: ...
    def close(self, instance: Instance) -> None: ...
