"""Private preparation IPC. This module never knows the controller cache location."""

import json
import os
import shutil
import time
import uuid
from pathlib import Path


def download(url, checksum, target, *, cancelled=None):
    return _request({"url": url, "checksum": checksum}, target, cancelled=cancelled)


def python_catalog(version, target, *, cancelled=None):
    return _request({"python_catalog": version}, target, cancelled=cancelled)


def _request(request, target, *, cancelled=None):
    root = os.environ.get("ENVWEAVER_DOWNLOAD_REQUESTS")
    if not root:
        return False
    directory = Path(root) / uuid.uuid4().hex
    directory.mkdir()
    temporary = directory / "request.tmp"
    temporary.write_text(json.dumps(request))
    temporary.rename(directory / "request.json")
    try:
        deadline = time.monotonic() + 3600
        while not (directory / "response.json").exists():
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("Preparation download cancelled")
            if not directory.is_dir():
                raise InterruptedError("Preparation download service stopped")
            if time.monotonic() > deadline:
                raise TimeoutError("Preparation download timed out")
            time.sleep(0.05)
        result = json.loads((directory / "response.json").read_text())
        if result.get("native"):
            return False
        if result.get("error"):
            raise OSError(result["error"])
        shutil.copyfile(directory / "body", target)
        return True
    finally:
        shutil.rmtree(directory, ignore_errors=True)
