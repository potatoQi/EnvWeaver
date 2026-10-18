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


def test_qemu_snapshot_preserves_backing_and_restarts_on_conversion_failure(tmp_path, monkeypatch):
    backend = QemuBackend(tmp_path)
    firmware = tmp_path / "firmware.fd"
    nvram = tmp_path / "live-nvram.fd"
    firmware.write_bytes(b"firmware")
    nvram.write_bytes(b"guest-variables")
    config = {
        "firmware": str(firmware),
        "nvram": "old-nvram.fd",
        "disk_dir": str(tmp_path / "disks"),
    }
    instance = Instance(
        "vm",
        "qemu",
        str(tmp_path),
        {
            "ref": RuntimeRef("qemu", "/images/base.qcow2", config).to_dict(),
            "ssh": {
                "user": "actual-user",
                "python": "/opt/python",
                "host": "localhost",
                "port": 2222,
                "identity_file": "/keys/private",
            },
            "disk": "live-overlay.qcow2",
            "nvram": str(nvram),
        },
    )
    events = []
    monkeypatch.setattr(backend, "_shutdown", lambda _: events.append("shutdown"))
    monkeypatch.setattr(backend, "_launch", lambda _: events.append("launch"))

    def convert(args):
        events.append("convert")
        assert args[1:-2] == [
            "convert",
            "-f",
            "qcow2",
            "-O",
            "qcow2",
            "-B",
            "/images/base.qcow2",
            "-F",
            "qcow2",
        ]
        assert args[-2] == "live-overlay.qcow2"
        Path(args[-1]).write_bytes(b"delta-disk")

    monkeypatch.setattr(backend, "_run", convert)
    saved = backend.save(instance)
    assert saved.connection == {"ssh": {"user": "actual-user", "python": "/opt/python"}}
    assert (
        json.loads(next((tmp_path / "runtimes").rglob("runtime.json")).read_text())["connection"]
        == saved.connection
    )
    assert events == ["shutdown", "convert", "launch"]
    assert Path(saved.image).read_bytes() == b"delta-disk"
    assert Path(saved.config["nvram"]).read_bytes() == b"guest-variables"
    assert Path(saved.config["firmware"]).read_bytes() == b"firmware"
    assert not list((tmp_path / "runtimes").rglob("*.qcow2"))
    assert not list((tmp_path / "disks").rglob("*.json"))
    events.clear()
    assets_before = set((tmp_path / "runtimes").iterdir())

    def fail(_):
        raise OSError("disk full")

    monkeypatch.setattr(backend, "_run", fail)
    with pytest.raises(OSError, match="disk full"):
        backend.save(instance)
    assert events == ["shutdown", "launch"]
    assert set((tmp_path / "runtimes").iterdir()) == assets_before


@pytest.mark.parametrize("failure", [None, "info", "rebase"])
def test_consolidated_save_rebases_new_snapshot_and_cleans_failed_output(
    tmp_path, monkeypatch, failure
):
    backend = QemuBackend(tmp_path / "records")
    instance = Instance(
        "vm",
        "qemu",
        str(backend.root),
        {
            "ref": RuntimeRef(
                "qemu",
                "/images/A.qcow2",
                {"disk_dir": str(tmp_path / "disks"), "save_mode": "consolidated"},
            ).to_dict(),
            "disk": "/instances/live.qcow2",
        },
    )
    events = []
    monkeypatch.setattr(backend, "_shutdown", lambda _: events.append("shutdown"))
    monkeypatch.setattr(backend, "_launch", lambda _: events.append("launch"))

    def run(args):
        action = args[1]
        events.append(action)
        if action == failure:
            raise RuntimeError("injected failure")
        if action == "convert":
            assert args[args.index("-B") + 1] == "/images/A.qcow2"
            Path(args[-1]).write_bytes(b"snapshot")
        elif action == "info":
            return json.dumps(
                [
                    {"filename": args[-1], "format": "qcow2"},
                    {
                        "filename": "/images/A.qcow2",
                        "format": "qcow2",
                        "full-backing-filename": "/images/base.raw",
                    },
                    {"filename": "/images/base.raw", "format": "raw"},
                ]
            )
        elif action == "rebase":
            assert "-u" not in args
            assert args[args.index("-b") + 1] == "/images/base.raw"
            assert args[args.index("-F") + 1] == "raw"
            assert Path(args[-1]).is_relative_to(tmp_path / "disks/runtimes")
        else:
            pytest.fail(f"Unexpected command: {args}")

    monkeypatch.setattr(backend, "_run", run)
    if failure:
        with pytest.raises(RuntimeError, match="injected failure"):
            backend.save(instance)
        assert not list((tmp_path / "disks/runtimes").iterdir())
        assert not backend.root.exists()
    else:
        saved = backend.save(instance)
        assert Path(saved.image).read_bytes() == b"snapshot"
        assert saved.config["save_mode"] == "consolidated"
        assert events == ["shutdown", "convert", "info", "rebase", "launch"]
    assert events[-1] == "launch"


