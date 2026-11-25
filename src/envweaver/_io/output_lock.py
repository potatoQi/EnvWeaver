"""A process-owned file lock; closing stdin also releases remote locks."""

import select
import sys
from contextlib import contextmanager
from pathlib import Path

from . import ssh_transport as _ssh_transport
from .paths import _location, normalize_address
from .process import open_pipe

_LOCK = """
import fcntl, sys
with open(sys.argv[1], "a") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if sys.argv[2] == "wait" else fcntl.LOCK_NB))
    except BlockingIOError:
        print("busy", flush=True)
    else:
        print("locked", flush=True)
        sys.stdin.buffer.read()
"""


@contextmanager
def output_lock(address, *, wait=False):
    address = normalize_address(address)
    mode = "wait" if wait else "fail"
    if isinstance(address, Path):
        command = [sys.executable, "-c", _LOCK, str(address), mode]
    else:
        host, path = _location(address)
        command = _ssh_transport.command_args(host, ["python3", "-c", _LOCK, path, mode])
    with open_pipe(command) as process:
        if not select.select([process.stdout], [], [], 30)[0]:
            raise TimeoutError("Timed out acquiring Eval output lock")
        response = process.stdout.readline()
        if response == b"busy\n":
            raise FileExistsError("Another eval is writing to this output directory")
        if response != b"locked\n":
            raise OSError("Cannot lock Eval output: " + process.stderr.read().decode(errors="replace"))
        yield process
