import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from envweaver import Instance, Runtime, RuntimeRef, _ssh_transport
from envweaver.runtime import _remote
from envweaver.runtime.qemu import QemuBackend


def test_ssh_port_quoting_credentials_and_errors(monkeypatch):
    calls = []
    response = {"result": {"ok": True}}

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(response).encode(), stderr=b"")

    monkeypatch.setattr(_ssh_transport.subprocess, "run", run)
    payload = {"credential": {"name": "TOKEN", "value": "secret-value"}}
    assert _remote.call(
        "ssh://user@server:2222", "/opt/env with spaces/python", payload, timeout=20
    ) == {"ok": True}
    args, options = calls[0]
    assert args[-3:-1] == ["--", "user@server"]
    assert args[args.index("-p") + 1] == "2222"
    assert shlex.split(args[-1])[0] == "/opt/env with spaces/python"
    assert "secret-value" not in " ".join(args)
    assert json.loads(options["input"]) == payload
    response = {"error": {"type": "FileNotFoundError", "message": "base missing"}}
    with pytest.raises(FileNotFoundError, match="base missing"):
        _remote.call("ssh://server", "python3", {}, timeout=20)


@pytest.mark.parametrize("mode", ["incremental", "consolidated"])
def test_remote_qemu_separates_local_records_and_remote_disks(tmp_path, monkeypatch, mode):
    records = tmp_path / "local-records"
    disk_dir = tmp_path / "remote-disks"
    other_records = tmp_path / "other-records"
    calls = []
    monkeypatch.setattr(QemuBackend, "_wait_ready", lambda *a: None)

    def start(self, ref, connection=None):
        assert self.host is None
        directory = Path(ref.config["disk_dir"]) / "instances/test-vm"
        directory.mkdir(parents=True)
        (directory / "disk.qcow2").write_bytes(b"delta")
        return Instance(
            "test-vm",
            "qemu",
            str(self.root),
            {
                "ssh": {"host": "127.0.0.1", "port": 2222, "user": "worker"},
                "ref": ref.to_dict(),
                "disk": str(directory / "disk.qcow2"),
                "qmp": str(directory / "qmp.sock"),
            },
        )

    def save(self, instance):
        assert instance.host is None
        assert not self._check_ssh
        assert instance.data["ssh"]["via"] == "ssh://server-a"
        asset = Path(instance.data["ref"]["config"]["disk_dir"]) / "runtimes/saved"
        asset.mkdir(parents=True)
        (asset / "disk.qcow2").write_bytes(b"saved")
        (asset / "mode.txt").write_text(instance.data["ref"]["config"]["save_mode"])
        return RuntimeRef("qemu", str(asset / "disk.qcow2"), instance.data["ref"]["config"])

    def close(self, instance):
        import shutil

        assert instance.host is None
        assert not self._check_ssh
        shutil.rmtree(Path(instance.data["disk"]).parent)

    monkeypatch.setattr(QemuBackend, "_start", start)
    monkeypatch.setattr(QemuBackend, "_save", save)
    monkeypatch.setattr(QemuBackend, "_close", close)

    def call(host, python, payload, *, timeout):
        calls.append((host, python, payload["action"]))
        original = json.dumps(payload, sort_keys=True)
        result = _remote.dispatch(payload)
        assert json.dumps(payload, sort_keys=True) == original
        return result

    monkeypatch.setattr(_remote, "call", call)
    manager = Runtime(root=records, host="ssh://server-a")
    ref = RuntimeRef(
        "qemu",
        "/images/base.qcow2",
        {
            "disk_dir": str(disk_dir),
            "save_mode": mode,
            "remote_python": "/opt/venv/bin/python",
        },
    )
    instance = manager.start(ref)
    assert instance.host == "ssh://server-a"
    assert instance.root == str(records)
    assert (
        json.loads((records / "instances/test-vm/instance.json").read_text())["host"]
        == instance.host
    )
    # The manager selects local record storage; the instance still selects server-a.
    other = Runtime(root=other_records, host="ssh://server-b")
    saved = other.save(instance)
    assert saved.config["save_mode"] == mode
    assert Path(saved.image).with_name("mode.txt").read_text() == mode
    saved_record = next(other_records.glob("runtimes/*/runtime.json"))
    assert json.loads(saved_record.read_text()) == saved.to_dict()
    assert json.loads(saved_record.with_name("metadata.json").read_text())["host"] == instance.host
    other.close(instance)
    assert Path(saved.image).exists()
    assert not (records / "instances/test-vm").exists()
    assert not (disk_dir / "instances/test-vm").exists()
    assert not list(records.rglob("*.qcow2"))
    assert not list(other_records.rglob("*.qcow2"))
    assert not list(disk_dir.rglob("*.json"))
    assert calls == [
        ("ssh://server-a", "/opt/venv/bin/python", action) for action in ("start", "save", "close")
    ]


def test_root_is_local_even_with_remote_host(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert Runtime(root="./records", host="ssh://server").root == tmp_path / "records"
    assert Runtime(root="~/records", host="ssh://server").root == Path.home() / "records"


def test_remote_start_closes_vm_if_local_record_cannot_be_saved(tmp_path, monkeypatch):
    from envweaver.runtime import qemu

    monkeypatch.setattr(QemuBackend, "_wait_ready", lambda *a: None)

    ref = RuntimeRef("qemu", "/images/base", {"disk_dir": "/data/disks"})
    instance = Instance(
        "vm",
        "qemu",
        str(tmp_path),
        {"ref": ref.to_dict(), "ssh": {}, "qmp": "/data/disks/vm/qmp.sock"},
    )
    closed = []

    def call(host, python, payload, *, timeout):
        if payload["action"] == "start":
            return instance.to_dict()
        closed.append((host, payload["action"]))

    monkeypatch.setattr(_remote, "call", call)

    def fail(*args, **kwargs):
        raise PermissionError("local records unavailable")

    monkeypatch.setattr(qemu, "write_records", fail)
    with pytest.raises(PermissionError, match="local records unavailable"):
        Runtime(root=tmp_path, host="ssh://server").start(ref)
    assert closed == [("ssh://server", "close")]