@pytest.mark.parametrize("host", [None, "ssh://server"])
@pytest.mark.parametrize("method", ["create", "start", "save"])
@pytest.mark.parametrize("mode", ["invalid", None])
def test_invalid_save_mode_fails_before_touching_qemu(tmp_path, monkeypatch, host, method, mode):
    backend = QemuBackend(tmp_path, host=host)
    ref = RuntimeRef(
        "qemu",
        "/images/base.qcow2",
        {
            "disk_dir": str(tmp_path / "disks"),
            "save_mode": mode,
        },
    )

    def unexpected(*args, **kwargs):
        pytest.fail("Invalid save_mode must fail before contacting or stopping QEMU")

    monkeypatch.setattr(backend, "_call_remote", unexpected)
    monkeypatch.setattr(backend, "_start", unexpected)
    monkeypatch.setattr(backend, "_shutdown", unexpected)
    with pytest.raises(ValueError, match="config.save_mode"):
        if method == "create":
            backend.create(image=ref.image, config=ref.config)
        elif method == "start":
            backend.start(ref)
        else:
            backend.save(Instance("vm", "qemu", data={"ref": ref.to_dict()}, host=host))
    assert not list(tmp_path.iterdir())


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


def test_close_preserves_live_disk_on_exit_timeout(tmp_path, monkeypatch):
    from envweaver.runtime import qemu

    directory = tmp_path / "instances" / "live"
    directory.mkdir(parents=True)
    disk = directory / "disk.qcow2"
    disk.write_bytes(b"live-delta")
    pidfile = directory / "qemu.pid"
    pidfile.write_text("12345")
    instance = Instance(
        "live",
        "qemu",
        str(tmp_path),
        {
            "disk": str(disk),
            "pidfile": str(pidfile),
            "qmp": str(directory / "qmp.sock"),
        },
    )

    class QMP:
        def __init__(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, command):
            assert command == "quit"

    monkeypatch.setattr(qemu, "QMP", QMP)
    ticks = iter([0, 16])
    monkeypatch.setattr(qemu.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeError, match="live files were preserved"):
        QemuBackend(tmp_path).close(instance)
    assert disk.read_bytes() == b"live-delta"
    assert pidfile.exists()


@pytest.mark.parametrize("host", [None, "ssh://server"])
@pytest.mark.parametrize("method", ["create", "start"])
@pytest.mark.parametrize("disk_dir", [None, "", "  ", "relative", 42])
def test_qemu_requires_absolute_disk_dir_before_start(
    tmp_path, monkeypatch, host, disk_dir, method
):
    from envweaver.runtime import _remote

    def unexpected(*args, **kwargs):
        pytest.fail("Invalid disk_dir must fail before contacting or starting QEMU")

    monkeypatch.setattr(QemuBackend, "_run", unexpected)
    monkeypatch.setattr(_remote, "call", unexpected)
    config = {} if disk_dir is None else {"disk_dir": disk_dir}
    with pytest.raises(ValueError, match="disk_dir"):
        backend = QemuBackend(tmp_path, host=host)
        if method == "create":
            backend.create(image="/base.qcow2", config=config)
        else:
            backend.start(RuntimeRef("qemu", "/base.qcow2", config))
    assert not list(tmp_path.iterdir())
