"""Read native snapshots inside the sandbox; only stdlib is available here."""

import base64
import hashlib
import json
import os
import sys
from pathlib import Path


def read_file(path, previous):
    with path.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        stamp = [stat.st_mtime_ns, stat.st_size, stat.st_dev, stat.st_ino]
        if previous.get("stat") == stamp:
            return None
        start = 0
        digest = hashlib.sha256()
        if (
            path.suffix == ".jsonl" and previous
            and previous["stat"][2:] == stamp[2:]
            and stat.st_size > previous["stat"][1]
        ):
            # Growing files can also have been rewritten in place. Verify the
            # committed prefix without transmitting or retaining its contents.
            remaining = previous["offset"]
            while remaining:
                chunk = stream.read(min(remaining, 1024 * 1024))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            if not remaining and digest.hexdigest() == previous["digest"]:
                start = previous["offset"]
            else:
                digest = hashlib.sha256()
        stream.seek(start)
        data = stream.read(stat.st_size - start)
    boundary = data.rfind(b"\n") + 1 if path.suffix == ".jsonl" else len(data)
    digest.update(data[:boundary])
    return {
        "stamp": {"stat": stamp, "offset": start + boundary, "digest": digest.hexdigest()},
        "start": start,
        "data": base64.b64encode(data).decode(),
    }


def scan(sources, previous):
    result = {}
    for label, folder in sources.items():
        root = Path(folder)
        directory = root.is_dir()
        for path in root.rglob("*") if directory else [root]:
            if path.is_symlink() or not path.is_file() or path.suffix not in (".json", ".jsonl"):
                continue
            name = label + "/" + (str(path.relative_to(root)) if directory else path.name)
            try:
                item = read_file(path, previous.get(name, {}))
            except FileNotFoundError:
                continue
            if item is not None:
                result[name] = item
    return result


if __name__ == "__main__":
    print(json.dumps(scan(*json.loads(sys.argv[1]))))
