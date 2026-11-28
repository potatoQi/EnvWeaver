from dataclasses import replace
from pathlib import Path

from .._io.paths import normalize_address
from .._io.retry import retry
from .._io.ssh_config import normalize as normalize_ssh
from .._io.storage import local_source, local_target
from .._util import read_json, validate_setup, write_json
from ..errors import RuntimeError
from ..models import Instance, RuntimeRef
from ._connection import fixed_connection, merge_connection
from ._relay import RelayBackend
from .base import RuntimeBackend
from .docker import DockerBackend
from .qemu import QemuBackend

_BUILTIN_BACKENDS = {"docker": DockerBackend, "qemu": QemuBackend, "_verifier": RelayBackend}


class Runtime:
    """
    Args:
        root: 环境记录存储目录；省略时不写记录。
        host: 后端服务地址；Docker、QEMU 填 ssh://服务器，不填则使用当前机器。自定义后端决定地址格式。
        ssh: 沙箱服务器的 SSH 配置，字段见[SSH 配置](runtime.md#ssh-config)。新建环境时不填则使用系统默认配置；connect 时不填沿用连接记录，{} 使用系统默认配置，非空字典与记录递归合并。

    Raises:
        ValueError: root 或 host 为空，或 root 地址格式有误。
        TypeError: ssh 不是字典。
    """

    def __init__(
        self, root: str | Path | None = None, *, host: str | None = None, ssh: dict | None = None
    ):
        if isinstance(root, str) and not root.strip():
            raise ValueError("root must be a nonempty directory path")
        if host is not None and (not isinstance(host, str) or not host.strip()):
            raise ValueError("host must be a nonempty string or None")
        self.host = host
        self.ssh = normalize_ssh(ssh)
        self._ssh_explicit = ssh is not None
        self.root = normalize_address(root) if root is not None else None
        self._backends: dict[str, RuntimeBackend] = {}

    def register(self, name: str, backend: type[RuntimeBackend]) -> None:
        """添加自定义后端。

        Args:
            name: 后端名称，之后填入 create 的 backend 参数。
            backend: 自定义后端类，构造函数接受 root、host、ssh。方法见下方 RuntimeBackend。

        Returns:
            None。

        Raises:
            TypeError: 后端类不接受 root、host 或 ssh 参数。

        同名注册会替换原后端，仅对当前 Runtime 对象生效。
        后端构造函数抛出的异常会直接传给调用者。
        """
        self._backends[name] = backend(
            root=self.root,
            host=self.host,
            ssh=self.ssh if self._ssh_explicit or self.ssh else None,
        )

    def _backend(self, name):
        if name not in self._backends:
            if name not in _BUILTIN_BACKENDS:
                raise ValueError(f"Unknown runtime backend: {name}")
            self.register(name=name, backend=_BUILTIN_BACKENDS[name])
        return self._backends[name]

    def _open_pipe(self, instance, command):
        if instance.backend not in self._backends and instance.backend not in _BUILTIN_BACKENDS:
            return None
        factory = getattr(self._backend(instance.backend), "_open_pipe", None)
        return factory(instance, command) if factory else None

    def create(
        self,
        backend: str,
        image: str,
        config: dict | None = None,
        connection: dict | None = None,
        *,
        metadata: dict | None = None,
        name: str | None = None,
    ) -> RuntimeRef:
        """检查现有镜像并创建引用。

        Args:
            name: 环境名称；不填时使用传入的 image。
            backend: docker、qemu 或通过 register 注册的后端名称。
            image: Docker 镜像名/ID、QEMU 磁盘路径，或自定义后端的环境标识。
            config: 后端配置，默认 {}，字段见[后端配置](runtime.md#backend-config)。
            connection: SSH 用户名、Python 路径或 VNC 密码环境变量名，见[连接配置](runtime.md#connection-config)。

            metadata: 自定义 JSON 字典，默认 {}。

        Returns:
            可传给 start 的 RuntimeRef。

        Raises:
            ValueError: 镜像或名称为空、后端未注册、配置有误，或 metadata 包含非有限数字、无效 tags。
            TypeError: name 不是字符串，config、connection 或 metadata 不是字典，或 metadata 包含不可序列化的值。
            OSError: 镜像文件不存在、不可读，或缺少所需程序。
            envweaver.errors.RuntimeError: Docker 镜像不存在或后端检查失败。
            TimeoutError: SSH 操作超时。
            subprocess.TimeoutExpired: Docker 命令超时。
        """
        if not isinstance(image, str) or not image.strip():
            raise ValueError("image must be a nonempty string")
        if config is not None and not isinstance(config, dict):
            raise TypeError("config must be a dictionary")
        fixed = fixed_connection(backend, connection)
        metadata = {} if metadata is None else metadata
        # Validate before the backend performs any operations.
        requested = RuntimeRef(backend=backend, image=image, metadata=metadata, name=name)
        ref = self._backend(backend).create(image=image, config=config, connection=fixed)
        return replace(ref, metadata=requested.metadata, name=requested.name)

    def start(
        self,
        ref: RuntimeRef,
        context: list[dict[str, str]] | None = None,
        *,
        setup: dict | None = None,
        connection: dict | None = None,
    ) -> Instance:
        """启动容器或虚拟机。

        Docker 镜像缺少默认 python3 时自动安装；安装需能访问软件源，并具备容器内 root 或免密 sudo 权限。

        Args:
            ref: 要启动的环境，包含镜像和启动配置。
            context: 启动后要复制进去的文件列表，不填则不复制。<br>
                每项的 source 填文件或目录路径；
                target 填容器或虚拟机内的目标路径。
            setup: 复制 context 后执行的准备操作；command 为命令字符串或参数列表，
                timeout 为最长执行秒数，须大于 0。命令准备完成后以退出码 0 结束。
            connection: 本次连接设置；重新填写的覆盖 ref.connection 中的值，没填的保留原值。
                字段见[连接配置](runtime.md#connection-config)。

        Returns:
            完成文件复制和准备操作后的 Instance。

        Raises:
            TypeError: ref 不是 RuntimeRef 对象，或 connection 不是字典。
            ValueError: 后端名称、配置或 setup 有误。
            OSError: 无法访问镜像、输入文件或存储目录，或缺少所需程序。
            envweaver.errors.RuntimeError: 环境启动、Python 准备或初始化文件传输失败，或准备命令返回非零退出码。
            TimeoutError: 初始化命令或 SSH 操作超时。
            subprocess.TimeoutExpired: Docker 或 QEMU 命令超时。
        """
        return self._start(ref, context, setup=setup, connection=connection)

    def _start(
        self,
        ref,
        context=None,
        *,
        setup=None,
        connection=None,
        on_started=None,
        check_cancelled=None,
    ):
        """Eval can own an instance before context/setup finish, without changing start's API."""
        if not isinstance(ref, RuntimeRef):
            raise TypeError("ref must be a RuntimeRef object")
        setup = validate_setup(setup)
        connection = merge_connection(ref.connection, connection)
        backend = self._backend(ref.backend)
        instance = backend.start(ref, connection=connection)
        if on_started is not None:
            on_started(instance)
        try:
            for item in context or []:
                if check_cancelled is not None:
                    check_cancelled()
                self.upload(instance, source=item["source"], target=item["target"])
            if setup is not None:
                if check_cancelled is not None:
                    check_cancelled()
                code, stdout, stderr = self.exec(
                    instance=instance, command=setup["command"], timeout=setup["timeout"]
                )
                if code:
                    raise RuntimeError(f"Setup exited with code {code}: {stderr or stdout}")
        except BaseException as error:
            if on_started is None:
                try:
                    backend.close(instance)
                except Exception as cleanup_error:
                    error.add_note(f"Instance cleanup also failed: {cleanup_error}")
            raise
        return instance

    def exec(
        self,
        instance: Instance,
        command: str | list[str],
        cwd: str | None = None,
        timeout: float | None = 300,
    ) -> tuple[int, str, str]:
        """在容器或虚拟机中执行命令。

        Args:
            instance: 要操作的 Instance。
            command: 命令字符串，或程序及参数列表，如 ["python3", "-c", "print(1)"]。
            cwd: 在容器或虚拟机内的哪个目录执行。
            timeout: 最长执行秒数，None 表示不限时。

        Returns:
            退出码、标准输出、标准错误。命令执行失败时返回非零退出码。

        Raises:
            TimeoutError: 目标命令或 SSH 操作超时。
            ValueError: 后端未注册，或连接配置、访问密钥有误。
            envweaver.errors.RuntimeError: 后端操作失败，或未收到可解析的执行响应。
            OSError: 无法启动所需程序。
            subprocess.TimeoutExpired: Docker 命令超时。

        """
        return self._backend(instance.backend).exec(instance, command, cwd, timeout)

    def upload(self, instance: Instance, source: str | Path, target: str) -> None:
        """上传文件或目录到容器或虚拟机。

        Args:
            instance: 要操作的 Instance。
            source: 要上传的文件或目录路径。
            target: 容器或虚拟机内的完整目标路径，包含文件名或目录名。

        Returns:
            None。

        Raises:
            OSError: 文件无法读取、路径类型冲突，或缺少所需程序。
            ValueError: 文件地址、后端或连接配置有误，或待传目录含不支持的符号链接。
            envweaver.errors.RuntimeError: 上传失败，例如目标路径不可写或连接服务报错。
            TimeoutError: 目标准备命令或 SSH 操作超时。
            subprocess.TimeoutExpired: Docker 命令超时。

        """
        with local_source(source) as path:
            return retry(
                lambda: self._backend(instance.backend).upload(instance, path, target), replay=True
            )

    def download(self, instance: Instance, source: str, target: str | Path) -> None:
        """下载容器或虚拟机内的文件或目录。

        Args:
            instance: 要下载文件的 Instance。
            source: 容器或虚拟机内的文件或目录。
            target: 下载保存路径，包含文件名或目录名。

        Returns:
            None。

        Raises:
            OSError: 目标无法写入、路径类型冲突，或缺少所需程序。
            ValueError: 文件地址、后端或连接配置有误，或返回的文件包路径不合法。
            envweaver.errors.RuntimeError: 下载失败，例如远端文件不存在或连接服务报错。
            TimeoutError: 目标检查命令或 SSH 操作超时。
            subprocess.TimeoutExpired: Docker 命令超时。

        """
        with local_target(target) as path:
            retry(
                lambda: self._backend(instance.backend).download(instance, source, path),
                replay=True,
            )

    def save(self, instance: Instance) -> RuntimeRef:
        """将当前实例保存为可复用的环境。

        Args:
            instance: start 返回的 Instance；connect 返回的实例不能保存。

        Returns:
            包含镜像、启动配置、[连接配置](runtime.md#connection-config)和原引用 name、metadata 的 RuntimeRef。

        Raises:
            ValueError: 实例来自 connect，或后端、配置、保存模式有误。
            OSError: 无法读写环境资产，或缺少所需程序。
            envweaver.errors.RuntimeError: 保存失败，或虚拟机无法正常关机、重启。
            TimeoutError: 连接虚拟机或 SSH 操作超时。
            subprocess.TimeoutExpired: Docker 或 QEMU 命令超时。
        """
        if instance.data.get("_connected"):
            raise ValueError(
                "Cannot save an instance returned by connect; use the instance returned by start"
            )
        return self._backend(instance.backend).save(instance)

    def close(self, instance: Instance) -> None:
        """关闭并删除容器或虚拟机，保留已保存的环境。

        Args:
            instance: 要关闭的 Instance。

        Returns:
            None。

        Raises:
            ValueError: 后端或连接配置有误。
            OSError: 无法删除实例文件，或缺少所需程序。
            envweaver.errors.RuntimeError: 容器或虚拟机关闭失败。
            TimeoutError: 连接虚拟机或 SSH 操作超时。
            subprocess.TimeoutExpired: Docker 命令超时。
        """
        return self._backend(instance.backend).close(instance)

    def connect(self, path: str | Path) -> Instance:
        """连接已经运行的环境。

        Args:
            path: instance.json 文件路径。

        Returns:
            连接到的 Instance，不能用于 save。验证程序收到的连接支持 exec、upload、download 和 screenshot。

        Raises:
            OSError: 文件无法读取，或缺少连接程序。
            ValueError: 文件地址、记录格式无效或环境配置不受支持。
            envweaver.errors.RuntimeError: SSH 文件传输失败，或环境不存在、已退出或无法连接。
            TimeoutError: 文件传输或连接超时。
        """
        with local_source(path) as source:
            record = read_json(source)
        if (
            not isinstance(record, dict)
            or type(record.get("version")) is not int
            or record["version"] != 1
        ):
            raise ValueError("Unsupported instance record; expected version 1")
        if not isinstance(record.get("backend"), str):
            raise ValueError("backend is required")
        backend = self._backend(record["backend"])
        instance = backend.connect(record)
        return replace(instance, data={**instance.data, "_connected": True})

    def describe(self, instance: Instance, path: str | Path) -> None:
        """保存实例的连接记录。

        Args:
            instance: 要记录的 Instance。
            path: JSON 文件的保存路径。

        Returns:
            None。

        Raises:
            OSError: 文件无法写入。
            ValueError: 文件地址无效、后端未注册或记录包含无效的 JSON 值。
            TypeError: 记录包含不能保存为 JSON 的对象。
            envweaver.errors.RuntimeError: SSH 文件传输失败。
            TimeoutError: SSH 文件传输超时。
        """
        record = dict(self._backend(instance.backend).describe(instance))
        record.update(version=1, backend=instance.backend, host=instance.host)
        if instance.data.get("host_ssh"):
            record["host_ssh"] = instance.data["host_ssh"]
        with local_target(path) as target:
            write_json(target, record, private=True)

    def screenshot(self, instance: Instance) -> bytes:
        """获取桌面截图。

        Docker 使用 VNC，调用该接口的环境需要安装 `envweaver[desktop]`；QEMU 使用 QMP。

        Args:
            instance: 要截图的 Instance。

        Returns:
            PNG 图片字节。

        Raises:
            ValueError: 实例未配置桌面连接。
            OSError: 截图程序不可用，或截图文件无法读取、格式无效。
            envweaver.errors.RuntimeError: 截图失败。
            TimeoutError: 截图超时。
        """
        return self._backend(instance.backend).screenshot(instance)
