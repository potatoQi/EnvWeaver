"""Private, per-verification file transport. No daemon access in the verifier."""

import base64
import json
import time
import uuid
from pathlib import Path

from .._files import pack, unpack
from .._util import write_json
from ..errors import RuntimeError
from ..models import Instance


def encode_file(path):
    kind, payload = pack(path)
    return {
        "kind": kind,
        "payload": base64.b64encode(payload).decode(),
        "mode": Path(path).stat().st_mode & 0o777,
    }


def decode_file(value, path):
    unpack(
        value["kind"], base64.b64decode(value["payload"], validate=True), path, mode=value["mode"]
    )


class RelayBackend:
    def __init__(self, root=None, *, host=None, ssh=None):
        pass

    def connect(self, record):
        directory = Path(record["directory"])
        if not directory.is_dir():
            raise RuntimeError("Verification session has ended")
        return Instance(record["id"], "_verifier", data=record)

    def _request(self, instance, operation, **arguments):
        directory = Path(instance.data["directory"])
        request_id = uuid.uuid4().hex
        request = directory / (request_id + ".request")
        response = directory / (request_id + ".response")
        write_json(
            request,
            {"id": request_id, "operation": operation, "arguments": arguments},
            private=True,
        )
        deadline = time.monotonic() + instance.data["timeout"]
        try:
            while not response.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Verification session timed out")
                if not directory.is_dir():
                    raise RuntimeError("Verification session has ended")
                time.sleep(0.05)
            value = json.loads(response.read_text())
            if "error" in value:
                error_type = TimeoutError if value.get("timeout") else RuntimeError
                raise error_type(value["error"])
            return value["result"]
        finally:
            request.unlink(missing_ok=True)
            response.unlink(missing_ok=True)

    def describe(self, instance):
        return dict(instance.data)

    def exec(self, instance, command, cwd=None, timeout=300):
        return tuple(self._request(instance, "exec", command=command, cwd=cwd, timeout=timeout))

    def upload(self, instance, source, target):
        self._request(instance, "upload", file=encode_file(source), target=target)

    def download(self, instance, source, target):
        decode_file(self._request(instance, "download", source=source), target)

    def screenshot(self, instance):
        return base64.b64decode(self._request(instance, "screenshot"), validate=True)

    def close(self, instance):
        raise ValueError("The caller owns the task instance")
