"""Sandbox end of the preparation pipe: announce requests, receive private files."""

import json
import re
import sys
import threading
from pathlib import Path


def main(root):
    stopped = threading.Event()

    def announce():
        pending = set()
        while not stopped.is_set():
            for path in root.glob("*/request.json"):
                identity = path.parent.name
                if identity in pending or (path.parent / "response.json").exists():
                    continue
                try:
                    request = json.loads(path.read_text())
                except FileNotFoundError:
                    continue
                print(json.dumps({**request, "id": identity}), flush=True)
                pending.add(identity)
            pending.intersection_update(p.name for p in root.iterdir())
            stopped.wait(0.01)

    watcher = threading.Thread(target=announce, daemon=True)
    watcher.start()
    incoming = sys.stdin.buffer
    try:
        while line := incoming.readline():
            response = json.loads(line)
            identity = response.pop("id")
            if not re.fullmatch(r"[0-9a-f]{32}", identity):
                raise ValueError("Invalid preparation download identity")
            remaining = response.pop("size", 0)
            directory = root / identity
            if "error" not in response and not response.get("native"):
                temporary = directory / "body.tmp"
                with temporary.open("wb") as target:
                    while remaining:
                        chunk = incoming.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise EOFError("Preparation download was interrupted")
                        target.write(chunk)
                        remaining -= len(chunk)
                temporary.replace(directory / "body")
            temporary = directory / "response.tmp"
            temporary.write_text(json.dumps(response))
            temporary.replace(directory / "response.json")
    finally:
        stopped.set()
        watcher.join()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
