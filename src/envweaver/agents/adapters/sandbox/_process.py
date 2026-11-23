"""Standalone stdlib-only process supervisor, copied into Linux task sandboxes."""

import base64
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path


def write(path, value):
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def finish(directory, code, error=None):
    status = {"code": code, "offsets": {
        name: (directory / name).stat().st_size if (directory / name).exists() else 0
        for name in ("stdout", "stderr")
    }}
    if error is not None:
        status["error"] = str(error)
    write(directory / "status.json", status)


def supervise(directory):
    request_path = directory / "request.json"
    process = None
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request_path.unlink()  # Credentials are not retained as artifacts.
        if (directory / "cancel").exists():
            finish(directory, -1)
            return
        with (
            (open(request["stdin_path"], "rb") if request.get("stdin_path") else nullcontext(subprocess.DEVNULL)) as stdin,
            (directory / "stdout").open("wb") as stdout,
            (directory / "stderr").open("wb") as stderr,
        ):
            process = subprocess.Popen(
                request["command"],
                cwd=request["cwd"],
                env={**os.environ, **request.get("env", {})},
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            stopping = False
            while process.poll() is None:
                if not stopping and (directory / "cancel").exists():
                    # Request the CLI's own shutdown; task services belong to the Instance.
                    process.send_signal(signal.SIGTERM)
                    stopping = True
                time.sleep(0.05)
            finish(directory, process.returncode)
    except BaseException as error:
        request_path.unlink(missing_ok=True)
        if process is not None:
            process.kill()
            process.wait()
        finish(directory, -1, error)


def main(action, directory, payload):
    directory = Path(directory)
    if action == "start":
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (directory / "cancel").exists():
            finish(directory, -1)
            return {"started": False}
        request_path = directory / "request.json"
        try:
            write(request_path, payload)
            os.chmod(request_path, 0o600)
            subprocess.Popen(
                [sys.executable, __file__, "supervise", str(directory)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as error:
            request_path.unlink(missing_ok=True)
            finish(directory, -1, error)
            raise
        return {"started": True}
    if action == "stop":
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        (directory / "cancel").touch()
        # The supervisor signals its live child, avoiding stale PID files.
        return {}
    if action == "poll":
        status_path = directory / "status.json"
        deadline = time.monotonic() + payload.get("wait", 0)
        while not status_path.exists() and time.monotonic() < deadline:
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        status = json.loads(status_path.read_text()) if status_path.exists() else None
        chunks = {}
        for name in ("stdout", "stderr"):
            if name not in payload:
                continue
            path = directory / name
            offset = payload[name]
            size = 256 * 1024
            if status is not None:
                size = min(size, max(0, status["offsets"][name] - offset))
            chunk = b""
            if size and path.exists():
                with path.open("rb") as stream:
                    stream.seek(offset)
                    chunk = stream.read(size)
            chunks[name] = chunk
        # Exit can race with reading the logs. Trim against the final boundary too.
        if status is None and status_path.exists():
            status = json.loads(status_path.read_text())
        result = {"status": status, "streams": {}}
        for name, chunk in chunks.items():
            offset = payload[name]
            if status is not None:
                chunk = chunk[:max(0, status["offsets"][name] - offset)]
            result["streams"][name] = {
                "offset": offset + len(chunk),
                "data": base64.b64encode(chunk).decode(),
            }
        return result
    if action == "read":
        path = Path(payload["path"])
        if not path.is_file():
            return {"data": None}
        # Native trajectories are snapshots; callers tolerate a partially written JSON.
        return {"data": base64.b64encode(path.read_bytes()).decode()}
    raise ValueError(f"Unknown operation: {action}")


if __name__ == "__main__":
    if sys.argv[1] == "supervise":
        supervise(Path(sys.argv[2]))
    else:
        value = sys.argv[3]
        if value.startswith("@"):
            path = Path(value[1:])
            try:
                value = path.read_text(encoding="utf-8")
            finally:
                path.unlink(missing_ok=True)
        print(json.dumps(main(sys.argv[1], sys.argv[2], json.loads(value))))
