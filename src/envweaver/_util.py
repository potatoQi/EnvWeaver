import json
import math
import os
import tempfile
from copy import deepcopy
from pathlib import Path, PurePosixPath, PureWindowsPath


def merge_dicts(base, overrides):
    """Recursively merge dictionaries without sharing mutable input values."""
    merged = deepcopy(base)
    for key, value in overrides.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = merge_dicts(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def clean_tags(tags):
    if any(not isinstance(t, str) or not t.strip() or len(t.strip()) > 100 for t in tags):
        raise ValueError("标签须为 1–100 字符的非空文字")
    return sorted(set(t.strip() for t in tags))


def metadata_tags(metadata):
    """Read asset tags from their single persisted source, metadata.tags."""
    if not isinstance(metadata, dict):
        raise TypeError("metadata must be a dictionary")
    tags = metadata.get("tags", [])
    if not isinstance(tags, list):
        raise ValueError("metadata.tags must be a list of strings")
    return clean_tags(tags)


def validate_cwd(cwd):
    """Validate a guest path without resolving it on the current machine."""
    if (
        not isinstance(cwd, str)
        or not cwd
        or "\0" in cwd
        or not (PurePosixPath(cwd).is_absolute() or PureWindowsPath(cwd).is_absolute())
    ):
        raise ValueError("cwd must be an absolute sandbox path")
    return cwd


def positive(value, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a finite positive number")


def validate_goal(goal):
    """Validate the ordered text/image input shared by Task and Agent execution."""
    if not isinstance(goal, list) or not goal:
        raise ValueError("goal must be a nonempty list")
    for block in goal:
        if not isinstance(block, dict) or block.get("type") not in ("text", "image"):
            raise ValueError("goal entries must be text or image objects")
        key = "text" if block["type"] == "text" else "path"
        if not isinstance(block.get(key), str):
            raise ValueError(f"goal entry requires {key}")


def validate_setup(setup):
    """Validate the shared Runtime/Task preparation contract."""
    if setup is None:
        return None
    if not isinstance(setup, dict) or set(setup) != {"command", "timeout"}:
        raise ValueError("setup must contain command and timeout")
    command, timeout = setup["command"], setup["timeout"]
    parts = command if isinstance(command, list) else [command]
    if not parts or any(not isinstance(p, str) or "\0" in p for p in parts) or not parts[0].strip():
        raise ValueError("setup.command must be a nonempty command string or argument list")
    positive(timeout, "setup.timeout")
    return {"command": list(command) if isinstance(command, list) else command, "timeout": timeout}


def write_json(path, value, *, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, 0o600 if private else 0o644)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run_python(runtime, instance, source, timeout=300):
    """Run internal Python code through the ordinary command interface."""
    return runtime.exec(
        instance=instance,
        command=[instance.data.get("python", "python3"), "-c", source],
        timeout=timeout,
    )
