"""Local working directories with incremental publication to SSH destinations."""

import base64
import shutil
import tempfile
import threading
import weakref
from contextlib import nullcontext
from pathlib import Path

from .paths import join_address, normalize_address
from .storage import _dispatch


class OutputDirectory:
    def __init__(self, address, *, exist_ok=False, sentinel=None, source_lock=None):
        self.address = normalize_address(address)
        if isinstance(self.address, Path):
            self.path = self.address.resolve()
            if sentinel and (self.path / sentinel).exists():
                raise FileExistsError(self.path / sentinel)
            self.path.mkdir(parents=True, exist_ok=exist_ok)
        else:
            self._lock = threading.RLock()
            self._source_lock = source_lock or nullcontext()
            self._published = {}
            created = _dispatch(
                self.address,
                {"action": "mkdir", "exist_ok": exist_ok, "sentinel": sentinel},
            )
            if not created:
                raise FileExistsError(self.address)
            self.path = Path(tempfile.mkdtemp(prefix="envweaver-output-"))
            self._cleanup = weakref.finalize(self, shutil.rmtree, self.path, True)

    def sync(self):
        if isinstance(self.address, Path):
            return
        with self._lock:
            try:
                # Capture a consistent snapshot, then release the Agent state lock
                # before network I/O so stop() can still meet its deadline.
                updates = []
                with self._source_lock:
                    files = sorted(
                        self.path.rglob("*"),
                        key=lambda path: (
                            path.name in {"result.json", "results.json"},
                            path.name == "trajectory.json",
                            str(path),
                        ),
                    )
                    for path in files:
                        if path.is_symlink():
                            raise ValueError("Output files must not be symbolic links")
                        if not path.is_file():
                            continue
                        relative = path.relative_to(self.path).as_posix()
                        stat = path.stat()
                        signature = (stat.st_mtime_ns, stat.st_size, stat.st_mode)
                        if self._published.get(relative) != signature:
                            updates.append((relative, signature, path.read_bytes(), stat.st_mode & 0o777))
                # Publish referenced files before the snapshot which advertises them.
                for relative, signature, body, mode in updates:
                    _dispatch(
                        join_address(self.address, relative),
                        {
                            "action": "publish",
                            "body": base64.b64encode(body).decode(),
                            "mode": mode,
                        },
                    )
                    self._published[relative] = signature
            except BaseException as error:
                # Keep recoverable local artifacts if the remote destination fails.
                self._cleanup.detach()
                error.add_note(f"Unsynchronized output retained at {self.path}")
                raise
