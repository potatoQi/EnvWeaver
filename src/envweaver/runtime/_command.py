"""This file is also sent as source to prepared Docker images (stdlib only)."""

import json
import os
import signal
import subprocess
import sys


def execute(command, cwd=None, timeout=300):
    if timeout is not None and timeout <= 0:
        raise ValueError("timeout must be positive or None")
    shell = command if isinstance(command, list) else (
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
        if os.name == "nt"
        else ["/bin/sh", "-c", command]
    )
    process = subprocess.Popen(
        shell,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        process.communicate()
        return {"error": "timeout", "message": f"Command exceeded {timeout} seconds"}
    return {
        "exit_code": process.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


if __name__ == "__main__":
    print(json.dumps(execute(**json.loads(sys.argv[1]))))
