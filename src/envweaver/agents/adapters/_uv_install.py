"""Cache uv-selected Python builds and wheels without changing native resolution."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


def read_catalog(uv):
    custom = os.environ.get("UV_PYTHON_DOWNLOADS_JSON_URL")
    if custom:
        with urllib.request.urlopen(custom, timeout=15) as response:
            return json.load(response)
    version = subprocess.check_output([*uv, "--version"], text=True).split()[1]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        return {}
    url = (
        f"https://raw.githubusercontent.com/astral-sh/uv/{version}"
        "/crates/uv-python-managed/download-metadata.json"
    )
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.load(response)


def canonical_url(url):
    return unquote(url.replace(
        "https://releases.astral.sh/github/python-build-standalone/",
        "https://github.com/astral-sh/python-build-standalone/",
    ))


@contextmanager
def python_downloads(uv, request):
    """Yield subprocess environment overrides, without downloading unused builds."""
    found = subprocess.run([*uv, "python", "find", "--no-python-downloads", request],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if found.returncode == 0:
        yield {}
        return
    candidates = json.loads(subprocess.check_output(
        [*uv, "python", "list", request, "--only-downloads", "--output-format", "json"],
        text=True,
    ))
    candidates = [item for item in candidates if item["implementation"] == "cpython"]
    bases = {item["url"].rsplit("/", 2)[0] for item in candidates}
    if (len(bases) != 1 or any(urlsplit(item["url"]).scheme not in {"http", "https"}
                             or urlsplit(item["url"]).username is not None
                             for item in candidates)):
        yield {}
        return
    base = bases.pop()
    selected = {unquote(item["url"][len(base):]): item for item in candidates}
    catalog = None
    cancelled = threading.Event()
    with tempfile.TemporaryDirectory(prefix="envweaver-python-") as directory:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                nonlocal catalog
                self.connection.settimeout(5)
                path = unquote(urlsplit(self.path).path)
                item = selected.get(path)
                original = item["url"] if item else base + self.path
                try:
                    if item and catalog is None:
                        try:
                            catalog = read_catalog(uv)
                            if not isinstance(catalog, dict):
                                catalog = {}
                        except (OSError, ValueError):
                            # Missing metadata never changes uv's native download behavior.
                            catalog = {}
                    entry = (catalog or {}).get(item["key"], {}) if item else {}
                    digest = entry.get("sha256") or ""
                    if (re.fullmatch(r"[0-9a-fA-F]{64}", digest)
                            and canonical_url(entry.get("url", "")) == canonical_url(original)):
                        from _download_client import download

                        target = Path(directory) / "python.tar"
                        if download(original, "sha256:" + digest, target, cancelled=cancelled):
                            self.send_response(200)
                            self.send_header("Content-Length", str(target.stat().st_size))
                            self.end_headers()
                            with target.open("rb") as file:
                                shutil.copyfileobj(file, self.wfile, 1024 * 1024)
                            target.unlink()
                            return
                    self.send_response(307)
                    self.send_header("Location", original)
                    self.end_headers()
                except (OSError, ValueError):
                    if not cancelled.is_set():
                        self.send_error(502, "Python download failed")

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            bypass = os.environ.get("no_proxy", os.environ.get("NO_PROXY", ""))
            yield {"UV_PYTHON_INSTALL_MIRROR": f"http://127.0.0.1:{server.server_port}",
                   "no_proxy": bypass + ",127.0.0.1", "NO_PROXY": bypass + ",127.0.0.1"}
        finally:
            cancelled.set()
            server.shutdown()
            server.server_close()
            thread.join()


def cache_wheels(lock):
    """Replace only unambiguously selected uv wheel URLs with private cached copies."""
    import tomllib

    from _download_client import download

    lock = Path(lock)
    text = lock.read_text()

    def materialize(package):
        wheels = package.get("wheels", [])
        # Do not guess between multiple candidates or prefetch source-build inputs.
        if len(wheels) != 1:
            return None
        wheel = wheels[0]
        url = wheel.get("url")
        hashes = wheel.get("hashes", {})
        algorithm = next((name for name in ("sha256", "sha512") if name in hashes), None)
        if not url or not algorithm or urlsplit(url).username is not None:
            return None
        filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        if Path(filename).name != filename or not filename.endswith(".whl"):
            raise ValueError("Invalid uv wheel filename")
        target = lock.parent / package["name"] / filename
        target.parent.mkdir()
        if download(url, f"{algorithm}:{hashes[algorithm]}", target):
            original = "url = " + json.dumps(url, ensure_ascii=False)
            return original, "path = " + json.dumps(str(target))
        return None

    with ThreadPoolExecutor(max_workers=8) as workers:
        for replacement in workers.map(materialize, tomllib.loads(text)["packages"]):
            if replacement is not None:
                if replacement[0] not in text:
                    raise ValueError("Unexpected uv lock URL encoding")
                text = text.replace(*replacement)
    lock.write_text(text)
