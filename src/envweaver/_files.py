import io
import shutil
import zipfile
from pathlib import Path, PurePosixPath


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


def unpack(kind, payload, target):
    target = Path(target)
    if kind == "file":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
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
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(item) as source, destination.open("wb") as output:
                        shutil.copyfileobj(source, output)
    else:
        raise ValueError(f"Unknown file transfer kind: {kind}")
