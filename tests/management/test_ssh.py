"""Executable caching never caches data or replays an executed operation."""

import hashlib
import json
import os
import subprocess
import sys

from envweaver.management import _ssh


def test_worker_cache_refreshes_code_and_recovers_when_missing_or_corrupt(tmp_path):
    calls = []

    def send(host, command, payload, **options):
        calls.append(command)
        return subprocess.run(
            [sys.executable, *command[1:]], input=json.dumps(payload).encode(),
            capture_output=True, check=True, env={**os.environ, "TMPDIR": str(tmp_path)},
        ).stdout

    source = "import json, sys; print(json.dumps({'result': json.load(sys.stdin)['value']}))"
    assert _ssh.call(send, "ssh://test", source, {"value": 1}) == {"result": 1}
    assert len(calls) == 2
    calls.clear()
    assert _ssh.call(send, "ssh://test", source, {"value": 2}) == {"result": 2}
    assert len(calls) == 1
    cache = tmp_path / f"envweaver-management-{os.getuid()}"
    path = cache / (hashlib.sha256(source.encode()).hexdigest() + ".py")
    for corrupt in (False, True):
        if corrupt:
            path.write_text("raise RuntimeError('must never execute corrupt code')")
        else:
            path.unlink()
        calls.clear()
        assert _ssh.call(send, "ssh://test", source, {"value": 3}) == {"result": 3}
        assert len(calls) == 2
    updated = source.replace("['value']", "['value'] + 10")
    assert _ssh.call(send, "ssh://test", updated, {"value": 4}) == {"result": 14}
    # A non-writable cache must not make otherwise readable Pools inaccessible.
    for path in cache.iterdir():
        path.unlink()
    cache.rmdir()
    cache.write_text("not a directory")
    assert _ssh.call(send, "ssh://test", source, {"value": 5}) == {"result": 5}


def test_worker_errors_do_not_replay_the_operation():
    calls = []

    def send(host, command, payload, **options):
        calls.append(command)
        return b'{"error": "write failed", "type": "OSError"}'

    assert _ssh.call(send, "ssh://test", "code", {})["error"] == "write failed"
    assert len(calls) == 1
