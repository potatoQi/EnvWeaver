"""OpenSSH transport; the guest executes only transient Python standard-library code."""

import base64
import json
import shlex
import subprocess
import zlib
from pathlib import Path

from .._io.retry import connection_error, retry
from .._io.ssh_config import flags
from .._io.ssh_transport import _parse_host
from ..errors import RuntimeError


def command_args(connection, command):
    if not isinstance(connection, dict) or not connection.get("host") or not connection.get("user"):
        raise ValueError("ssh.host and ssh.user are required")
    for key in ("host", "user"):
        if any(c.isspace() for c in connection[key]) or connection[key].startswith("-"):
            raise ValueError(f"Invalid ssh.{key}")
    guest_options = flags(connection)
    args = ["ssh", *guest_options, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if connection.get("via"):
        jump = _parse_host(connection["via"])
        address = (jump.username + "@" if jump.username else "") + jump.hostname
        jump_options = connection.get("via_ssh")
        if jump_options or guest_options:
            proxy = [
                "ssh",
                *flags(jump_options),
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
            ]
            if jump.port:
                proxy += ["-p", str(jump.port)]
            proxy += ["-W", "%h:%p", "--", address]
            args += ["-o", "ProxyCommand=" + shlex.join(proxy)]
        else:
            if jump.port:
                address += ":" + str(jump.port)
            args += ["-J", address]
    port = connection.get("port", 22)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("ssh.port must be an integer in 1..65535")
    args += ["-p", str(port)]
    return [*args, "--", connection["user"] + "@" + connection["host"], shlex.join(command)]


def request(connection, action, **payload):
    args = command_args(connection, [])
    executable = connection.get("python", "python3")
    if any(c in executable for c in "\r\n\"'`$;&|<>"):
        raise ValueError("Invalid ssh.python executable")
    source = Path(__file__).with_name("_command.py").read_text().split("if __name__")[0]
    files = Path(__file__).parents[1].joinpath("_io", "files.py").read_text()
    source += (
        "\n"
        + files
        + """
import base64
p = json.load(sys.stdin)
try:
    if p['action'] == 'exec':
        result = execute(p['command'], p.get('cwd'), p.get('timeout', 300))
    elif p['action'] == 'upload':
        unpack(p['kind'], base64.b64decode(p['body']), p['target'])
        result = None
    elif p['action'] == 'download':
        kind, body = pack(p['source'])
        result = {'kind': kind, 'body': base64.b64encode(body).decode()}
    elif p['action'] == 'health':
        result = None
    else:
        raise ValueError('Unknown SSH action')
    response = {'result': result}
except Exception as error:
    response = {'error': {'type': type(error).__name__, 'message': str(error)}}
print(json.dumps(response))
"""
    )
    encoded = base64.b64encode(zlib.compress(source.encode())).decode()
    args[-1] = f'"{executable}" -c "import base64,zlib;exec(zlib.decompress(base64.b64decode(\'{encoded}\')))"'
    timeout = payload.get("timeout", 300)
    body = json.dumps(dict(action=action, **payload)).encode()

    def send():
        try:
            response = subprocess.run(
                args, input=body, capture_output=True,
                timeout=None if timeout is None else timeout + 15,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("SSH operation timed out") from error
        if response.returncode:
            message = response.stderr.decode(errors="replace")
            if response.returncode == 255:
                raise connection_error(message)
            raise RuntimeError(message)
        return response

    response = retry(send, replay=action in {"health", "upload", "download"})
    try:
        result = json.loads(response.stdout)
    except ValueError as error:
        raise RuntimeError("SSH target did not return a Python response") from error
    if "error" in result:
        error = result["error"]
        cls = {
            "FileNotFoundError": FileNotFoundError,
            "PermissionError": PermissionError,
            "ValueError": ValueError,
            "IsADirectoryError": IsADirectoryError,
        }.get(error["type"], RuntimeError)
        raise cls(error["message"])
    return result["result"]


def execute(connection, command, cwd=None, timeout=300):
    result = request(connection, "exec", command=command, cwd=cwd, timeout=timeout)
    if "error" in result:
        raise TimeoutError(result["message"])
    return result["exit_code"], result["stdout"], result["stderr"]


def upload(connection, source, target):
    from .._io.files import pack

    kind, body = pack(source)
    request(connection, "upload", kind=kind, body=base64.b64encode(body).decode(), target=target)


def download(connection, source, target):
    from .._io.files import unpack

    result = request(connection, "download", source=source)
    unpack(result["kind"], base64.b64decode(result["body"]), target)
