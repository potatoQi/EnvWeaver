import json
import sys
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from envweaver.agents.adapters import _uv_install as uv


def test_uv_keeps_existing_python(monkeypatch):
    monkeypatch.setattr(uv.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(uv.subprocess, 'check_output', lambda *a, **kw: pytest.fail('No selection needed'))
    with uv.python_downloads(['uv'], '3.12') as env:
        assert env == {}


@pytest.mark.parametrize('cache_result', [True, False, 'error'])
def test_uv_only_downloads_build_requested_by_uv_and_closes_mirror(tmp_path, monkeypatch, cache_result):
    monkeypatch.setenv('ENVWEAVER_DOWNLOAD_REQUESTS', str(tmp_path))
    monkeypatch.setenv('NO_PROXY', 'example.org')
    monkeypatch.delenv('no_proxy', raising=False)
    base = 'https://github.com/astral-sh/python-build-standalone/releases/download'
    candidates = [dict(key=f'cpython-{name}', implementation='cpython', url=f'{base}/20261013/{name}.tar.gz')
                  for name in ('chosen', 'unused')]
    catalog = {item['key']: {'url': item['url'], 'sha256': 'a'*64} for item in candidates}
    monkeypatch.setattr(uv.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1))
    monkeypatch.setattr(uv.subprocess, 'check_output', lambda *a, **kw: json.dumps(candidates))
    monkeypatch.setattr(uv, 'read_catalog', lambda command, **kwargs: catalog)
    downloads = []
    def download(url, checksum, target, **kwargs):
        downloads.append((url, checksum, target))
        if cache_result == 'error':
            raise OSError('download failed')
        target.write_bytes(b'python archive')
        return cache_result
    monkeypatch.setitem(sys.modules, '_download_client', SimpleNamespace(download=download))
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with uv.python_downloads(['uv'], '3.12') as env:
        assert downloads == []
        assert 'example.org' in env['NO_PROXY']
        url = env['UV_PYTHON_INSTALL_MIRROR'] + '/20261013/chosen.tar.gz'
        if cache_result is True:
            with opener.open(url) as response:
                assert response.read() == b'python archive'
        else:
            with pytest.raises(urllib.error.HTTPError) as error:
                opener.open(url)
            assert error.value.code == (502 if cache_result == 'error' else 307)
            if cache_result is False:
                assert error.value.headers['Location'] == candidates[0]['url']
    assert len(downloads) == 1
    assert downloads[0][0] == candidates[0]['url']
    assert not downloads[0][2].parent.exists()
    with pytest.raises(urllib.error.URLError):
        opener.open(url, timeout=1)


def test_uv_catalog_source_failure_preserves_native_download(tmp_path, monkeypatch):
    monkeypatch.setenv('ENVWEAVER_DOWNLOAD_REQUESTS', str(tmp_path))
    monkeypatch.setattr(uv.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1))
    monkeypatch.setattr(uv.subprocess, 'check_output', lambda *a, **kw: json.dumps([
        dict(key='custom', implementation='cpython', url='file:///build/python.tar.gz')]))
    with uv.python_downloads(['uv'], '3.12') as env:
        assert env == {}


def test_uv_official_release_redirect_matches_catalog_url():
    path = 'releases/download/20261026/cpython-3.12.16%2B20261026-linux.tar.gz'
    assert uv.canonical_url('https://releases.astral.sh/github/python-build-standalone/' + path) == uv.canonical_url('https://github.com/astral-sh/python-build-standalone/' + path)


