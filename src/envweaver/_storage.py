"""Local and SSH file transfers and environment record storage."""

import base64
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

# The SSH worker receives _files.py before this module; it needs only Python's stdlib.
if __package__:
    from ._files import pack, unpack
    from ._paths import _location, normalize_address


def _operate(payload):
    if payload["action"] == "read":
        kind, body = pack(payload["root"])
        return {
            "kind": kind,
            "body": base64.b64encode(body).decode(),
            "mode": Path(payload["root"]).stat().st_mode & 0o777,
        }
    if payload["action"] == "write":
        unpack(
            payload["kind"],
            base64.b64decode(payload["body"]),
            payload["root"],
            mode=payload["mode"],
        )
        return
    relative = PurePosixPath(payload["directory"])
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("Record directory must be relative to root")
    target = Path(payload["root"]) / str(relative)
    if payload["action"] == "remove":
        if target.exists():
            shutil.rmtree(target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".record-", dir=target.parent) as temporary:
        stage = Path(temporary) / "record"
        stage.mkdir()
        for name, value in payload["records"].items():
            if Path(name).name != name:
                raise ValueError("Record name must be a filename")
            path = stage / name
            path.write_text(
                json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8"
            )
            os.chmod(path, 0o600 if payload["private"] else 0o644)
        stage.rename(target)


def _dispatch(root, payload, *, ssh_options=None):
    root = normalize_address(root)
    if isinstance(root, Path):
        return _operate({**payload, "root": str(root)})
    else:
        from ._ssh_transport import ssh

        host, path = _location(root)
        source = (
            Path(__file__).with_name("_files.py").read_text() + "\n" + Path(__file__).read_text()
        )
        output = ssh(
            host,
            ["python3", "-c", source],
            {**payload, "root": path},
            **({"ssh_options": ssh_options} if ssh_options else {}),
        )
        return json.loads(output)


def write_records(root, directory, records, *, private=False, ssh_options=None):
    _dispatch(
        root,
        {"action": "records", "directory": directory, "records": records, "private": private},
        ssh_options=ssh_options,
    )


def remove_records(root, directory, *, ssh_options=None):
    _dispatch(root, {"action": "remove", "directory": directory}, ssh_options=ssh_options)


@contextmanager
def local_source(source, *, ssh_options=None):
    """Yield a local path, downloading SSH input for the duration of the operation."""
    address = normalize_address(source)
    if isinstance(address, Path):
        yield address
        return
    _, path = _location(address)
    with tempfile.TemporaryDirectory(prefix="envweaver-files-") as temporary:
        name = PurePosixPath(path).name
        target = Path(temporary) / (name if name not in {"", ".", ".."} else "data")
        result = _dispatch(address, {"action": "read"}, ssh_options=ssh_options)
        unpack(result["kind"], base64.b64decode(result["body"]), target, mode=result["mode"])
        yield target


@contextmanager
def local_target(target, *, ssh_options=None):
    """Yield a local destination; copy to SSH only after the caller succeeds."""
    address = normalize_address(target)
    if isinstance(address, Path):
        yield address
        return
    with tempfile.TemporaryDirectory(prefix="envweaver-files-") as temporary:
        source = Path(temporary) / "data"
        yield source
        kind, body = pack(source)
        _dispatch(
            address,
            {
                "action": "write",
                "kind": kind,
                "body": base64.b64encode(body).decode(),
                "mode": source.stat().st_mode & 0o777,
            },
            ssh_options=ssh_options,
        )


if __name__ == "__main__":
    print(json.dumps(_operate(json.load(sys.stdin))))
