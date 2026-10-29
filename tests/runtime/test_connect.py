import io
import json
from pathlib import Path

import pytest
from PIL import Image

from envweaver import Instance, Runtime, RuntimeRef
from envweaver.runtime import _ssh, qemu


@pytest.fixture
def qemu_record(tmp_path, monkeypatch):
    disk = tmp_path / "live.qcow2"
    disk.write_bytes(b"live disk")
    record = {
        "version": 1,
        "backend": "qemu",
        "host": None,
        "qmp": str(tmp_path / "qmp.sock"),
        "ssh": {"host": "127.0.0.1", "port": 2222, "user": "worker"},
        "startup": {
            "machine": "q35",
            "accel": "kvm",
            "disk_interface": "virtio",
            "network_device": "virtio-net-pci",
            "args": ["-vga", "std"],
        },
    }

    class Monitor:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, command, arguments=None):
            if command == "screendump":
                Path(arguments["filename"]).write_bytes(b"P6\n1 1\n255\n\xff\x00\x00")
                return
            return {
                "query-status": {"status": "running"},
                "query-block": [{"inserted": {"drv": "qcow2", "file": str(disk), "ro": False}}],
                "query-memory-size-summary": {"base-memory": 8192 * 1024 * 1024},
                "query-name": {"name": "external"},
            }[command]

    monkeypatch.setattr(qemu, "QMP", Monitor)
    monkeypatch.setattr(_ssh, "request", lambda *a, **k: None)
    monkeypatch.setattr(
        qemu.QemuBackend,
        "_run",
        lambda *a: pytest.fail("connect must not inspect the backing chain"),
    )
    return record


def connect_record(tmp_path, record):
    path = tmp_path / "instance.json"
    path.write_text(json.dumps(record))
    return Runtime().connect(path=path)


def test_external_qemu_queries_state_but_cannot_save(tmp_path, monkeypatch, qemu_record):
    instance = connect_record(tmp_path, qemu_record)
    assert instance.root is None
    assert instance.data["ssh"]["port"] == qemu_record["ssh"]["port"]
    assert "port" not in instance.data
    assert instance.data["ref"]["config"] == qemu_record["startup"]
    assert "disk_dir" not in instance.data["ref"]["config"]
    with Image.open(io.BytesIO(Runtime().screenshot(instance))) as image:
        assert image.format == "PNG"
        assert image.getpixel((0, 0)) == (255, 0, 0)

    def unexpected(*args, **kwargs):
        pytest.fail("Connected instances must be rejected before backend.save")

    monkeypatch.setattr(qemu.QemuBackend, "save", unexpected)
    with pytest.raises(ValueError, match="returned by connect"):
        Runtime().save(instance)
    assert Path(instance.data["disk"]).read_bytes() == b"live disk"


def test_qemu_descriptor_omits_duplicate_storage_and_observed_values(tmp_path):
    instance = Instance(
        "vm",
        "qemu",
        "/records",
        {
            "qmp": "/vms/one/qmp.sock",
            "ssh": {"host": "127.0.0.1", "port": 2222, "user": "worker", "via": "ssh://server"},
            "ref": RuntimeRef(
                "qemu",
                "/base.qcow2",
                {
                    "memory": 8192,
                    "cpus": 4,
                    "disk_dir": "/snapshots",
                    "firmware": "/firmware.fd",
                    "remote_python": "/opt/env/bin/python",
                },
            ).to_dict(),
        },
        "ssh://server",
    )
    path = tmp_path / "instance.json"
    Runtime().describe(instance, path)
    record = json.loads(path.read_text())
    assert set(record) == {"version", "backend", "host", "qmp", "ssh", "startup"}
    assert "via" not in record["ssh"]
    assert not {"disk_dir", "memory", "cpus", "backend", "root"} & record["startup"].keys()
    assert record["startup"]["firmware"] == "/firmware.fd"
    assert record["startup"]["remote_python"] == "/opt/env/bin/python"
    assert path.stat().st_mode & 0o777 == 0o600


def test_external_qemu_close_retains_original_disk(tmp_path, monkeypatch, qemu_record):
    instance = connect_record(tmp_path, qemu_record)
    monkeypatch.setattr(qemu.QemuBackend, "_running", lambda *a: False)

    class Monitor:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def execute(self, command):
            assert command == "quit"

    monkeypatch.setattr(qemu, "QMP", Monitor)
    Runtime().close(instance)
    assert Path(instance.data["disk"]).exists()


