"""Only uv-selected, hash-identified wheel files may be delivered to preparation."""

import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from envweaver.agents.adapters.terminus import _uv_install, install


@pytest.mark.parametrize("enabled", [True, False])
def test_uv_resolves_fresh_and_downloads_only_unambiguous_wheels(tmp_path, monkeypatch, enabled):
    if enabled:
        monkeypatch.setenv("ENVWEAVER_DOWNLOAD_REQUESTS", str(tmp_path / "requests"))
    else:
        monkeypatch.delenv("ENVWEAVER_DOWNLOAD_REQUESTS", raising=False)
    plan = """lock-version = "1.0"
created-by = "uv"
[[packages]]
name = "selected"
version = "1"
wheels = [{url = "https://example.com/selected-1-py3-none-any.whl", hashes = {sha256 = "digest"}}]
sdist = {url = "https://example.com/unneeded.tar.gz", hashes = {sha256 = "source"}}
[[packages]]
name = "ambiguous"
version = "1"
wheels = [{url = "https://example.com/a.whl"}, {url = "https://example.com/b.whl"}]
[[packages]]
name = "unhashed"
version = "1"
wheels = [{url = "https://example.com/unhashed.whl"}]
"""
    downloads, stages, folders = [], [], []

    def download(url, checksum, target):
        downloads.append((url, checksum))
        target.write_bytes(b"verified artifact")
        return True

    def run(command, **kwargs):
        stage = command[4]
        stages.append(stage)
        if stage == "compile":
            lock = Path(command[command.index("--output-file") + 1])
            lock.write_text(plan)
            folders.append(lock.parent)
        else:
            assert stage == "sync"
            data = tomllib.loads(Path(command[5]).read_text())
            selected = data["packages"][0]["wheels"][0]
            if enabled:
                assert Path(selected["path"]).read_bytes() == b"verified artifact"
                assert "url" not in selected
            else:
                assert selected["url"].endswith("selected-1-py3-none-any.whl")
            assert data["packages"][1:] == tomllib.loads(plan)["packages"][1:]

    monkeypatch.setitem(sys.modules, "_uv_install", _uv_install)
    monkeypatch.setitem(sys.modules, "_download_client", SimpleNamespace(download=download))
    monkeypatch.setattr(install.subprocess, "run", run)
    install.install_packages()
    install.install_packages()
    assert stages == ["compile", "sync", "compile", "sync"]
    assert len(downloads) == (2 if enabled else 0)
    assert all("selected-1-" in url for url, _ in downloads)
    assert not any(folder.exists() for folder in folders)


def test_cache_disabled_install_does_not_import_uv_helper(tmp_path, monkeypatch):
    monkeypatch.delenv('ENVWEAVER_DOWNLOAD_REQUESTS', raising=False)
    monkeypatch.setitem(sys.modules, '_uv_install', None)
    monkeypatch.setattr(sys, 'argv', ['terminus-install.py', str(tmp_path)])
    calls = []
    monkeypatch.setattr(install.subprocess, 'run', lambda command, **kwargs: calls.append((command, kwargs)))
    install.main()
    assert calls[0][0][1:3] == ['-m', 'pip']
    assert calls[1][0][1:6] == ['-m', 'uv', 'venv', '--python', '3.12']
    assert 'UV_PYTHON_INSTALL_MIRROR' not in calls[1][1]['env']
    assert calls[2][0][-1] == 'packages'
