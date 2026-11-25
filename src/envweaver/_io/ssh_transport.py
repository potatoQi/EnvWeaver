"""Run commands over SSH with a JSON request on standard input."""

import json
import shlex
import subprocess
from urllib.parse import urlsplit

from ..errors import RuntimeError
from .retry import connection_error, retry
from .ssh_config import flags


def _parse_host(host):
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
    _ = parsed.port  # Validate port syntax and range.
    return parsed


def _validate_host(host):
    if host is not None:
        _parse_host(host)
    return host


def command_args(host, command, *, ssh_options=None):
    parsed = _parse_host(host)
    destination = parsed.hostname
    if parsed.username:
        destination = f"{parsed.username}@{destination}"
    args = ["ssh", *flags(ssh_options), "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if parsed.port is not None:
        args += ["-p", str(parsed.port)]
    args += ["--", destination, shlex.join(command)]
    return args


def ssh(host, command, payload, *, timeout=60, ssh_options=None):
    args = command_args(host, command, ssh_options=ssh_options)
    body = json.dumps(payload, allow_nan=False).encode()

    def send():
        try:
            result = subprocess.run(args, input=body, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("Remote EnvWeaver call timed out") from error
        if result.returncode:
            message = result.stderr.decode(errors="replace")
            if result.returncode == 255:
                raise connection_error(message)
            raise RuntimeError(f"Remote command failed: {message}")
        return result.stdout

    return retry(send)
