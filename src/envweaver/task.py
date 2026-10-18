import base64
import json
import shutil
import tempfile
import weakref
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from ._files import pack, unpack
from ._paths import _location, normalize_address
from ._storage import local_source
from ._task_store import list_tasks, operate, publish, validate_name
from ._util import read_json, validate_setup, write_json
from .errors import TaskExistsError
from .models import Instance, RuntimeRef, Task
from .runtime import Runtime
from .runtime._connection import fixed_connection
from .verifier import Verifier


def _task_runtime(ref: RuntimeRef) -> dict:
    """Keep task requirements, excluding the producer's EnvWeaver settings."""
    omitted = {
        "docker": {"startup_timeout"},
        "qemu": {
            "disk_dir",
            "save_mode",
            "remote_python",
            "binary",
            "image_binary",
            "startup_timeout",
            "shutdown_timeout",
        },
    }
    data = ref.to_dict()
    data["connection"] = fixed_connection(ref.backend, ref.connection)
    for key in omitted.get(ref.backend, ()):
        data["config"].pop(key, None)
    return data


def _copy(source, destination):
    source = Path(source).resolve(strict=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        if destination.resolve().is_relative_to(source):
            raise ValueError(
                "Cannot archive a directory into itself; use a separate verifier package"
            )
        # Materialize symlinks so tasks do not depend on a producer's temporary paths.
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def _archive_assets(stage, goal, entry, context, *, ssh_options=None):
    """Copy producer-owned inputs, recording only task-relative paths."""
    saved_goal = []
    for index, block in enumerate(goal):
        block = dict(block)
        if block["type"] == "image":
            with local_source(block["path"], ssh_options=ssh_options) as source:
                relative = Path("goal") / f"{index}{source.suffix}"
                _copy(source, stage / relative)
            block["path"] = relative.as_posix()
        saved_goal.append(block)
    _copy(entry.parent, stage / "verifier")
    saved_context = []
    for index, item in enumerate(context):
        if not isinstance(item.get("target"), str) or not item["target"]:
            raise ValueError("Context target must be a nonempty string")
        with local_source(item["source"], ssh_options=ssh_options) as source:
            relative = Path("context") / str(index) / source.name
            _copy(source, stage / relative)
        saved_context.append({"source": relative.as_posix(), "target": item["target"]})
    return {"goal": saved_goal, "context": saved_context}


class TaskPool:
    """
    Args:
        root: 任务存储目录，填写本地路径或 ssh://服务器/绝对路径。
        runtime: 保存任务沙箱和执行 Verify 使用的沙箱管理器。
    """

    def __init__(self, root: str | Path, *, runtime: Runtime):
        self.root = normalize_address(root)
        if isinstance(self.root, Path):
            self.root = self.root.resolve()
        self.runtime = runtime

    def _remote(self, action, **payload):
        from ._ssh_transport import ssh

        host, path = _location(self.root)
        source = (
            Path(__file__).with_name("_files.py").read_text()
            + "\n"
            + Path(__file__).with_name("_task_store.py").read_text()
        )
        response = json.loads(
            ssh(
                host,
                ["python3", "-c", source],
                {"root": path, "action": action, **payload},
                **({"ssh_options": self.runtime.ssh} if self.runtime.ssh else {}),
            )
        )
        if error := response.get("error"):
            kind = error["type"]
            if kind == "FileExistsError" and action in {"reserve", "publish"}:
                raise TaskExistsError(error["message"])
            if kind in {"ValueError", "JSONDecodeError"}:
                raise ValueError(error["message"])
            if kind == "BadZipFile":
                raise zipfile.BadZipFile(error["message"])
            if kind == "KeyError":
                raise KeyError(error["message"])
            raise OSError(error["message"])
        return response["result"]

    @contextmanager
    def _destination(self, name):
        def operation(action):
            if isinstance(self.root, Path):
                try:
                    return operate({"root": str(self.root), "action": action, "name": name})
                except FileExistsError as error:
                    raise TaskExistsError(f"Task {name!r} exists or is being saved") from error
            return self._remote(action, name=name)

        operation("reserve")
        try:
            yield self.root / "tasks" / name if isinstance(self.root, Path) else name
        except BaseException as error:
            try:
                operation("release")
            except Exception as cleanup_error:
                error.add_note(f"Failed to release task name: {cleanup_error}")
            raise
        else:
            operation("release")

    def save(
        self,
        instance: Instance,
        name: str,
        goal: list[dict],
        verifier: Verifier,
        verifier_runtime_ref: RuntimeRef,
        context: list[dict[str, str]] | None = None,
        metadata: dict | None = None,
        *,
        setup: dict | None = None,
        format: str = "directory",
    ) -> str:
        """

        Args:
            name: 任务名称，用作目录名，不能为空，同一 TaskPool 内唯一。支持中文；不能以点开头、以点结尾，
                不能包含首尾空白、控制字符、/ \\ < > : " | ? * 或使用 Windows 保留名；最多 249 个 UTF-8 字节。
            goal: 任务要求，按顺序填写文字和图片。文字格式为 {"type": "text", "text": ...}，图片为
                {"type": "image", "path": ...}。图片路径支持本地路径或 ssh://服务器/绝对路径。
            metadata: 自定义任务信息，如来源、标签、软件名称，填写可保存为 JSON 的字典。
            instance: 要保存为任务初始环境的沙箱，须由 Runtime.start 创建。
            context: 任务输入文件列表。source 填本地路径或 ssh://服务器/绝对路径，
                target 填容器或虚拟机内的目标路径。
            setup: 准备命令和超时，格式同 [Runtime.start](index.md#start) 的 setup。
            verifier_runtime_ref: 验证环境的 [RuntimeRef](index.md#runtimeref)。
            verifier: 任务的[验证器](verifier.md)。
            format: directory 保存任务目录；zip 保存 ZIP 文件。

        Returns:
            task_ref，可传给 load。directory 返回 root/tasks/<name>/task.json，
            zip 返回 root/archives/<name>.zip；支持本地路径和 SSH 地址。

        Raises:
            envweaver.errors.TaskExistsError: 同名任务已存在或正在保存。
            ValueError: format 不是 directory 或 zip，或任务名称、验证入口、setup、连接配置无效。
            TypeError: verifier 或 verifier_runtime_ref 类型不符、metadata、连接配置不是字典，或任务信息不能保存为 JSON。
            OSError: 验证器、图片或输入文件无法读取，或存储目录无法写入。
            envweaver.errors.RuntimeError: SSH 文件传输失败。
            TimeoutError: SSH 文件传输超时。

        """
        validate_name(name)
        if format not in ("directory", "zip"):
            raise ValueError("format must be directory or zip")
        setup = validate_setup(setup)
        if metadata is not None and not isinstance(metadata, dict):
            raise TypeError("metadata must be a dictionary")
        metadata = json.loads(json.dumps(metadata or {}, allow_nan=False))
        if not isinstance(goal, list) or not goal:
            raise ValueError("Goal must be a nonempty ordered list")
        for block in goal:
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                continue
            if block.get("type") == "image" and isinstance(block.get("path"), str):
                continue
            raise ValueError("Goal entries must be text/text or image/path objects")
        if not isinstance(verifier, Verifier):
            raise TypeError("verifier must be a Verifier")
        if not isinstance(verifier_runtime_ref, RuntimeRef):
            raise TypeError("verifier_runtime_ref must be a RuntimeRef")
        verification = {
            "entry": "verifier/run.py",
            "timeout": verifier.timeout,
            "required_env": verifier.required_env,
            "requires_task_instance": verifier.requires_task_instance,
        }
        verifier_runtime_data = json.loads(
            json.dumps(_task_runtime(verifier_runtime_ref), allow_nan=False)
        )
        # Reject unserializable configuration before creating environment assets.
        verification = json.loads(json.dumps(verification, allow_nan=False))
        with self._destination(name) as target:
            with tempfile.TemporaryDirectory(
                prefix=".save-", dir=target.parent if isinstance(target, Path) else None
            ) as temporary:
                stage = Path(temporary) / "task"
                stage.mkdir()
                with verifier._local_entry() as entry:
                    assets = _archive_assets(
                        stage, goal, entry, context or [], ssh_options=self.runtime.ssh
                    )
                ref = self.runtime.save(instance)
                write_json(
                    stage / "task.json",
                    {
                        "version": 1,
                        "name": name,
                        "goal": assets["goal"],
                        "metadata": metadata,
                        "task_runtime_ref": _task_runtime(ref),
                        "context": assets["context"],
                        **({"setup": setup} if setup is not None else {}),
                        "verifier_runtime_ref": verifier_runtime_data,
                        "verifier": verification,
                    },
                )
                # Publish atomically on the storage machine.
                if isinstance(target, Path):
                    return publish(stage, self.root, name, format=format)
                _, body = pack(stage)
                path = self._remote(
                    "publish", name=target, body=base64.b64encode(body).decode(), format=format
                )
                host, _ = _location(self.root)
                return host + quote(path, safe="/")

    def load(self, task_ref: str | Path) -> Task:
        """

        Args:
            task_ref: 当前 Task Pool 内的 task.json，或打包后的 ZIP；支持本地路径和 SSH 地址。

        Returns:
            Task 对象；材料路径已转换为可直接使用的本地路径。

        Raises:
            OSError: 任务文件或必要材料不存在、无法读取。
            ValueError: JSON、setup 或连接配置无效、任务不属于当前 Pool、格式版本不支持或路径越界。
            KeyError: 任务文件缺少必要字段。
            TypeError: 保存的 RuntimeRef 字段不匹配，或连接配置不是字典。
            envweaver.errors.RuntimeError: SSH 连接失败。
            TimeoutError: SSH 操作超时。
            zipfile.BadZipFile: ZIP 文件损坏或格式无效。
        """
        address = normalize_address(task_ref)
        filename = address if isinstance(address, Path) else PurePosixPath(_location(address)[1])
        if filename.suffix.lower() == ".zip":
            with local_source(address, ssh_options=self.runtime.ssh) as source:
                return self._load_archive(source.read_bytes(), str(address))
        if isinstance(self.root, Path):
            if not isinstance(address, Path):
                raise ValueError("Task reference belongs to a different Pool")
            path = address.resolve(strict=True)
            if not path.is_relative_to(self.root / "tasks"):
                raise ValueError("Task reference belongs to a different Pool")
            return self._load(path, str(path))
        if not isinstance(address, str):
            raise ValueError("Task reference belongs to a different Pool")
        host, path = _location(address)
        pool_host, pool_path = _location(self.root)
        if host != pool_host or not PurePosixPath(path).is_relative_to(
            PurePosixPath(pool_path) / "tasks"
        ):
            raise ValueError("Task reference belongs to a different Pool")
        result = self._remote("read", path=path)
        return self._load_archive(base64.b64decode(result["body"]), address)

    def _load_archive(self, body, task_ref):
        materials = tempfile.TemporaryDirectory(prefix="envweaver-task-")
        try:
            local = Path(materials.name)
            unpack("directory", body, local, mode=0o700)
            task = self._load(local / "task.json", task_ref)
        except BaseException:
            materials.cleanup()
            raise
        # The Task owns its Verifier; keep files alive when the verifier is retained separately.
        weakref.finalize(task.verifier, materials.cleanup)
        return task

    def _load(self, path, task_ref):
        path = path.resolve(strict=True)
        data = read_json(path)
        if type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("Unsupported task format version; expected 1")
        setup = validate_setup(data.get("setup"))
        for key in ("task_runtime_ref", "verifier_runtime_ref"):
            ref = data[key]
            ref["connection"] = fixed_connection(ref["backend"], ref.get("connection"))

        def asset(relative):
            resolved = (path.parent / relative).resolve(strict=True)
            if not resolved.is_relative_to(path.parent):
                raise ValueError("Task asset escapes its package")
            return str(resolved)

        goal = []
        for part in data["goal"]:
            part = dict(part)
            if part["type"] == "image":
                part["path"] = asset(part["path"])
            goal.append(part)
        context = [{**item, "source": asset(item["source"])} for item in data.get("context", [])]
        verification = dict(data["verifier"])
        verification["entry"] = asset(verification["entry"])
        return Task(
            name=data["name"],
            task_ref=task_ref,
            goal=goal,
            metadata=data.get("metadata", {}),
            task_runtime_ref=RuntimeRef.from_dict(data["task_runtime_ref"]),
            context=context,
            setup=setup,
            verifier_runtime_ref=RuntimeRef.from_dict(data["verifier_runtime_ref"]),
            verifier=Verifier(**verification, runtime=self.runtime),
        )

    def search(self, filters: dict | None = None) -> list[str]:
        """

        Args:
            filters: 按 metadata 字段的值精确筛选，如 {"software": "LibreOffice"}。
                多个条件需同时满足；不填或传 {} 返回全部任务。

        Returns:
            匹配任务的 task_ref 列表，包含 task.json 或 ZIP 的本地路径或 SSH 地址，可传给 load。

        Raises:
            TypeError: filters 不是字典。
            OSError: 任务文件无法读取。
            ValueError: 任务文件不是有效 JSON。
            KeyError: 任务文件缺少必要字段。
            envweaver.errors.RuntimeError: SSH 连接失败。
            TimeoutError: SSH 操作超时。
            zipfile.BadZipFile: ZIP 文件损坏或格式无效。
        """
        if filters is not None and not isinstance(filters, dict):
            raise TypeError("filters must be a dictionary")
        if isinstance(self.root, Path):
            entries = (
                (str(Path(item["path"]).resolve()), item["data"]) for item in list_tasks(self.root)
            )
        else:
            host, _ = _location(self.root)
            entries = (
                (host + quote(item["path"], safe="/"), item["data"])
                for item in self._remote("list")
            )
        found = []
        for path, data in entries:
            metadata = data.get("metadata", {})
            if filters and any(
                key not in metadata or metadata[key] != value for key, value in filters.items()
            ):
                continue
            found.append(path)
        return found
