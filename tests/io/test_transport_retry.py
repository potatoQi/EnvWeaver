import importlib
import io
import json
import subprocess
import threading
from types import SimpleNamespace

import pytest

from envweaver import Runtime
from envweaver._io import files, ssh_transport
from envweaver._io.retry import ConnectionFailure, connection_error, retry
from envweaver._io.storage import _dispatch
from envweaver.errors import RuntimeError as SandboxError
from envweaver.runtime import _ssh

recovery = importlib.import_module("envweaver._io.retry")


@pytest.fixture(autouse=True)
def fast_recovery(monkeypatch):
    monkeypatch.setattr(recovery.time, "sleep", lambda delay: None)


@pytest.mark.parametrize("message", [
    "Permission denied (publickey).", "Host key verification failed.",
    "REMOTE HOST IDENTIFICATION HAS CHANGED!", "python3: command not found",
])
def test_permanent_failures_do_not_retry(message):
    calls = []

    def operation():
        calls.append(1)
        raise connection_error(message)

    with pytest.raises(SandboxError):
        retry(operation, replay=True)
    assert len(calls) == 1


@pytest.mark.parametrize("replay,before_dispatch,expected", [
    (False, False, 1), (False, True, 3), (True, False, 3),
])
def test_bounded_retries_respect_command_dispatch(replay, before_dispatch, expected):
    calls = []

    def operation():
        calls.append(1)
        raise ConnectionFailure("disconnected", before_dispatch=before_dispatch)

    with pytest.raises(ConnectionFailure):
        retry(operation, replay=replay)
    assert len(calls) == expected


def test_nested_layers_share_one_attempt_budget():
    calls = []

    def operation():
        calls.append(1)
        raise TimeoutError("transfer timed out")

    with pytest.raises(TimeoutError):
        retry(lambda: retry(operation, replay=True), replay=True)
    assert len(calls) == 3


def test_cancellation_interrupts_recovery_wait():
    cancelled = threading.Event()

    def operation():
        cancelled.set()
        raise ConnectionResetError("disconnected")

    with pytest.raises(InterruptedError, match="cancelled"):
        retry(operation, replay=True, cancelled=cancelled)


@pytest.mark.parametrize("action,diagnostic,expected", [
    ("exec", "ssh: connect to host server port 22: Connection refused", 3),
    ("exec", "Connection reset by peer", 1),
    ("health", "Connection reset by peer", 3),
    ("download", "Connection reset by peer", 3),
    ("upload", "Connection reset by peer", 3),
    ("health", "Permission denied (publickey).", 1),
])
def test_guest_ssh_reconnect_policy(monkeypatch, action, diagnostic, expected):
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs["input"])
        return SimpleNamespace(returncode=255, stdout=b"", stderr=diagnostic.encode())

    monkeypatch.setattr(_ssh.subprocess, "run", run)
    with pytest.raises(SandboxError):
        _ssh.request({"host": "server", "user": "worker"}, action)
    assert len(calls) == expected
    assert len(set(calls)) == 1


def test_remote_command_failure_is_not_a_connection_failure(monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"Connection reset by peer")

    monkeypatch.setattr(ssh_transport.subprocess, "run", run)
    with pytest.raises(SandboxError):
        retry(lambda: ssh_transport.ssh("ssh://server", ["false"], {}), replay=True)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["disconnect", "timeout"])
def test_file_publication_retries_lost_response(monkeypatch, tmp_path, failure):
    from envweaver._io.storage import _operate

    target = tmp_path / "result.json"
    calls = []

    def run(args, **kwargs):
        payload = json.loads(kwargs["input"])
        calls.append(payload)
        _operate(payload)
        if len(calls) == 1:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 60)
            return SimpleNamespace(returncode=255, stdout=b"", stderr=b"Connection reset by peer")
        return SimpleNamespace(returncode=0, stdout=b"null", stderr=b"")

    monkeypatch.setattr(ssh_transport.subprocess, "run", run)
    _dispatch(f"ssh://files{target}", {"action": "publish", "body": "e30=", "mode": 0o600})
    assert target.read_text() == "{}"
    assert calls[0] == calls[1]
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_file_write_preserves_previous_content_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "answer"
    target.write_bytes(b"previous")

    def broken_copy(source, output):
        output.write(b"partial")
        raise OSError("interrupted write")

    monkeypatch.setattr(files.shutil, "copyfileobj", broken_copy)
    with pytest.raises(OSError, match="interrupted"):
        files._write_file(target, io.BytesIO(b"next version"))
    assert target.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_runtime_file_transfer_recovers_on_same_instance(tmp_path, monkeypatch, ssh_instance, direction):
    backend = Runtime()._backend("qemu")
    original = getattr(backend, direction)
    calls = []
    source = tmp_path / "source"
    source.write_bytes(b"unchanged input")
    target = tmp_path / "destination"

    def transfer(instance, source, target):
        calls.append(instance.id)
        original(instance, source, target)
        if len(calls) == 1:
            raise TimeoutError("response lost after transfer")

    monkeypatch.setattr(backend, direction, transfer)
    runtime = Runtime()
    monkeypatch.setattr(runtime, "_backend", lambda name: backend)
    getattr(runtime, direction)(instance=ssh_instance, source=str(source), target=str(target))
    assert calls == [ssh_instance.id, ssh_instance.id]
    assert target.read_bytes() == source.read_bytes()
