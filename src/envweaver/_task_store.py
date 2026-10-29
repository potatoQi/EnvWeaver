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
    from ._io.files import pack, unpack


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


def publish(stage, root, name):
    """Publish a task directory atomically and return its reference."""
    target = root / name
    if target.exists() or target.is_symlink():
        raise FileExistsError("Task already exists")
    stage.rename(target)
    return str(target / "task.json")


def resolve_task_path(root, path):
    path = Path(path).resolve(strict=True)
    if path.name != "task.json" or path.parent.parent != root.resolve():
        raise ValueError("Task reference belongs to a different Pool")
    return path


def list_tasks(root):
    """Read metadata from the Pool's task directories."""
    for path in sorted(root.glob("*/task.json")):
        if path.parent.name.startswith("."):
            continue
        try:
            resolve_task_path(root, path)
        except ValueError:
            continue
        yield {"path": str(path), "data": json.loads(path.read_text(encoding="utf-8"))}


def operate(payload):
    root = Path(payload["root"])
    action = payload["action"]
    if action == "list":
        return list(list_tasks(root))
    if action == "read":
        path = resolve_task_path(root, payload["path"])
        _, body = pack(path.parent)
        return {"body": base64.b64encode(body).decode()}

    name = payload["name"]
    validate_name(name)
    target, lock = root / name, root / f".{name}.lock"
    if action == "reserve":
        root.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        if target.exists() or target.is_symlink():
            lock.unlink()
            raise FileExistsError("Task already exists")
    elif action == "release":
        lock.unlink(missing_ok=True)
    elif action == "publish":
        with tempfile.TemporaryDirectory(prefix=".save-", dir=root) as temporary:
            stage = Path(temporary) / "task"
            body = base64.b64decode(payload["body"])
            unpack("directory", body, stage, mode=0o700)
            return publish(stage, root, name)
    else:
        raise ValueError(f"Unknown task operation: {action}")


if __name__ == "__main__":
    try:
        response = {"result": operate(json.load(sys.stdin))}
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        response = {"error": {"type": type(error).__name__, "message": str(error)}}
    print(json.dumps(response))
