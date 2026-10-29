"""Serve installation download requests while a supervised prepare command runs."""

import json
import os
import re
import select
import tempfile
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

from ...cache import DownloadCache


class PreparationDownloads:
    def __init__(self, *, runtime, instance, directory, env, cancelled, redact, helpers):
        self.runtime = runtime
        self.instance = instance
        self.directory = directory
        self.python = instance.data.get("python", "python3")
        self.cancelled = cancelled
        self.redact = redact
        self.proxies = {
            scheme: env.get(scheme + "_proxy", env.get(scheme.upper() + "_PROXY"))
            for scheme in ("http", "https")
        }
        self.proxies = {key: value for key, value in self.proxies.items() if value}
        self.cache = DownloadCache()
        self._pipe = None
        self._pipe_context = None
        self._thread = None
        self._error = None
        self._cancelled = threading.Event()
        self._write_lock = threading.Lock()
        self.helpers = (
            "_download_client.py",
            "_download_pipe.py",
            *helpers,
        )

    @property
    def streaming(self):
        return self._pipe is not None

    def setup(self):
        for name in self.helpers:
            self.runtime.upload(
                instance=self.instance,
                source=Path(__file__).with_name(name),
                target=str(PurePosixPath(self.directory).parent / name),
            )
        code, _, error = self.runtime.exec(
            instance=self.instance,
            command=[self.python, "-c", "import os,sys; os.mkdir(sys.argv[1])", self.directory],
            timeout=30,
        )
        if code:
            raise OSError(error)
        self._pipe_context = self.runtime._open_pipe(
            self.instance,
            [self.python, str(PurePosixPath(self.directory).parent / "_download_pipe.py"), self.directory],
        )
        if self._pipe_context is not None:
            self._pipe = self._pipe_context.__enter__()
            os.set_blocking(self._pipe.stdin.fileno(), False)
            self._thread = threading.Thread(target=self._receive, daemon=True)
            self._thread.start()

    def _fetch(self, request, target):
        try:
            self.cache._download(
                url=request["url"], checksum=request["checksum"], target=target,
                proxies=self.proxies or None,
                cancelled=self._cancelled if self._pipe else self.cancelled,
            )
            return {}
        except InterruptedError:
            raise
        except urllib.error.HTTPError as error:
            return {"native": True} if error.code in {401, 403} else {"error": self.redact(str(error))}
        except (OSError, ValueError) as error:
            return {"error": self.redact(str(error))}

    def _write(self, data):
        remaining = memoryview(data)
        while remaining:
            if self._cancelled.is_set():
                raise InterruptedError("Preparation download cancelled")
            if select.select([], [self._pipe.stdin], [], 0.1)[1]:
                try:
                    count = os.write(self._pipe.stdin.fileno(), remaining)
                    remaining = remaining[count:]
                except BlockingIOError:
                    pass

    def _respond(self, request):
        try:
            with tempfile.TemporaryDirectory(prefix="envweaver-download-") as folder:
                target = Path(folder) / "body"
                result = self._fetch(request, target)
                size = target.stat().st_size if target.exists() else 0
                with self._write_lock:
                    self._write(json.dumps({"id": request["id"], "size": size, **result}).encode() + b"\n")
                    if size:
                        with target.open("rb") as source:
                            while chunk := source.read(1024 * 1024):
                                self._write(chunk)
        except Exception as error:
            if not self._cancelled.is_set():
                self._error = error

    def _receive(self):
        try:
            with ThreadPoolExecutor(max_workers=8) as workers:
                while line := self._pipe.stdout.readline():
                    request = json.loads(line)
                    if not re.fullmatch(r"[0-9a-f]{32}", request["id"]):
                        raise ValueError("Invalid preparation download identity")
                    workers.submit(self._respond, request)
            if not self._cancelled.is_set():
                raise OSError("Preparation download pipe closed unexpectedly")
        except Exception as error:
            if not self._cancelled.is_set():
                self._error = error

    def serve(self):
        if self.cancelled.is_set():
            self._cancelled.set()
            if self._pipe is not None:
                return False
        if self._pipe is not None:
            if self._error is not None:
                raise ConnectionError("Preparation download pipe failed") from self._error
            return False

        script = (
            "import json,pathlib,sys\nroot=pathlib.Path(sys.argv[1]); requests={}\n"
            "for p in root.glob('*/request.json'):\n"
            " try:\n"
            "  if not (p.parent/'response.json').exists():\n"
            "   requests[p.parent.name]=json.loads(p.read_text())\n"
            " except FileNotFoundError: pass\n"
            "print(json.dumps(requests))"
        )
        code, output, error = self.runtime.exec(
            instance=self.instance,
            command=[self.python, "-c", script, self.directory],
            timeout=30,
        )
        if code:
            raise OSError(error)
        requests = json.loads(output)
        if not requests:
            return False
        if any(not re.fullmatch(r"[0-9a-f]{32}", identity) for identity in requests):
            raise ValueError("Invalid preparation download identity")
        with tempfile.TemporaryDirectory(prefix="envweaver-download-") as folder:
            def fetch(item):
                identity, request = item
                response_dir = Path(folder) / identity
                response_dir.mkdir()
                result = self._fetch(request, response_dir / "body")
                (response_dir / "response.tmp").write_text(json.dumps(result))

            with ThreadPoolExecutor(max_workers=min(8, len(requests))) as workers:
                for _ in workers.map(fetch, requests.items()):
                    pass
            delivery = self.directory + "/delivery"
            self.runtime.upload(instance=self.instance, source=folder, target=delivery)
            # Publish only after every body in this batch has arrived.
            code, _, error = self.runtime.exec(
                instance=self.instance,
                command=[
                    self.python, "-c",
                    "import pathlib,shutil,sys\nroot=pathlib.Path(sys.argv[1])\n"
                    "delivery=root/'delivery'\n"
                    "for name in sys.argv[2:]:\n"
                    " source=delivery/name; target=root/name\n"
                    " if (source/'body').exists(): (source/'body').replace(target/'body')\n"
                    " (source/'response.tmp').replace(target/'response.json')\n"
                    "shutil.rmtree(delivery)",
                    self.directory, *requests,
                ],
                timeout=30,
            )
            if code:
                raise OSError(error)
        return True

    def close(self):
        self._cancelled.set()
        if self._pipe is not None:
            with self._write_lock:
                self._pipe_context.__exit__(None, None, None)
            if self._thread is not None and self._thread.ident is not None:
                self._thread.join(timeout=35)
                if self._thread.is_alive():
                    raise TimeoutError("Preparation download workers did not stop")
        code, _, error = self.runtime.exec(
            instance=self.instance,
            command=[
                self.python,
                "-c",
                "import pathlib,shutil,sys; shutil.rmtree(sys.argv[1],ignore_errors=True); "
                "root=pathlib.Path(sys.argv[1]).parent\n"
                "for name in sys.argv[2:]:\n"
                " (root/name).unlink(missing_ok=True)\n"
                " for p in root.glob('__pycache__/'+pathlib.Path(name).stem+'.*.pyc'): p.unlink()",
                self.directory,
                *self.helpers,
            ],
            timeout=30,
        )
        if code:
            raise OSError(error)
