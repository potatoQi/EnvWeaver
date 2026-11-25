"""Local and SSH file transfers and environment record storage."""

import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

# The SSH worker receives files.py before this module; it needs only Python's stdlib.
if __package__:
    from .files import pack, unpack
    from .paths import _location, normalize_address
    from .retry import retry


def _operate(payload):
    if payload["action"] == "exists":
        return Path(payload["root"]).exists()
    if payload["action"] == "publish_directory":
        target = Path(payload["root"])
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise FileExistsError(str(target))
        if "source" in payload:
            Path(payload["source"]).rename(target)
        else:
            with tempfile.TemporaryDirectory(prefix=".publish-", dir=target.parent) as temporary:
                stage = Path(temporary) / "new"
                unpack("directory", base64.b64decode(payload["body"]), stage, mode=0o700)
                stage.rename(target)
        return
    if payload["action"] == "read_json":
        target = Path(payload["root"])
        return json.loads(target.read_text()) if target.exists() else None
    if payload["action"] == "fingerprint":
        target = Path(payload["root"]).resolve(strict=True)
        digest = hashlib.sha256()
        entries = [target] if target.is_file() else [target, *sorted(target.rglob("*"))]
        for entry in entries:
            if entry.is_symlink():
                raise ValueError(f"File transfer does not follow symlinks: {entry}")
            content = hashlib.sha256()
            if entry.is_file():
                with entry.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        content.update(chunk)
            digest.update(json.dumps([
                str(entry.relative_to(target)), entry.stat().st_mode, content.hexdigest(),
            ]).encode())
        return {"kind": "file" if target.is_file() else "directory", "sha256": digest.hexdigest()}
    if payload["action"] == "mkdir":
        target = Path(payload["root"])
        sentinel = payload.get("sentinel")
        if sentinel and (target / sentinel).exists():
            return False
        try:
            target.mkdir(parents=True, exist_ok=payload["exist_ok"])
        except FileExistsError:
            return False
        return True
    if payload["action"] == "publish":
        target = Path(payload["root"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".envweaver-", dir=target.parent) as temporary:
            source = Path(temporary) / "file"
            source.write_bytes(base64.b64decode(payload["body"]))
            os.chmod(source, payload["mode"])
            os.replace(source, target)
        return
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


def _dispatch(root, payload):
    root = normalize_address(root)
    if isinstance(root, Path):
        return _operate({**payload, "root": str(root)})
    from .ssh_transport import ssh

    host, path = _location(root)
    source = (
        Path(__file__).with_name("files.py").read_text() + "\n" + Path(__file__).read_text()
    )
    output = retry(lambda: ssh(
        host,
        ["python3", "-c", source],
        {**payload, "root": path},
    ), replay=payload["action"] in {"exists", "read", "read_json", "fingerprint", "write", "publish", "remove"})
    return json.loads(output)


def write_records(root, directory, records, *, private=False):
    _dispatch(
        root,
        {"action": "records", "directory": directory, "records": records, "private": private},
    )


def remove_records(root, directory):
    _dispatch(root, {"action": "remove", "directory": directory})


@contextmanager
def local_source(source):
    """Yield a local path, downloading SSH input for the duration of the operation."""
    address = normalize_address(source)
    if isinstance(address, Path):
        yield address
        return
    _, path = _location(address)
    with tempfile.TemporaryDirectory(prefix="envweaver-files-") as temporary:
        name = PurePosixPath(path).name
        target = Path(temporary) / (name if name not in {"", ".", ".."} else "data")
        result = _dispatch(address, {"action": "read"})
        unpack(result["kind"], base64.b64decode(result["body"]), target, mode=result["mode"])
        yield target


@contextmanager
def local_target(target):
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
        )


if __name__ == "__main__":
    print(json.dumps(_operate(json.load(sys.stdin))))
