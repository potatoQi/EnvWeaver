import json
import math
import os
import tempfile
from pathlib import Path


def validate_setup(setup):
    """Validate the shared Runtime/Task preparation contract."""
    if setup is None:
        return None
    if not isinstance(setup, dict) or set(setup) != {"command", "timeout"}:
        raise ValueError("setup must contain command and timeout")
    command, timeout = setup["command"], setup["timeout"]
    parts = command if isinstance(command, list) else [command]
    if (
        not parts
        or any(not isinstance(p, str) or "\0" in p for p in parts)
        or not parts[0].strip()
    ):
        raise ValueError("setup.command must be a nonempty command string or argument list")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("setup.timeout must be a finite positive number")
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
