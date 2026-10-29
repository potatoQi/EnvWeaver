import hashlib
from pathlib import Path

import pytest

from envweaver.cache import DownloadCache
from envweaver.runtime._bootstrap import apt_downloads, prepare_python


def plan(url='https://example.org/python.deb', filename='python.deb', digest=None):
    checksum = digest or hashlib.sha256(b'package').hexdigest()
    return f"'{url}' {filename} 7 SHA256:{checksum}\n"


def test_apt_only_selects_safe_hash_addressed_packages():
    text = plan() + plan(url='https://user:password@example.org/p.deb')
    text += plan().replace('SHA256:', 'MD5Sum:')
    text += plan(url='file:///private/package.deb')
    assert list(apt_downloads(text)) == [
        ('https://example.org/python.deb', 'python.deb', 7,
         'sha256:' + hashlib.sha256(b'package').hexdigest())]
    with pytest.raises(ValueError, match='filename'):
        list(apt_downloads(plan(filename='../escape.deb')))


@pytest.mark.parametrize('available', [True, False])
def test_apt_copies_only_selected_archives_and_preserves_native_install(tmp_path, monkeypatch, available):
    calls, folders, downloads = [], [], []

    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == 'cp':
            folder = Path(args[1])
            folders.append(folder)
            assert sorted(p.name for p in folder.iterdir()) == (['python.deb'] if available else [])
        if args[0] == 'exec' and args[4].startswith('mode=plan'):
            return 'APT output\nENVWEAVER_APT_DOWNLOADS\n' + plan()
        return ''

    def download(self, **kwargs):
        downloads.append(kwargs)
        if not available:
            raise OSError('Only reachable from sandbox')
        kwargs['target'].write_bytes(b'package')

    monkeypatch.setattr(DownloadCache, '_download', download)
    prepare_python(docker, 'container', 'python3', '', {'https_proxy': 'https://proxy'})
    assert len(downloads) == 1
    assert downloads[0]['proxies'] == {'https': 'https://proxy'}
    assert calls[-1][4].startswith('mode=finish\narchives=/tmp/envweaver-apt-')
    assert not any(folder.exists() for folder in folders)


def test_apt_unneeded_bootstrap_does_not_touch_cache(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('No downloads when Python exists or custom installer handled it')
    monkeypatch.setattr(DownloadCache, '_download', forbidden)
    calls = []
    prepare_python(lambda *args, **kwargs: calls.append(args) or '', 'c', 'python3', 'custom', {})
    assert len(calls) == 1
    assert calls[0][-2:] == ('custom', '')


def test_prepare_hook_keeps_native_cache_and_shared_deadline(monkeypatch):
    calls = []
    ticks = iter([100, 101, 105, 110, 115])
    monkeypatch.setattr('envweaver.runtime._bootstrap.time.monotonic', lambda: next(ticks))
    def docker(*args, **kwargs):
        calls.append((args, kwargs['timeout']))
        if args[0] == 'exec' and args[4].startswith('mode=plan'):
            return 'ENVWEAVER_APT_DOWNLOADS\n'
        return ''
    prepare_python(docker, 'c', 'python3', '', {}, prepare_command='configure-source', timeout=1200)
    assert [timeout for _, timeout in calls] == [1199, 1190, 1185]
    assert calls[0][0][-2:] == ('', 'configure-source')
    assert calls[-1][0][4].startswith('mode=finish')


def test_apt_corruption_never_reaches_container(monkeypatch):
    def download(*args, **kwargs):
        raise ValueError('Checksum mismatch')
    def docker(*args, **kwargs):
        assert args[0] != 'cp'
        return 'ENVWEAVER_APT_DOWNLOADS\n' + plan()
    monkeypatch.setattr(DownloadCache, '_download', download)
    with pytest.raises(ValueError, match='Checksum'):
        prepare_python(docker, 'c', 'python3', '', {})


def test_apt_timeout_does_not_fall_back_to_unbounded_native_download(monkeypatch):
    def download(*args, **kwargs):
        raise InterruptedError('deadline')
    def docker(*args, **kwargs):
        assert args[0] != 'cp'
        assert 0 < kwargs['timeout'] <= 300
        return 'ENVWEAVER_APT_DOWNLOADS\n' + plan()
    monkeypatch.setattr(DownloadCache, '_download', download)
    with pytest.raises(TimeoutError, match='bootstrap'):
        prepare_python(docker, 'c', 'python3', '', {})


def test_prepare_hook_runs_once_before_native_plan_and_not_for_existing_python(tmp_path):
    import os
    import shlex
    import subprocess
    import sys

    script = Path(__file__).parents[2] / 'src/envweaver/runtime/_python.sh'
    events = tmp_path / 'events'
    binary = tmp_path / 'python3'
    apt = tmp_path / 'apt-get'
    apt.write_text('#!/bin/sh\n'
                   f'echo "apt:$*" >> {shlex.quote(str(events))}\n'
                   f'case "$*" in *Dir::Cache::archives=*) /bin/ln -s {shlex.quote(sys.executable)} '
                   f'{shlex.quote(str(binary))};; esac\n')
    apt.chmod(0o755)
    identity = tmp_path / 'id'
    identity.write_text('#!/bin/sh\necho 0\n')
    identity.chmod(0o755)
    (tmp_path / 'env').symlink_to('/usr/bin/env')
    (tmp_path / 'rm').symlink_to('/bin/rm')
    archives = tmp_path / 'archives'
    archives.mkdir()
    environment = {**os.environ, 'PATH': str(tmp_path)}
    hook = f'echo prepare >> {shlex.quote(str(events))}'
    def run(mode, command=hook):
        return subprocess.run(['/bin/sh', str(script), 'python3', '', command],
                              env={**environment, 'mode': mode, 'archives': str(archives)},
                              capture_output=True, text=True)
    plan_result = run('plan')
    assert plan_result.returncode == 0, plan_result.stderr
    assert 'ENVWEAVER_APT_DOWNLOADS' in plan_result.stdout
    assert run('finish').returncode == 0
    assert not archives.exists()
    assert run('plan', 'exit 42').returncode == 0
    lines = events.read_text().splitlines()
    assert lines[0] == 'prepare'
    assert lines.count('prepare') == 1
    assert lines[1] == 'apt:update'
    assert '--print-uris' in lines[2]
    assert lines[3].startswith(f'apt:-o Dir::Cache::archives={archives} install')
    binary.unlink()
    assert run('plan', 'exit 42').returncode == 42
