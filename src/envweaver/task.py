import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from ._util import read_json, write_json
from .errors import TaskExistsError
from .models import RuntimeRef, Task
from .runtime import Runtime


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


@contextmanager
def _reserve_name(root, name):
    """Hold a single name until publication; other names remain independent."""
    tasks = root / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(name.encode()).hexdigest()
    target, lock = tasks / key, tasks / f".{key}.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise TaskExistsError(f"Task {name!r} exists or is being saved") from error
    os.close(fd)
    try:
        if target.exists():
            raise TaskExistsError(f"Task {name!r} already exists")
        yield target
    finally:
        lock.unlink(missing_ok=True)


def _archive_assets(stage, goal, entry, context):
    """Copy producer-owned inputs, recording only task-relative paths."""
    saved_goal = []
    for index, block in enumerate(goal):
        block = dict(block)
        if block["type"] == "image":
            relative = Path("goal") / f"{index}{Path(block['path']).suffix}"
            _copy(block["path"], stage / relative)
            block["path"] = relative.as_posix()
        saved_goal.append(block)
    _copy(entry.parent, stage / "verifier")
    saved_context = []
    for index, item in enumerate(context):
        if not isinstance(item.get("target"), str) or not item["target"]:
            raise ValueError("Context target must be a nonempty string")
        relative = Path("context") / str(index) / Path(item["source"]).name
        _copy(item["source"], stage / relative)
        saved_context.append({"source": relative.as_posix(), "target": item["target"]})
    return {"goal": saved_goal, "verifier": "verifier/run.py", "context": saved_context}


class TaskPool:
    def __init__(self, root, *, runtime=None):
        self.root = Path(root).expanduser().resolve()
        self.runtime = runtime or Runtime()

    def save(self, instance, name, goal, verifier, context=None, metadata=None):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Task name must be a nonempty string")
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
        entry = Path(verifier).resolve(strict=True)
        if entry.name != "run.py":
            raise ValueError("Verifier entry must be named run.py")
        with _reserve_name(self.root, name) as target:
            with tempfile.TemporaryDirectory(prefix=".save-", dir=target.parent) as temporary:
                stage = Path(temporary) / "task"
                stage.mkdir()
                assets = _archive_assets(stage, goal, entry, context or [])
                ref = self.runtime.save(instance, name)
                write_json(
                    stage / "task.json",
                    {
                        "version": 1,
                        "name": name,
                        "runtime": ref.to_dict(),
                        **assets,
                        "metadata": metadata,
                    },
                )
                # Same filesystem rename: readers see either no task or the complete task.
                stage.rename(target)
            return str(target / "task.json")

    def load(self, task_ref):
        path = Path(task_ref).resolve(strict=True)
        if not path.is_relative_to(self.root / "tasks"):
            raise ValueError("Task reference belongs to a different Pool")
        data = read_json(path)
        if data.get("version") != 1:
            raise ValueError("Unsupported task format version")

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
        context = [{**item, "source": asset(item["source"])} for item in data["context"]]
        return Task(
            name=data["name"],
            task_ref=str(path),
            runtime=RuntimeRef.from_dict(data["runtime"]),
            goal=goal,
            verifier=asset(data["verifier"]),
            context=context,
            metadata=data["metadata"],
        )

    def search(self, query=None, filters=None):
        found = []
        for path in sorted((self.root / "tasks").glob("*/task.json")):
            if path.parent.name.startswith("."):
                continue
            data = read_json(path)
            metadata = data["metadata"]
            if filters and any(
                key not in metadata or metadata[key] != value for key, value in filters.items()
            ):
                continue
            text = " ".join(
                [
                    data["name"],
                    *(part.get("text", "") for part in data["goal"]),
                    json.dumps(metadata, ensure_ascii=False),
                ]
            )
            if query and query.casefold() not in text.casefold():
                continue
            found.append(str(path.resolve()))
        return found