def test_uv_mirror_cancellation_stops_pending_download(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from http.client import RemoteDisconnected

    monkeypatch.setenv('ENVWEAVER_DOWNLOAD_REQUESTS', str(tmp_path))
    item = dict(key='cpython-test', implementation='cpython', url='https://example.org/build/python.tar.gz')
    monkeypatch.setattr(uv.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1))
    monkeypatch.setattr(uv.subprocess, 'check_output', lambda *a, **kw: json.dumps([item]))
    monkeypatch.setattr(uv, 'read_catalog', lambda command, **kwargs: {item['key']: {'url': item['url'], 'sha256': 'a'*64}})
    started, stopped = threading.Event(), threading.Event()
    folders = []
    def download(url, checksum, target, *, cancelled):
        folders.append(target.parent)
        started.set()
        assert cancelled.wait(5)
        stopped.set()
        raise InterruptedError('cancelled')
    monkeypatch.setitem(sys.modules, '_download_client', SimpleNamespace(download=download))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with ThreadPoolExecutor(1) as executor:
        with uv.python_downloads(['uv'], '3.12') as env:
            future = executor.submit(opener.open, env['UV_PYTHON_INSTALL_MIRROR'] + '/build/python.tar.gz')
            assert started.wait(5)
        assert stopped.is_set()
        with pytest.raises(RemoteDisconnected):
            future.result(timeout=2)
    assert not folders[0].exists()


def test_official_catalog_uses_controller_and_removes_private_copy(tmp_path, monkeypatch):
    monkeypatch.delenv('UV_PYTHON_DOWNLOADS_JSON_URL', raising=False)
    monkeypatch.setattr(uv.subprocess, 'check_output', lambda *a, **kw: 'uv 0.13.7')
    catalog = {'build': {'url': 'https://example.org/python', 'sha256': 'a' * 64}}
    paths = []
    def fetch(version, target, **kwargs):
        assert version == '0.13.7'
        paths.append(target)
        target.write_text(json.dumps(catalog))
        return True
    monkeypatch.setitem(sys.modules, '_download_client', SimpleNamespace(python_catalog=fetch))
    monkeypatch.setattr(uv.urllib.request, 'urlopen', lambda *a, **kw: pytest.fail('sandbox network'))
    assert uv.read_catalog(['uv']) == catalog
    assert not paths[0].parent.exists()


def test_custom_catalog_is_not_permanently_cached(monkeypatch):
    import io
    monkeypatch.setenv('UV_PYTHON_DOWNLOADS_JSON_URL', 'https://custom.example/catalog')
    seen = []
    def fetch(url, **kwargs):
        seen.append(url)
        return io.BytesIO(json.dumps({'revision': len(seen)}).encode())
    monkeypatch.setattr(uv.urllib.request, 'urlopen', fetch)
    assert uv.read_catalog(['uv']) == {'revision': 1}
    assert uv.read_catalog(['uv']) == {'revision': 2}
    assert seen == ['https://custom.example/catalog'] * 2


def test_truncated_catalog_response_does_not_crash_mirror(monkeypatch):
    import http.client
    item = dict(key='build', implementation='cpython', url='https://example.org/build/python.tar.gz')
    monkeypatch.setattr(uv.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1))
    monkeypatch.setattr(uv.subprocess, 'check_output', lambda *a, **kw: json.dumps([item]))
    def interrupted(*a, **kw):
        raise http.client.IncompleteRead(b'partial', 10)
    monkeypatch.setattr(uv, 'read_catalog', interrupted)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with uv.python_downloads(['uv'], '3.12') as env:
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(env['UV_PYTHON_INSTALL_MIRROR'] + '/build/python.tar.gz')
        assert error.value.code == 307
        assert error.value.headers['Location'] == item['url']


def test_unavailable_official_catalog_does_not_repeat_sandbox_request(monkeypatch):
    monkeypatch.delenv('UV_PYTHON_DOWNLOADS_JSON_URL', raising=False)
    monkeypatch.setattr(uv.subprocess, 'check_output', lambda *a, **kw: 'uv 0.13.7')
    monkeypatch.setitem(sys.modules, '_download_client', SimpleNamespace(python_catalog=lambda *a, **kw: False))
    monkeypatch.setattr(uv.urllib.request, 'urlopen', lambda *a, **kw: pytest.fail('duplicate catalog request'))
    assert uv.read_catalog(['uv']) == {}
