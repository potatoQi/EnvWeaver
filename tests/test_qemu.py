import json
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from envweaver import Instance, RuntimeRef
from envweaver.errors import RuntimeError
from envweaver.runtime.qemu import QMP, QemuBackend


def test_qmp_handshake_ignores_events_and_surfaces_errors(tmp_path):
    short_directory = tempfile.TemporaryDirectory(prefix="ew-qmp-", dir="/tmp")
    path = Path(short_directory.name) / "qmp.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen()

    def serve():
        connection, _ = server.accept()
        with connection, connection.makefile("rwb") as stream:
            stream.write(b'{"QMP": {}}\n')
            stream.flush()
            while line := stream.readline():
                command = json.loads(line)
                stream.write(b'{"event":"STOP"}\n')
                result = {"id": command["id"]}
                if command["execute"] == "bad":
                    result["error"] = {"desc": "unknown command"}
                else:
                    result["return"] = {}
                stream.write(json.dumps(result).encode() + b"\n")
                stream.flush()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        with QMP(path) as client:
            assert client.execute("system_powerdown") == {}
            with pytest.raises(RuntimeError, match="unknown command"):
                client.execute("bad")
    finally:
        thread.join(timeout=3)
        server.close()
        short_directory.cleanup()


def test_qemu_snapshot_is_standalone_and_restarts_on_conversion_failure(tmp_path, monkeypatch):
    backend = QemuBackend(tmp_path)
    firmware = tmp_path / "firmware.fd"
    nvram = tmp_path / "live-nvram.fd"
    firmware.write_bytes(b"firmware")
    nvram.write_bytes(b"guest-variables")
    config = {"firmware": str(firmware), "nvram": "old-nvram.fd"}
    instance = Instance(
        "vm",
        "qemu",
        str(tmp_path),
        {
            "ref": RuntimeRef("qemu", "base.qcow2", config).to_dict(),
            "disk": "live-overlay.qcow2",
            "nvram": str(nvram),
        },
    )
    events = []
    monkeypatch.setattr(backend, "_shutdown", lambda _: events.append("shutdown"))
    monkeypatch.setattr(backend, "_launch", lambda _: events.append("launch"))

    def convert(args):
        events.append("convert")
        assert args[1:4] == ["convert", "-O", "qcow2"]
        Path(args[-1]).write_bytes(b"standalone-disk")

    monkeypatch.setattr(backend, "_run", convert)
    saved = backend.save(instance, "task")
    assert events == ["shutdown", "convert", "launch"]
    assert Path(saved.image).read_bytes() == b"standalone-disk"
    assert Path(saved.config["nvram"]).read_bytes() == b"guest-variables"
    assert Path(saved.config["firmware"]).read_bytes() == b"firmware"
    events.clear()

    def fail(_):
        raise OSError("disk full")

    monkeypatch.setattr(backend, "_run", fail)
    with pytest.raises(OSError, match="disk full"):
        backend.save(instance, "failed")
    assert events == ["shutdown", "launch"]


def test_windows_guest_command_uses_powershell(monkeypatch):
    from types import SimpleNamespace

    from envweaver.runtime import _command

    seen = []

    class Process:
        returncode = 0

        def __init__(self, args, **kwargs):
            seen.append((args, kwargs))

        def communicate(self, **kwargs):
            return b"ok", b""

    monkeypatch.setattr(_command, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(_command.subprocess, "Popen", Process)
    assert _command.execute("Get-ChildItem", "C:\\Tasks")["stdout"] == "ok"
    assert seen[0][0] == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-ChildItem",
    ]
    assert seen[0][1]["cwd"] == "C:\\Tasks"
