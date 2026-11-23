"""Task files and their Pool annotations."""

import base64
import json
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .._io.storage import local_source
from .._task_store import validate_name
from .._util import clean_tags, metadata_tags, validate_goal, write_json
from ..runtime import Runtime
from ..task import TaskPool
from .runtimes import normalize_ref, same_environment
from .store import Conflict, digest, encode


def safe_path(root, relative):
    p = PurePosixPath(relative)
    if not p.parts or p.is_absolute() or ".." in p.parts or "\\" in relative or "\x00" in relative:
        raise ValueError("文件路径须位于任务目录内")
    root = Path(root).resolve()
    path = root.joinpath(*p.parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError("文件路径越界")
    return path


def copy_materials(source, target):
    source = Path(source)
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError("任务材料不能包含符号链接")
        if not path.is_dir() and not path.is_file():
            raise ValueError("任务材料只能包含普通文件和目录")
    shutil.copytree(source, target)


def check_manifest(directory, manifest):
    allowed = {
        "version",
        "name",
        "goal",
        "metadata",
        "task_runtime_ref",
        "verifier_runtime_ref",
        "context",
        "setup",
        "cwd",
        "verifier",
    }
    if set(manifest) - allowed:
        raise ValueError("Task 包含未知字段")
    validate_name(manifest["name"])
    validate_goal(manifest["goal"])
    if not isinstance(manifest.get("metadata", {}), dict):
        raise ValueError("metadata 须为字典")
    for ref in ("task_runtime_ref", "verifier_runtime_ref"):
        manifest[ref] = normalize_ref(manifest[ref])
    paths = [p["path"] for p in manifest["goal"] if p["type"] == "image"]
    for item in manifest.get("context", []):
        if not isinstance(item.get("target"), str) or not item["target"]:
            raise ValueError("context.target 须为非空字符串")
        paths.append(item["source"])
    paths.append(manifest["verifier"]["entry"])
    for path in paths:
        if not isinstance(path, str):
            raise ValueError("材料路径须为字符串")
        safe_path(directory, path)
    write_json(directory / "task.json", manifest)
    # Load through the existing Task contract, without starting environments or grading.
    TaskPool(root=directory.parent, runtime=Runtime()).load(task_ref=directory / "task.json")


class Tasks:
    def __init__(self, store, runtimes):
        self.store, self.runtimes = store, runtimes

    def link(self, asset_id, manifest, host, actor):
        links = self.store.links.setdefault(asset_id, {})
        for role in ("task_runtime_ref", "verifier_runtime_ref"):
            old = self.store.assets.get(links.get(role))
            if old and old["host"] == host and same_environment(old["spec"], manifest[role]):
                continue
            links[role] = self.runtimes.ensure(manifest[role], host, actor)["id"]

    @contextmanager
    def replacement(self, asset_id, revision):
        with self.store.transaction():
            row = self.store.record(asset_id)
            self.store.check_revision(row, revision)
            files, source = self.store.source(asset_id)
            with local_source(files.address(source["directory"])) as original:
                with tempfile.TemporaryDirectory(prefix="envweaver-edit-") as temporary:
                    stage = Path(temporary) / "task"
                    copy_materials(original, stage)
                    yield row, stage
                    stamp = files.publish(source["directory"], stage, source["stamp"])
                    self.store.remember_source(asset_id, source["directory"], stamp)

    def edit(self, asset_id, value, actor):
        with self.replacement(asset_id, value.revision) as (row, stage):
            if value.ref is not None:
                raise ValueError("Task 使用 manifest 内的环境引用")
            manifest = value.manifest if value.manifest is not None else row["spec"]
            manifest = json.loads(encode(manifest))
            if value.name is not None:
                manifest["name"] = value.name
            if value.metadata is not None:
                manifest["metadata"] = value.metadata
            if value.tags is not None:
                manifest.setdefault("metadata", {})["tags"] = clean_tags(value.tags)
            host = value.host if "host" in value.model_fields_set else row["host"]
            check_manifest(stage, manifest)
            self.link(asset_id, manifest, host, actor)
            self.store.update(
                asset_id,
                name=manifest["name"],
                spec=manifest,
                metadata=manifest.get("metadata", {}),
                host=host,
            )
            self.store.set_tags(asset_id, metadata_tags(manifest.get("metadata", {})))
            self.store.touch(asset_id, actor)
            return self.store.record(asset_id)

    def copy(self, asset_id, value, actor):
        with self.store.transaction():
            row = self.store.record(asset_id)
            new_id = digest([row["pool_id"], value.name])
            self.store.check_revision(row, value.revision)
            files, source = self.store.source(asset_id)
            with local_source(files.address(source["directory"])) as original:
                with tempfile.TemporaryDirectory(prefix="envweaver-copy-") as temporary:
                    stage = Path(temporary) / "task"
                    copy_materials(original, stage)
                    manifest = row["spec"]
                    manifest["name"] = value.name
                    check_manifest(stage, manifest)
                    self.store.insert(
                        asset_id=new_id,
                        kind="task",
                        name=value.name,
                        spec=manifest,
                        actor=actor,
                        host=row["host"],
                        pool_id=row["pool_id"],
                    )
                    self.store.links[new_id] = dict(self.store.links.get(asset_id, {}))
                    stamp = files.publish(value.name, stage)
                    self.store.remember_source(new_id, value.name, stamp)
                    return self.store.record(new_id)

    def files(self, asset_id):
        row = self.store.get(asset_id)
        if row["kind"] not in {"task", "trajectory"}:
            raise ValueError("环境记录没有产物文件")
        with self.store.read():
            files, source = self.store.source(asset_id)
        return files.call("files", directory=source["directory"])

    def write_file(self, asset_id, relative, value, actor, *, delete=False):
        if relative == "task.json":
            raise ValueError("请通过 Task 编辑接口修改 task.json")
        with self.replacement(asset_id, value.revision) as (row, stage):
            if row["kind"] != "task":
                raise ValueError("仅 Task 支持材料编辑")
            path = safe_path(stage, relative)
            if delete:
                path.unlink()
            else:
                body = base64.b64decode(value.content_base64, validate=True)
                if len(body) > 16 * 1024 * 1024:
                    raise ValueError("单次文件编辑上限为 16 MiB")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
            check_manifest(stage, row["spec"])
            self.store.touch(asset_id, actor)
            return self.store.record(asset_id)

    def read_file(self, asset_id, relative):
        row = self.store.get(asset_id)
        if row["kind"] not in {"task", "trajectory"}:
            raise Conflict("不是 Task 或轨迹")
        with self.store.read():
            files, source = self.store.source(asset_id)
        if isinstance(files.root, Path):
            path = files.call("resolve", directory=source["directory"], relative=relative)
            return (files.root / source["directory"] / path).read_bytes()
        body = files.call("read", directory=source["directory"], relative=relative)
        return base64.b64decode(body, validate=True)
