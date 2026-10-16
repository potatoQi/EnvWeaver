import os
from pathlib import Path

from .._util import read_json, write_json
from ..models import Instance, RuntimeRef
from .base import RuntimeBackend
from .docker import DockerBackend
from .qemu import QemuBackend

_BUILTIN_BACKENDS = {"docker": DockerBackend, "qemu": QemuBackend}


class Runtime:
    """管理运行中的环境及持久化资产，按 backend 分派操作。

    Args:
        root: 资产目录。省略时使用 ENVWEAVER_HOME，未设置则为 ~/.envweaver。
        backends: 可选的自定义后端实例映射，键为后端名。
    """

    def __init__(
        self, root: str | Path | None = None, *, backends: dict[str, RuntimeBackend] | None = None
    ):
        self.root = (
            Path(root or os.environ.get("ENVWEAVER_HOME", "~/.envweaver")).expanduser().resolve()
        )
        self._backends = dict(backends or {})

    def register(self, name: str, backend: RuntimeBackend) -> None:
        """在当前 Runtime 对象上注册或替换后端，不影响其他 Runtime 对象。

        Args:
            name: RuntimeRef.backend 使用的名称。
            backend: 实现 RuntimeBackend 方法的对象，无需继承。
        """
        self._backends[name] = backend

    def _backend(self, name):
        if name not in self._backends:
            if name not in _BUILTIN_BACKENDS:
                raise ValueError(f"Unknown runtime backend: {name}")
            self._backends[name] = _BUILTIN_BACKENDS[name](self.root)
        return self._backends[name]

    def start(
        self, ref: RuntimeRef | str | Path, context: list[dict[str, str]] | None = None
    ) -> Instance:
        """启动独立实例，并按顺序复制外 Context。

        Args:
            ref: 环境引用，或包含 RuntimeRef 描述的 JSON 文件路径。
            context: source（Pipeline 侧文件/目录）到 target（实例内路径）的映射列表。

        Returns:
            可供 exec、Agent、Verifier 等操作使用的 Instance。

        Raises:
            ValueError: 后端未知或连接配置不完整。

        Context 复制失败时尝试关闭新实例，并重新抛出原始异常。
        该方法不自动安装任务依赖，不恢复进程内存或原来的桌面窗口。
        """
        if isinstance(ref, (str, Path)):
            ref = RuntimeRef.from_dict(read_json(ref))
        backend = self._backend(ref.backend)
        instance = backend.start(ref)
        try:
            for item in context or []:
                backend.upload(instance, item["source"], item["target"])
        except BaseException as error:
            try:
                backend.close(instance)
            except Exception as cleanup_error:
                error.add_note(f"Instance cleanup also failed: {cleanup_error}")
            raise
        return instance

    def exec(
        self, instance: Instance, command: str, cwd: str | None = None, timeout: float = 300
    ) -> tuple[int, str, str]:
        """在目标实例执行命令，Linux 使用 sh，Windows 使用 PowerShell。

        Args:
            instance: 目标实例。
            command: 目标操作系统的命令文本，不自动跨系统翻译。
            cwd: 目标实例内的工作目录。
            timeout: 最长执行秒数。

        Returns:
            exit_code、stdout、stderr；命令非零退出不会自动抛异常。

        Raises:
            TimeoutError: 目标命令执行超时。
        """
        return self._backend(instance.backend).exec(instance, command, cwd, timeout)

    def upload(self, instance: Instance, local_path: str | Path, remote_path: str) -> None:
        """复制 Pipeline 侧文件或目录到实例内，不创建挂载。

        Args:
            instance: 目标实例。
            local_path: Pipeline 侧源文件或目录。
            remote_path: 实例内目标路径本身，不自动追加源名称。

        覆盖同名文件；目录合并，不删除目标目录内的其他文件。
        """
        return self._backend(instance.backend).upload(instance, str(local_path), remote_path)

    def download(self, instance: Instance, remote_path: str, local_path: str | Path) -> None:
        """复制实例内文件或目录到 Pipeline 侧。

        Args:
            instance: 源实例。
            remote_path: 实例内源文件或目录。
            local_path: Pipeline 侧目标路径本身。

        复制和覆盖语义与 upload 相同。
        """
        return self._backend(instance.backend).download(instance, remote_path, str(local_path))

    def save(self, instance: Instance, name: str) -> RuntimeRef:
        """保存持久化状态与启动配置，不保存进程内存。

        Args:
            instance: 要保存的运行中实例。
            name: 用于记录的名称；不承担 Task Pool 的唯一性检查。

        Returns:
            可再次 start 的环境引用，资产保存在当前 Runtime 的 root 下。

        Docker 同步文件后暂停提交，再导出镜像；不支持包含挂载的实例。
        QEMU 正常关机、复制磁盘后重新启动；原 Instance 继续可用，但内存状态丢失。
        """
        return self._backend(instance.backend).save(instance, name)

    def close(self, instance: Instance) -> tuple[bool, str | None]:
        """释放运行实例，不删除已保存的环境资产。

        Args:
            instance: 要释放的实例。

        Returns:
            success、error；成功为 (True, None)，失败包含错误文字。
        """
        try:
            self._backend(instance.backend).close(instance)
        except Exception as error:
            return False, str(error)
        return True, None

    @staticmethod
    def connect(path: str | Path) -> Instance:
        """从 JSON 描述文件读取 Instance，不启动环境，也不检查在线状态。

        Args:
            path: verifier.run 自动传入的 --instance 文件，或 describe 生成的文件。

        Returns:
            用于后续 Runtime 操作的连接描述。
        """
        return Instance.from_dict(read_json(path))

    @staticmethod
    def describe(instance: Instance, path: str | Path) -> None:
        """把 Instance 连接描述写入私有 JSON 文件，供另一个进程读取。

        Args:
            instance: 已运行的实例。
            path: Pipeline 侧描述文件路径。

        该文件不是快照；实例关闭后它不能恢复实例。
        """
        write_json(path, instance.to_dict(), private=True)


def start(ref, context=None):
    return Runtime().start(ref, context)


def exec(instance, command, cwd=None, timeout=300):
    return Runtime(instance.root).exec(instance, command, cwd, timeout)


def upload(instance, local_path, remote_path):
    return Runtime(instance.root).upload(instance, local_path, remote_path)


def download(instance, remote_path, local_path):
    return Runtime(instance.root).download(instance, remote_path, local_path)


def save(instance, name):
    return Runtime(instance.root).save(instance, name)


def close(instance):
    return Runtime(instance.root).close(instance)


connect = Runtime.connect
