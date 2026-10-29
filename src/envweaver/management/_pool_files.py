"""Filesystem operations shared by local and SSH Pool access (standard library only)."""

import base64
import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

if __package__:
    from .._io.files import unpack
    from ..trajectory.usage import token_usage


def stamp(directory):
    entries = []

    pending = [(directory, (), directory.lstat())]
    while pending:
        path, parts, info = pending.pop()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"Pool materials cannot contain symlinks: {path}")
        entries.append((parts, info))
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as children:
                pending.extend(
                    (child.path, (*parts, child.name), child.stat(follow_symlinks=False))
                    for child in children
                )

    digest = hashlib.sha256()
    for parts, info in sorted(entries, key=lambda entry: entry[0]):
        digest.update(
            json.dumps(
                [
                    "/".join(parts) or ".",
                    info.st_mode,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                ]
            ).encode()
        )
    return digest.hexdigest()


def checked_path(directory, relative):
    part = Path(relative)
    if not part.parts or part.is_absolute() or ".." in part.parts or "\\" in relative:
        raise ValueError("材料路径须位于任务目录内")
    path = directory / part
    if not path.resolve(strict=True).is_relative_to(directory.resolve(strict=True)):
        raise ValueError("材料路径越界")
    if any(p.is_symlink() for p in [path, *path.parents] if p != directory.parent):
        raise ValueError("材料不能包含符号链接")
    return path


def recover_publications(base):
    for temporary in base.glob(".publish-*"):
        journal = temporary / "intent.json"
        if not journal.is_file():
            continue
        intent = json.loads(journal.read_text())
        try:
            os.kill(intent["pid"], 0)
            continue
        except ProcessLookupError:
            pass
        name = intent["directory"]
        if Path(name).name != name or name.startswith("."):
            raise ValueError("Invalid publication journal")
        destination, backup = base / name, temporary / "old"
        if not destination.exists() and backup.exists():
            backup.rename(destination)
        shutil.rmtree(temporary)
        (base / f".{name}.lock").unlink(missing_ok=True)


def metadata_operation(root, kind, payload):
    folder = root / ".envweaver"
    target = folder / (kind + "-pool.json")
    if folder.is_symlink() or target.is_symlink():
        raise ValueError("Pool metadata cannot be a symlink")

    def read():
        body = target.read_bytes() if target.exists() else b""
        data = json.loads(body) if body else {"tags": {}, "assets": {}}
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("tags"), dict)
            or not isinstance(data.get("assets"), dict)
        ):
            raise ValueError("Invalid Pool metadata")
        return {"data": data, "stamp": hashlib.sha256(body).hexdigest()}

    if "data" not in payload:
        return read()
    folder.mkdir(exist_ok=True)
    lock = folder / (kind + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if read()["stamp"] != payload["expected"]:
            raise FileExistsError("Pool metadata changed; refresh and retry")
        fd, temporary = tempfile.mkstemp(prefix=".metadata-", dir=folder)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(payload["data"], output, ensure_ascii=False, allow_nan=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return read()


def pool_operation(payload):
    root = Path(payload["root"]).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    action = payload["action"]
    kind = payload["kind"]
    if action == "metadata":
        return metadata_operation(root, kind, payload)
    base = root / "runtimes" if kind == "runtime" else root
    recover_publications(base)
    filename = {"task": "task.json", "runtime": "runtime.json", "trajectory": "result.json"}[kind]
    if action == "asset_metadata_batch":
        return [
            pool_operation({**payload, **update, "action": "asset_metadata"})
            for update in payload["updates"]
        ]
    if action == "scan":
        known = payload.get("known", {})
        items = []
        if not base.exists():
            return items
        for directory in sorted(base.iterdir()):
            if directory.name.startswith(".") or not directory.is_dir():
                continue
            item = {"directory": directory.name}
            try:
                if directory.is_symlink():
                    raise ValueError("Pool directory cannot be a symlink")
                if not (directory / filename).is_file():
                    continue
                item["stamp"] = stamp(directory)
                if known.get(directory.name) == item["stamp"]:
                    items.append(item)
                    continue
                item["spec"] = json.loads((directory / filename).read_text())
                if kind == "trajectory":
                    agent = item["spec"].get("agent_result") or {}
                    item["token_usage"] = token_usage(directory / "agent", (agent.get("artifacts") or {}).get("trajectory"))
                if kind == "task":
                    spec = item["spec"]
                    paths = [p["path"] for p in spec.get("goal", []) if p.get("type") == "image"]
                    paths += [p["source"] for p in spec.get("context", [])]
                    paths.append(spec["verifier"]["entry"])
                    for relative in paths:
                        checked_path(directory, relative)
                if kind == "runtime":
                    meta = directory / "metadata.json"
                    item["metadata"] = json.loads(meta.read_text()) if meta.exists() else {}
                item["mtime"] = (directory / filename).stat().st_mtime
            except (OSError, ValueError, TypeError, KeyError) as error:
                item["error"] = str(error)
            items.append(item)
        return items
    name = payload["directory"]
    if not name or name.startswith(".") or Path(name).name != name or "\\" in name:
        raise ValueError("Invalid Pool directory")
    destination = base / name
    if destination.is_symlink():
        raise ValueError("Pool directory cannot be a symlink")
    if action == "files":
        return [
            {"path": str(p.relative_to(destination)), "size": p.stat().st_size}
            for p in sorted(destination.rglob("*"))
            if p.is_file()
        ]
    if action == "resolve":
        path = checked_path(destination, payload["relative"])
        if not path.is_file():
            raise FileNotFoundError(payload["relative"])
        return payload["relative"]
    base.mkdir(parents=True, exist_ok=True)
    lock = base / f".{name}.lock"
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        expected = payload.get("expected")
        if expected is None:
            if destination.exists():
                raise FileExistsError("Asset directory already exists")
        elif not destination.exists() or stamp(destination) != expected:
            raise ValueError("Pool files changed; refresh and retry")
        if action == "asset_metadata":
            manifest_path = destination / filename
            manifest = json.loads(manifest_path.read_text())
            manifest["metadata"] = payload["metadata"]
            fd, temporary = tempfile.mkstemp(prefix=".asset-metadata-", dir=base)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(manifest, output, ensure_ascii=False, allow_nan=False, indent=2)
                    output.flush()
                    os.fsync(output.fileno())
                if manifest_path.exists():
                    os.chmod(temporary, manifest_path.stat().st_mode & 0o777)
                os.replace(temporary, manifest_path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return stamp(destination)
        if action == "delete":
            shutil.rmtree(destination)
            return None
        with tempfile.TemporaryDirectory(prefix=".publish-", dir=base) as temporary:
            stage, backup = Path(temporary) / "new", Path(temporary) / "old"
            (Path(temporary) / "intent.json").write_text(
                json.dumps({"pid": os.getpid(), "directory": name})
            )
            # unpack is supplied by the caller, including on the remote server.
            if "stage" in payload:
                shutil.copytree(payload["stage"], stage)
            else:
                unpack("directory", base64.b64decode(payload["body"]), stage, mode=0o755)
            try:
                if destination.exists():
                    destination.rename(backup)
                stage.rename(destination)
            except BaseException:
                if backup.exists():
                    backup.rename(destination)
                raise
        return stamp(destination)
    finally:
        lock.unlink()
