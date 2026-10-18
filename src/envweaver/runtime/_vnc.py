"""Isolated VNC client: no reactor threads or credentials left in the caller."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ..errors import RuntimeError


def screenshot(connection):
    if not isinstance(connection, dict) or not connection.get("host"):
        raise ValueError("Configure a vnc connection for desktop screenshots")
    password = None
    if connection.get("password_env"):
        password = os.environ.get(connection["password_env"])
        if password is None:
            raise ValueError(f"Set {connection['password_env']} for VNC authentication")
    script = """import json, sys
from vncdotool import api
p = json.load(sys.stdin)
client = api.connect(p['server'], password=p['password'], timeout=30)
try:
    client.captureScreen(p['path'])
finally:
    client.disconnect()
    api.shutdown()
"""
    with tempfile.TemporaryDirectory(prefix="envweaver-vnc-") as directory:
        target = Path(directory) / "screen.png"
        request = {
            "server": f"{connection['host']}::{int(connection.get('port', 5900))}",
            "password": password,
            "path": str(target),
        }
        try:
            result = subprocess.run(
                [sys.executable, "-c", script],
                input=json.dumps(request).encode(),
                capture_output=True,
                timeout=45,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("VNC screenshot timed out") from error
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace"))
        return target.read_bytes()
