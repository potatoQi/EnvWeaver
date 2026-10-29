import io
import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest
from PIL import Image

from envweaver import Instance, Runtime, RuntimeRef
from envweaver.errors import RuntimeError
from envweaver.runtime.qemu import QMP, QemuBackend


@pytest.mark.parametrize("host", [None, "ssh://server"])
def test_screenshot_returns_png_with_original_pixels(monkeypatch, host):
    from envweaver.runtime import _remote, qemu

    pixels = b"\x0a\x20\x23\xff\x00\x80"
    screenshots = []

    class Monitor:
        def __init__(self, path):
            assert path == "/vm/qmp.sock"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, command, arguments):
            assert command == "screendump"
            assert set(arguments) == {"filename"}
            path = Path(arguments["filename"])
            screenshots.append(path)
            path.write_bytes(b"P6\n2 1\n255\n" + pixels)

    def call(server, python, payload, **kwargs):
        assert server == host
        return _remote.dispatch(payload)

    monkeypatch.setattr(qemu, "QMP", Monitor)
    monkeypatch.setattr(_remote, "call", call)
    result = Runtime().screenshot(Instance("vm", "qemu", data={"qmp": "/vm/qmp.sock"}, host=host))
    with Image.open(io.BytesIO(result)) as image:
        assert image.format == "PNG"
        assert image.size == (2, 1)
        assert image.tobytes() == pixels
    assert len(screenshots) == 1
    assert not screenshots[0].parent.exists()


def test_screenshot_cleans_up_invalid_image(monkeypatch):
    from envweaver.runtime import qemu

    screenshots = []

    class Monitor:
        def __init__(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, command, arguments):
            path = Path(arguments["filename"])
            screenshots.append(path)
            path.write_bytes(b"P6\n2 1\n255\n\x00")

    monkeypatch.setattr(qemu, "QMP", Monitor)
    with pytest.raises(OSError):
        Runtime().screenshot(Instance("vm", "qemu", data={"qmp": "/vm/qmp.sock"}))
    assert not screenshots[0].parent.exists()


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


def test_launch_uses_ssh_connection_port(tmp_path, monkeypatch):
    backend = QemuBackend()
    backend._check_ssh = False
    instance = Instance(
        "vm",
        "qemu",
        data={
            "ref": RuntimeRef("qemu", "/base.qcow2").to_dict(),
            "disk": "/live.qcow2",
            "qmp": str(tmp_path / "qmp.sock"),
            "pidfile": str(tmp_path / "qemu.pid"),
            "ssh": {"host": "127.0.0.1", "port": 2222, "user": "worker"},
        },
    )
    commands = []
    monkeypatch.setattr(backend, "_run", commands.append)
    backend._launch(instance)
    instance.data["ssh"]["port"] = 3333
    backend._launch(instance)
    assert [args[args.index("-netdev") + 1] for args in commands] == [
        "user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22",
        "user,id=net0,hostfwd=tcp:127.0.0.1:3333-:22",
    ]


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
            "ref": RuntimeRef(
                "qemu", "/images/base.qcow2", config, name="做题环境", metadata={"tags": ["base"]}
            ).to_dict(),
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
    assert saved.name == "做题环境"
    assert saved.metadata == {"tags": ["base"]}
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


@pytest.mark.parametrize("failure", ["image", "nvram", "launch", "ready"])
def test_failed_start_removes_owned_directory(monkeypatch, failure):
    with tempfile.TemporaryDirectory(prefix="ew-start-") as directory:
        root = Path(directory)
        image = root / "base.qcow2"
        image.touch()
        backend = QemuBackend()
        closed = []

        def run(args):
            Path(args[-1]).touch()
            if failure == "image":
                raise OSError("image failed")

        def launch(instance):
            if failure == "ready":
                Path(instance.data["pidfile"]).write_text("123")
            raise OSError("launch failed")

        def close(instance):
            closed.append(instance.id)
            shutil.rmtree(instance.data["owned_directory"])

        monkeypatch.setattr(backend, "_run", run)
        monkeypatch.setattr(backend, "_launch", launch)
        monkeypatch.setattr(backend, "_close", close)
        config = {"disk_dir": str(root)}
        if failure == "nvram":
            config["nvram"] = str(root / "missing.fd")
        with pytest.raises(OSError):
            backend.start(RuntimeRef("qemu", str(image), config), {"ssh": {"user": "test"}})
        assert list((root / "instances").iterdir()) == []
        assert bool(closed) == (failure == "ready")


def test_failed_start_preserves_live_files_if_cleanup_fails(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="ew-start-") as directory:
        root = Path(directory)
        image = root / "base.qcow2"
        image.touch()
        backend = QemuBackend()

        def launch(instance):
            Path(instance.data["pidfile"]).write_text("123")
            raise TimeoutError("SSH unavailable")

        def close(instance):
            raise OSError("QMP unavailable")

        monkeypatch.setattr(backend, "_run", lambda args: Path(args[-1]).touch())
        monkeypatch.setattr(backend, "_launch", launch)
        monkeypatch.setattr(backend, "_close", close)
        with pytest.raises(TimeoutError, match="SSH unavailable") as caught:
            backend.start(
                RuntimeRef("qemu", str(image), {"disk_dir": str(root)}),
                {"ssh": {"user": "test"}},
            )
        assert "QMP unavailable" in caught.value.__notes__[0]
        assert len(list((root / "instances").glob("*/disk.qcow2"))) == 1
