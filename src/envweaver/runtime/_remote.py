"""Dispatch QEMU operations to EnvWeaver on the target server."""

import json
import sys
from dataclasses import replace

from .. import _ssh_transport
from ..errors import RuntimeError


def call(host, python, payload, *, timeout, ssh_options=None):
    output = _ssh_transport.ssh(
        host,
        [python, "-c", "from envweaver.runtime._remote import main; main()"],
        payload,
        timeout=timeout,
        **({"ssh_options": ssh_options} if ssh_options else {}),
    )
    try:
        response = json.loads(output)
    except ValueError as error:
        raise RuntimeError("Remote Python did not return an EnvWeaver response") from error
    if "error" in response:
        error = response["error"]
        cls = {
            "ValueError": ValueError,
            "FileNotFoundError": FileNotFoundError,
            "PermissionError": PermissionError,
            "TimeoutError": TimeoutError,
        }.get(error["type"], RuntimeError)
        raise cls(error["message"])
    return response["result"]


def dispatch(payload):
    from ..models import Instance, RuntimeRef
    from .qemu import QemuBackend

    # root locates records independently of this worker; worker methods never write there.
    backend = QemuBackend(payload["root"])
    backend._check_ssh = False
    if payload["action"] == "create":
        ref = RuntimeRef.from_dict(payload["ref"])
        return backend.create(ref.image, ref.config, connection=ref.connection).to_dict()
    if payload["action"] == "start":
        ref = RuntimeRef.from_dict(payload["ref"])
        return backend._start(ref, payload.get("connection")).to_dict()
    if payload["action"] == "connect":
        return backend._connect(payload["record"]).to_dict()
    instance = replace(Instance.from_dict(payload["instance"]), host=None)
    if payload["action"] == "screenshot":
        import base64

        return base64.b64encode(backend.screenshot(instance)).decode()
    if payload["action"] == "save":
        return backend._save(instance).to_dict()
    if payload["action"] == "close":
        backend._close(instance)
        return None
    raise ValueError(f"Unknown remote action: {payload['action']}")


def main():
    try:
        result = dispatch(json.load(sys.stdin))
        response = {"result": result}
    except Exception as error:
        response = {"error": {"type": type(error).__name__, "message": str(error)}}
    print(json.dumps(response, allow_nan=False))
