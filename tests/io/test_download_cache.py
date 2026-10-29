import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from envweaver import DownloadCache


@pytest.fixture
def origin():
    class Handler(BaseHTTPRequestHandler):
        body = b"original artifact" * 100
        requests = 0
        truncated = False
        ranges = []
        user_agents = []
        resume = False

        def do_GET(self):
            type(self).requests += 1
            type(self).user_agents.append(self.headers.get("User-Agent"))
            offset = 0
            if self.resume and self.headers.get("Range"):
                type(self).ranges.append(self.headers["Range"])
                offset = int(self.headers["Range"].removeprefix("bytes=").removesuffix("-"))
            self.send_response(206 if offset else 200)
            if offset:
                self.send_header(
                    "Content-Range", f"bytes {offset}-{len(self.body) - 1}/{len(self.body)}"
                )
            self.send_header("Content-Length", str(len(self.body) - offset))
            self.end_headers()
            if self.truncated and self.requests == 1:
                self.wfile.write(self.body[:10])
            else:
                self.wfile.write(self.body[offset:])

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/artifact", Handler
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def checksum(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


def test_concurrent_downloads_share_one_fetch_but_not_writable_files(tmp_path, origin):
    url, handler = origin

    def download(number):
        DownloadCache(tmp_path / "cache").download(
            url=url,
            checksum=checksum(handler.body),
            target=tmp_path / str(number),
            proxies={},
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(download, range(24)))
    assert handler.requests == 1
    assert handler.user_agents == ["envweaver"]
    (tmp_path / "0").write_bytes(b"task-specific patched source")
    download(24)
    assert (tmp_path / "24").read_bytes() == handler.body
    assert (tmp_path / "1").read_bytes() == handler.body
    assert handler.requests == 1


def test_partial_download_retries_and_never_publishes_partial_data(tmp_path, origin):
    url, handler = origin
    handler.truncated = True
    DownloadCache(tmp_path / "cache").download(
        url=url,
        checksum=checksum(handler.body),
        target=tmp_path / "output",
        proxies={},
    )
    assert handler.requests == 2
    assert (tmp_path / "output").read_bytes() == handler.body


def test_corrupt_cache_is_downloaded_again(tmp_path, origin):
    url, handler = origin
    cache = DownloadCache(tmp_path / "cache")
    arguments = dict(
        url=url, checksum=checksum(handler.body), target=tmp_path / "output", proxies={}
    )
    cache.download(**arguments)
    next(cache.root.glob("*.data")).write_bytes(b"poison")
    cache.download(**arguments)
    assert handler.requests == 2
    assert (tmp_path / "output").read_bytes() == handler.body


def test_partial_download_resumes_at_received_byte(tmp_path, origin):
    url, handler = origin
    handler.truncated = handler.resume = True
    cache = DownloadCache(tmp_path / "cache")
    cache.download(url=url, checksum=checksum(handler.body), target=tmp_path / "output", proxies={})
    assert handler.ranges == ["bytes=10-"]
    assert (tmp_path / "output").read_bytes() == handler.body
    assert not list(cache.root.glob("*.part-*"))


def test_cancellation_during_transfer_preserves_target(tmp_path, origin):
    url, handler = origin

    class Cancellation:
        calls = 0

        def is_set(self):
            self.calls += 1
            return self.calls >= 3

    target = tmp_path / "output"
    target.write_bytes(b"previous")
    cache = DownloadCache(tmp_path / "cache")
    with pytest.raises(InterruptedError):
        cache._download(
            url=url,
            checksum=checksum(handler.body),
            target=target,
            proxies={},
            cancelled=Cancellation(),
        )
    assert target.read_bytes() == b"previous"
    assert not list(cache.root.glob("*.data"))
    assert not list(cache.root.glob("*.part-*"))


def test_bad_digest_preserves_target_and_does_not_fill_cache(tmp_path, origin):
    url, handler = origin
    target = tmp_path / "output"
    target.write_bytes(b"previous result")
    cache = DownloadCache(tmp_path / "cache")
    with pytest.raises(ValueError, match="checksum mismatch"):
        cache.download(url=url, checksum="sha256:" + "0" * 64, target=target, proxies={})
    assert target.read_bytes() == b"previous result"
    assert not list(cache.root.glob("*.data"))
    assert not [p for p in cache.root.iterdir() if p.suffix != ".lock"]


def test_capacity_cleanup_preserves_delivered_copy(tmp_path, origin):
    url, handler = origin
    cache = DownloadCache(tmp_path / "cache", max_bytes=1)
    cache.download(url=url, checksum=checksum(handler.body), target=tmp_path / "output", proxies={})
    assert not list(cache.root.glob("*.data"))
    assert (tmp_path / "output").read_bytes() == handler.body


def test_cancelled_request_does_not_download(tmp_path, origin):
    url, handler = origin
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(InterruptedError):
        DownloadCache(tmp_path / "cache")._download(
            url=url,
            checksum=checksum(handler.body),
            target=tmp_path / "output",
            cancelled=stopped,
        )
    assert handler.requests == 0


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:secret@example.com/x",
        "https://:secret@example.com/x",
    ],
)
def test_rejects_non_http_and_credentials(tmp_path, url):
    with pytest.raises(ValueError):
        DownloadCache(tmp_path).download(
            url=url, checksum="sha256:" + "0" * 64, target=tmp_path / "x"
        )
