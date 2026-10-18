"""Task directory operations sent over SSH; the server needs only Python."""

import base64
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

if __package__:
    from ._files import pack, unpack


def validate_name(name):
    """Validate the directory name before any storage operation."""
    if not isinstance(name, str) or not name or name != name.strip():
        raise ValueError("Task name must be nonempty, without leading or trailing whitespace")
    if (
        name.startswith(".")
        or name.endswith(".")
        or any(c in '/\\<>:"|?*' or ord(c) < 32 or ord(c) == 127 for c in name)
        or re.fullmatch(
            r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³]",
            name.split(".")[0].rstrip().upper(),
        )
    ):
        raise ValueError("Task name must be a valid directory name, without a leading dot")
    # Leave six bytes for the .<name>.lock filename.
    if len(name.encode("utf-8")) > 249:
        raise ValueError("Task name must not exceed 249 UTF-8 bytes")


def publish(stage, root, name, *, format="directory", body=None):
    """Publish one task representation and return its reference."""
    target = root / "tasks" / name
    zipped = root / "archives" / f"{name}.zip"
    if target.exists() or target.is_symlink() or zipped.exists() or zipped.is_symlink():
        raise FileExistsError("Task already exists")
    if format == "zip":
        try:
            zipped.parent.mkdir(parents=True, exist_ok=True)
        except FileExistsError as error:
            raise NotADirectoryError(str(zipped.parent)) from error
        with tempfile.TemporaryDirectory(prefix=".save-", dir=zipped.parent) as temporary:
            packed = Path(temporary) / "task.zip"
            packed.write_bytes(body if body is not None else pack(stage)[1])
            packed.rename(zipped)
        return str(zipped)
    stage.rename(target)
    return str(target / "task.json")


def list_tasks(root):
    """Read only metadata when searching directories and ZIPs."""
    for folder, pattern in (("tasks", "*/task.json"), ("archives", "*.zip")):
        directory = root / folder
        for path in sorted(directory.glob(pattern)):
            name = path.parent.name if folder == "tasks" else path.name
            if name.startswith(".") or not path.resolve().is_relative_to(directory.resolve()):
                continue
            if folder == "tasks":
                data = json.loads(path.read_text(encoding="utf-8"))
            else:
                with zipfile.ZipFile(path) as archive:
                    data = json.loads(archive.read("task.json"))
            yield {"path": str(path), "data": data}


def operate(payload):
    tasks = Path(payload["root"]) / "tasks"
    action = payload["action"]
    if action == "list":
        return list(list_tasks(tasks.parent))
    if action == "read":
        path = Path(payload["path"]).resolve(strict=True)
        if (
            path.name != "task.json"
            or path.parent == tasks.resolve()
            or not path.is_relative_to(tasks.resolve())
        ):
            raise ValueError("Task reference belongs to a different Pool")
        _, body = pack(path.parent)
        return {"body": base64.b64encode(body).decode()}

    name = payload["name"]
    validate_name(name)
    target, lock = tasks / name, tasks / f".{name}.lock"
    if action == "reserve":
        tasks.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        zipped = tasks.parent / "archives" / f"{name}.zip"
        if target.exists() or target.is_symlink() or zipped.exists() or zipped.is_symlink():
            lock.unlink()
            raise FileExistsError("Task already exists")
    elif action == "release":
        lock.unlink(missing_ok=True)
    elif action == "publish":
        with tempfile.TemporaryDirectory(prefix=".save-", dir=tasks) as temporary:
            stage = Path(temporary) / "task"
            body = base64.b64decode(payload["body"])
            unpack("directory", body, stage, mode=0o700)
            return publish(stage, tasks.parent, name, format=payload["format"], body=body)
    else:
        raise ValueError(f"Unknown task operation: {action}")


if __name__ == "__main__":
    try:
        response = {"result": operate(json.load(sys.stdin))}
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        response = {"error": {"type": type(error).__name__, "message": str(error)}}
    print(json.dumps(response))
