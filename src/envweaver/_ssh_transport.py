"""Run commands over SSH with a JSON request on standard input."""

import json
import shlex
import subprocess
from urllib.parse import urlsplit

from ._ssh_config import flags
from .errors import RuntimeError


def _validate_host(host):
    if host is None:
        return None
    if not isinstance(host, str):
        raise ValueError("host must be an SSH URL, e.g. ssh://user@server:22")
    parsed = urlsplit(host)
    if (
        parsed.scheme != "ssh"
        or not parsed.hostname
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or any(c.isspace() for c in host)
    ):
        raise ValueError("host must be an SSH URL without a password or path")
    parsed.port  # Validate port syntax and range.
    return host


def ssh(host, command, payload, *, timeout=60, ssh_options=None):
    parsed = urlsplit(_validate_host(host))
    destination = parsed.hostname
    if parsed.username:
        destination = f"{parsed.username}@{destination}"
    args = ["ssh", *flags(ssh_options), "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if parsed.port is not None:
        args += ["-p", str(parsed.port)]
    args += ["--", destination, shlex.join(command)]
    try:
        result = subprocess.run(
            args,
            input=json.dumps(payload, allow_nan=False).encode(),
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError(
            "Remote EnvWeaver call timed out; check the remote instance before retrying"
        ) from error
    if result.returncode:
        raise RuntimeError(f"Remote command failed: {result.stderr.decode(errors='replace')}")
    return result.stdout