def test_qemu_refuses_network_layout_it_cannot_restart(tmp_path, qemu_record):
    qemu_record["ssh"]["host"] = "192.168.1.10"
    with pytest.raises(ValueError, match="forwarding"):
        connect_record(tmp_path, qemu_record)


def test_qmp_timeout_does_not_mean_vm_has_exited(monkeypatch):
    def fail(*a, **k):
        raise TimeoutError("busy")

    monkeypatch.setattr(qemu, "QMP", fail)
    with pytest.raises(TimeoutError):
        qemu.QemuBackend._running(Instance("vm", "qemu", data={"qmp": "/qmp.sock"}))


def test_reconnected_managed_qemu_cannot_save(tmp_path, monkeypatch, qemu_record):
    # The live disk and marker determine ownership; the record carries no disk_dir.
    directory = tmp_path / "disks" / "instances" / "external"
    directory.mkdir(parents=True)
    (directory / ".envweaver-owned").write_text("external")
    disk = directory / "disk.qcow2"
    disk.write_bytes(b"live")
    qemu_record["qmp"] = str(directory / "qmp.sock")
    previous = qemu.QMP

    class Monitor(previous):
        def execute(self, command, arguments=None):
            if command == "query-block":
                return [{"inserted": {"drv": "qcow2", "file": str(disk), "ro": False}}]
            return super().execute(command, arguments)

    monkeypatch.setattr(qemu, "QMP", Monitor)
    instance = connect_record(tmp_path, qemu_record)
    assert "disk_dir" not in instance.data["ref"]["config"]
    assert instance.data["owned_directory"] == str(directory)

    def save(*args, **kwargs):
        pytest.fail("Reconnecting an EnvWeaver instance must not allow save")

    monkeypatch.setattr(qemu.QemuBackend, "_save", save)
    with pytest.raises(ValueError, match="returned by connect"):
        Runtime().save(instance)


@pytest.mark.parametrize("backend", ["docker", "qemu"])
def test_downstream_native_record_connects(tmp_path, monkeypatch, qemu_record, backend):
    from envweaver.runtime.docker import DockerBackend

    if backend == "docker":
        record = {
            "version": 1,
            "backend": "docker",
            "host": None,
            "container_id": "external-container",
        }

        def inspect(self, *args, **kwargs):
            assert args == ("inspect", "external-container")
            return json.dumps(
                [
                    {
                        "State": {"Running": True},
                        "Mounts": [],
                        "Config": {"Cmd": ["sleep", "infinity"]},
                        "HostConfig": {},
                        "Image": "sha256:base",
                        "Id": "external-container",
                    }
                ]
            )

        monkeypatch.setattr(DockerBackend, "_docker", inspect)
        monkeypatch.setattr(DockerBackend, "exec", lambda *a, **k: (0, "", ""))
    else:
        record = dict(qemu_record)
    (tmp_path / "instance.json").write_text(json.dumps(record))
    instance = Runtime().connect(path=tmp_path / "instance.json")
    assert instance.backend == backend
    assert instance.root is None
    with pytest.raises(ValueError, match="returned by connect"):
        Runtime().save(instance)


def test_connect_rejects_save_without_changing_original_instance(tmp_path, runtime):
    image = tmp_path / "image"
    image.mkdir()
    original = runtime.start(ref=runtime.create(backend="test", image=str(image)))
    descriptor = tmp_path / "instance.json"
    try:
        runtime.describe(instance=original, path=descriptor)
        connected = runtime.connect(path=descriptor)
        with pytest.raises(ValueError, match="returned by connect"):
            runtime.save(instance=connected)
        runtime.describe(instance=connected, path=descriptor)
        with pytest.raises(ValueError, match="returned by connect"):
            runtime.save(instance=runtime.connect(path=descriptor))
        assert "_connected" not in original.data
        assert runtime.save(instance=original).backend == "test"
    finally:
        runtime.close(instance=original)


@pytest.mark.parametrize("version", [True, 1.0, "1", None, 99])
def test_invalid_record_versions_fail_before_connect(tmp_path, monkeypatch, version):
    def unexpected(*args):
        pytest.fail("Invalid record must not contact an environment")

    runtime = Runtime()
    monkeypatch.setattr(runtime, "_backend", unexpected)
    path = tmp_path / "instance.json"
    path.write_text(json.dumps({"version": version, "backend": "docker", "container_id": "live"}))
    with pytest.raises(ValueError, match="version"):
        runtime.connect(path)
