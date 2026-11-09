import hashlib
import json
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from envweaver import Instance
from envweaver.agents.adapters._downloads import PreparationDownloads
from tests.agents.test_cli_agents import LocalTransport


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVWEAVER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")

    class Transport(LocalTransport):
        uploads = 0

        def upload(self, instance, source, target):
            self.uploads += 1
            super().upload(instance, source, target)

    remote = tmp_path / "sandbox"
    remote.mkdir()
    downloads = PreparationDownloads(
        runtime=Transport(), instance=Instance("local", "fake", data={"python": sys.executable}),
        directory=str(remote / "downloads"), env={}, cancelled=threading.Event(),
        redact=lambda text: text, helpers=("_pip_install.py",),
    )
    Path(downloads.directory).mkdir()
    return downloads


def request(bridge, number, url, checksum):
    directory = Path(bridge.directory) / f"{number:032x}"
    directory.mkdir()
    (directory / "request.json").write_text(json.dumps({"url": url, "checksum": checksum}))
    return directory


def test_parallel_batch_deduplicates_and_preserves_individual_errors(bridge):
    body = b"original artifact"
    checksum = "sha256:" + hashlib.sha256(body).hexdigest()
    lock = threading.Lock()
    counts = Counter()
    active = peak = 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal active, peak
            with lock:
                counts[self.path] += 1
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.05)
                self.send_response(401 if self.path == "/private" else 200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                with lock:
                    active -= 1

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        directories = [request(bridge, n, f"{url}/{n}", checksum) for n in range(16)]
        duplicate = request(bridge, 16, f"{url}/0", checksum)
        private = request(bridge, 17, f"{url}/private", checksum)
        corrupt = request(bridge, 18, f"{url}/corrupt", "sha256:" + "0" * 64)
        assert bridge.serve()
        assert 2 <= peak <= 8
        assert counts["/0"] == 1
        assert bridge.runtime.uploads == 1
        for directory in [*directories, duplicate]:
            assert (directory / "body").read_bytes() == body
            assert json.loads((directory / "response.json").read_text()) == {}
        assert json.loads((private / "response.json").read_text()) == {"native": True}
        assert "checksum mismatch" in json.loads((corrupt / "response.json").read_text())["error"]
        assert not (private / "body").exists()
        assert not (corrupt / "body").exists()
        assert not (Path(bridge.directory) / "delivery").exists()
        assert not bridge.serve()
        before = counts.copy()
        (directories[0] / "body").write_bytes(b"task modification")
        warm = request(bridge, 19, f"{url}/0", checksum)
        assert bridge.serve()
        assert counts == before
        assert (warm / "body").read_bytes() == body
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_cancelled_batch_does_not_publish_or_upload(bridge):
    directories = [
        request(bridge, n, f"http://127.0.0.1/{n}", "sha256:" + "0" * 64)
        for n in range(12)
    ]
    bridge.cancelled.set()
    with pytest.raises(InterruptedError):
        bridge.serve()
    assert bridge.runtime.uploads == 0
    assert all(not (directory / "response.json").exists() for directory in directories)


@pytest.mark.parametrize("body", [b"", b"a\n\x00b" * 400000], ids=["empty", "binary"] )
def test_pipe_streams_concurrent_files_without_runtime_calls(bridge, monkeypatch, tmp_path, body):
    from envweaver.agents.adapters._download_client import download

    Path(bridge.directory).rmdir()
    bridge.setup()
    monkeypatch.setenv("ENVWEAVER_DOWNLOAD_REQUESTS", bridge.directory)

    def fetch(request, target):
        target.write_bytes(body)
        return {}

    monkeypatch.setattr(bridge, "_fetch", fetch)
    uploads = bridge.runtime.uploads
    monkeypatch.setattr(bridge.runtime, "exec", lambda **kwargs: pytest.fail("unexpected exec"))
    try:
        with ThreadPoolExecutor(max_workers=8) as workers:
            def receive(number):
                target = tmp_path / f"file-{number}"
                assert download("https://example.org/wheel", "sha256:" + "0" * 64, target)
                assert target.read_bytes() == body

            list(workers.map(receive, range(24)))
        assert bridge.runtime.uploads == uploads
        assert bridge._error is None
    finally:
        monkeypatch.undo()
        bridge.close()
    assert bridge._pipe.poll() == 0
    assert not bridge._thread.is_alive()
    assert not Path(bridge.directory).exists()


def test_pipe_failure_is_reported_and_workers_stop(bridge):
    Path(bridge.directory).rmdir()
    bridge.setup()
    bridge._pipe.kill()
    bridge._pipe.wait()
    deadline = time.monotonic() + 5
    try:
        while bridge._error is None and time.monotonic() < deadline:
            time.sleep(0.01)
        with pytest.raises(ConnectionError, match="pipe failed"):
            bridge.serve()
    finally:
        bridge.close()
    assert not bridge._thread.is_alive()


def test_pipe_cancellation_interrupts_active_download(bridge, monkeypatch):
    started = threading.Event()

    def download(**kwargs):
        started.set()
        assert kwargs["cancelled"].wait(5)
        raise InterruptedError("cancelled")

    monkeypatch.setattr(bridge.cache, "_download", download)
    Path(bridge.directory).rmdir()
    bridge.setup()
    request(bridge, 1, "https://example.org/wheel", "sha256:" + "0" * 64)
    try:
        assert started.wait(5)
        bridge.cancelled.set()
        assert not bridge.serve()
    finally:
        bridge.close()
    assert not bridge._thread.is_alive()


def test_helper_cleanup_preserves_unrelated_files(bridge):
    root = Path(bridge.directory).parent
    extra = root / '_uv_install.py'
    extra.write_text('helper')
    bytecode = root / '__pycache__'
    bytecode.mkdir()
    (bytecode / '_uv_install.cpython-312.pyc').write_bytes(b'helper')
    (bytecode / '_download_client.cpython-312.pyc').write_bytes(b'helper')
    (bytecode / 'task_service.cpython-312.pyc').write_bytes(b'keep')
    bridge.helpers += ('_uv_install.py',)
    bridge.close()
    assert not extra.exists()
    assert sorted(p.name for p in bytecode.iterdir()) == ['task_service.cpython-312.pyc']


def test_python_catalog_requests_use_versioned_cache_through_pipe(bridge, monkeypatch, tmp_path):
    from envweaver.agents.adapters._download_client import python_catalog

    calls = []
    body = json.dumps({'build': {'url': 'https://example.org/python', 'sha256': 'a' * 64}}).encode()
    def retrieve(url, path, *a, **kw):
        calls.append(url)
        path.write_bytes(body)
    monkeypatch.setattr(bridge.cache, '_retrieve', retrieve)
    Path(bridge.directory).rmdir()
    bridge.setup()
    monkeypatch.setenv('ENVWEAVER_DOWNLOAD_REQUESTS', bridge.directory)
    try:
        with ThreadPoolExecutor(max_workers=24) as workers:
            def receive(number):
                target = tmp_path / f'catalog-{number}'
                assert python_catalog('0.13.7', target)
                assert target.read_bytes() == body
            list(workers.map(receive, range(24)))
        assert calls == ['https://raw.githubusercontent.com/astral-sh/uv/0.13.7/crates/uv-python-managed/download-metadata.json']
        assert bridge._error is None
        with pytest.raises(OSError, match='Invalid uv version'):
            python_catalog('../main', tmp_path / 'invalid')
    finally:
        bridge.close()
    assert not bridge._thread.is_alive()


def test_catalog_can_include_builds_without_upstream_checksum():
    from envweaver.agents.adapters._downloads import _validate_python_catalog
    _validate_python_catalog({'unhashed': {'url': 'https://example.org/build', 'sha256': None},
                              'hashed': {'url': 'https://example.org/other', 'sha256': 'a' * 64}})
