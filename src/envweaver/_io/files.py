import io
import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


def _write_file(target, source):
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".envweaver-transfer-", dir=target.parent) as directory:
        temporary = Path(directory) / "file"
        with temporary.open("wb") as output:
            shutil.copyfileobj(source, output)
        if target.is_file():
            temporary.chmod(target.stat().st_mode)
        os.replace(temporary, target)


def pack(path):
    path = Path(path).resolve(strict=True)
    if path.is_file():
        return "file", path.read_bytes()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in sorted(path.rglob("*")):
            if entry.is_symlink():
                raise ValueError(f"File transfer does not follow symlinks: {entry}")
            archive.write(entry, str(entry.relative_to(path)))
    return "directory", buffer.getvalue()


def unpack(kind, payload, target, *, mode=None):
    target = Path(target)
    permissions = []
    if kind == "file":
        _write_file(target, io.BytesIO(payload))
    elif kind == "directory":
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for item in archive.infolist():
                relative = PurePosixPath(item.filename)
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or "\\" in item.filename
                    or ":" in item.filename
                ):
                    raise ValueError("Unsafe archive path")
                destination = target.joinpath(*relative.parts)
                if not destination.resolve().is_relative_to(target.resolve()):
                    raise ValueError("Archive escapes destination")
                if item.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    with archive.open(item) as source:
                        _write_file(destination, source)
                if mode is not None:
                    permissions.append((destination, (item.external_attr >> 16) & 0o777))
    else:
        raise ValueError(f"Unknown file transfer kind: {kind}")
    if mode is not None:
        # Set directory modes last so read-only inputs can still be populated.
        for path, permission in reversed(permissions):
            path.chmod(permission)
        target.chmod(mode & 0o777)
