"""Real SSH recovery with an isolated, unprivileged localhost SSH server."""

import base64
import getpass
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from envweaver import Runtime, RuntimeRef
from envweaver._io import ssh_transport
from envweaver._io.storage import _dispatch
from envweaver.errors import RuntimeError as SandboxError
from envweaver.runtime import _ssh

pytestmark = pytest.mark.integration


@pytest.fixture
def ssh_server(tmp_path, monkeypatch):
    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    if not Path(sshd).exists():
        pytest.skip("requires OpenSSH server")
    for name in ("host", "client"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / name)], check=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    known = tmp_path / "known_hosts"
    known.write_text(f"[127.0.0.1]:{port} " + (tmp_path / "host.pub").read_text())
    client_config = tmp_path / "client_config"
    client_config.write_text("Host *\n  IdentitiesOnly yes\n  IdentityAgent none\n  ControlMaster no\n")
    server_config = tmp_path / "sshd_config"
    server_config.write_text(
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {tmp_path / 'host'}\n"
        f"PidFile {tmp_path / 'sshd.pid'}\nAuthorizedKeysFile {tmp_path / 'client.pub'}\n"
        "StrictModes no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM no\n"
        f"AllowUsers {getpass.getuser()}\n"
    )
    options = {"config_file": str(client_config), "identity_file": str(tmp_path / "client"),
               "known_hosts_file": str(known)}
    connection = {"host": "127.0.0.1", "port": port, "user": getpass.getuser(),
                  "python": sys.executable, **options}
    original_args = ssh_transport.command_args
    monkeypatch.setattr(ssh_transport, "command_args", lambda host, command, **kwargs: original_args(
        host, command, ssh_options=options,
    ))
    with (tmp_path / "sshd.log").open("wb") as log:
        server = subprocess.Popen([sshd, "-D", "-e", "-f", str(server_config)], stdout=log, stderr=log)
        try:
            for _ in range(100):
                if server.poll() is not None:
                    pytest.fail((tmp_path / "sshd.log").read_text())
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)
            _ssh.request(connection, "health")
            yield connection, f"ssh://{getpass.getuser()}@127.0.0.1:{port}"
        finally:
            server.terminate()
            server.wait(timeout=10)


def test_reconnect_before_command_runs_once(ssh_server, tmp_path, monkeypatch):
    connection, _ = ssh_server
    with socket.socket() as closed_port:
        closed_port.bind(("127.0.0.1", 0))
        unavailable = closed_port.getsockname()[1]
    original = subprocess.run
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            args = list(args)
            args[args.index("-p") + 1] = str(unavailable)
        return original(args, **kwargs)

    monkeypatch.setattr(_ssh.subprocess, "run", run)
    marker = tmp_path / "executions"
    command = [sys.executable, "-c", f"from pathlib import Path; p=Path({str(marker)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')"]
    assert _ssh.execute(connection, command)[0] == 0
    assert marker.read_text() == "x"
    assert len(calls) == 2


@pytest.mark.parametrize("action", ["write", "publish", "read"])
def test_transfer_recovers_after_remote_completion(ssh_server, tmp_path, monkeypatch, action):
    _, host = ssh_server
    target = tmp_path / "file"
    body = "完整文件内容\n".encode() * 10000
    if action == "read":
        target.write_bytes(body)
    original = subprocess.run
    calls = []

    def run(args, **kwargs):
        result = original(args, **kwargs)
        calls.append(result.returncode)
        assert result.returncode == 0, result.stderr
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(args, 60)
        return result

    monkeypatch.setattr(ssh_transport.subprocess, "run", run)
    payload = {"action": action}
    if action != "read":
        payload.update(kind="file", body=base64.b64encode(body).decode(), mode=0o600)
    result = _dispatch(f"{host}{target}", payload)
    if action == "read":
        assert base64.b64decode(result["body"]) == body
    assert target.read_bytes() == body
    assert len(calls) == 2
    assert not list(tmp_path.glob(".envweaver-*"))


def test_lost_command_reply_does_not_execute_twice(ssh_server, tmp_path, monkeypatch):
    connection, _ = ssh_server
    original = subprocess.run
    calls = []

    def run(args, **kwargs):
        result = original(args, **kwargs)
        calls.append(result)
        assert result.returncode == 0, result.stderr
        return subprocess.CompletedProcess(args, 255, b"", b"Connection reset by peer")

    monkeypatch.setattr(_ssh.subprocess, "run", run)
    marker = tmp_path / "executions"
    command = [sys.executable, "-c", f"open({str(marker)!r},'a').write('x')"]
    with pytest.raises(SandboxError):
        _ssh.execute(connection, command)
    assert marker.read_text() == "x"
    assert len(calls) == 1


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_docker_transfer_recovers_without_replacing_instance(tmp_path, monkeypatch, direction):
    if not shutil.which("docker"):
        pytest.skip("requires Docker and python:3.12-slim-bookworm")
    runtime = Runtime()
    instance = runtime.start(ref=RuntimeRef("docker", "python:3.12-slim-bookworm", {
        "command": ["sleep", "infinity"],
    }))
    original = subprocess.run
    copies = []
    source = tmp_path / "source"
    source.write_bytes(b"complete payload\n" * 10000)
    try:
        if direction == "download":
            runtime.upload(instance=instance, source=source, target="/tmp/payload")

        def run(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] == "docker" and "cp" in args:
                assert result.returncode == 0, result.stderr
                copies.append(args)
                if len(copies) == 1:
                    raise subprocess.TimeoutExpired(args, 300)
            return result

        monkeypatch.setattr(subprocess, "run", run)
        if direction == "upload":
            runtime.upload(instance=instance, source=source, target="/tmp/payload")
            monkeypatch.setattr(subprocess, "run", original)
        runtime.download(instance=instance, source="/tmp/payload", target=tmp_path / "received")
        assert (tmp_path / "received").read_bytes() == source.read_bytes()
        assert len(copies) == 2
        assert copies[0] == copies[1]
    finally:
        monkeypatch.setattr(subprocess, "run", original)
        runtime.close(instance=instance)
