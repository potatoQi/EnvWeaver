import json
import subprocess
import sys

import pytest

from envweaver import Instance, Runtime, RuntimeRef
from envweaver._io import ssh_transport as _ssh_transport
from envweaver._io import storage as _storage
from envweaver.runtime import _remote
from envweaver.runtime.docker import DockerBackend


@pytest.fixture
def storage_calls(monkeypatch):
    calls = []

    def ssh(host, command, payload):
        calls.append((host, payload))
        # Execute the exact standalone script sent to the storage server.
        return subprocess.run(
            [sys.executable, *command[1:]],
            input=json.dumps(payload).encode(),
            capture_output=True,
            check=True,
        ).stdout

    monkeypatch.setattr(_ssh_transport, "ssh", ssh)
    return calls


@pytest.mark.parametrize("remote", [False, True])
def test_record_write_and_cleanup(tmp_path, storage_calls, remote):
    directory = tmp_path / "records with spaces"
    root = f"ssh://user@storage:2222{directory}" if remote else directory
    runtime = Runtime(root=root, host="ssh://compute")
    _storage.write_records(
        runtime.root, "instances/one", {"instance.json": {"id": "one"}}, private=True
    )
    record = directory / "instances/one/instance.json"
    assert json.loads(record.read_text()) == {"id": "one"}
    assert record.stat().st_mode & 0o777 == 0o600
    _storage.write_records(runtime.root, "runtimes/saved", {"runtime.json": {"image": "base"}})
    _storage.remove_records(runtime.root, "instances/one")
    _storage.remove_records(runtime.root, "instances/one")
    assert not record.exists()
    assert (directory / "runtimes/saved/runtime.json").exists()
    if remote:
        assert {host for host, _ in storage_calls} == {"ssh://user@storage:2222"}
        assert all(call["root"] == str(directory) for _, call in storage_calls)
        assert "%20" in runtime.root
    else:
        assert not storage_calls


@pytest.mark.parametrize(
    "root",
    [
        "",
        "ssh://storage",
        "ssh://storage/path?query",
        "ssh://storage/path#fragment",
        "ssh://user:password@storage/path",
        "https://storage/path",
        "ssh://storage:bad/path",
        "ssh://storage/path%00",
    ],
)
def test_invalid_storage_url(root):
    with pytest.raises(ValueError):
        Runtime(root=root)


def test_failed_write_does_not_publish_partial_record(tmp_path):
    with pytest.raises(ValueError):
        _storage.write_records(tmp_path, "runtimes/one", {"ok.json": {}, "bad.json": float("nan")})
    assert list((tmp_path / "runtimes").iterdir()) == []
    with pytest.raises(ValueError):
        _storage.remove_records(tmp_path, "../outside")


def test_docker_saves_records_on_storage_server(tmp_path, monkeypatch, storage_calls):
    root = f"ssh://storage{tmp_path}"
    runtime = Runtime(root=root, host="ssh://compute")
    calls = []

    def docker(self, *args, host=None, **kwargs):
        calls.append(host)
        return "sha256:saved"

    monkeypatch.setattr(DockerBackend, "_docker", docker)
    monkeypatch.setattr(DockerBackend, "exec", lambda *args: (0, "", ""))
    instance = Instance(
        "container",
        "docker",
        root,
        {"ref": RuntimeRef("docker", "base").to_dict(), "python": "python3", "host_ssh": {}},
        host="ssh://compute",
    )
    saved = runtime.save(instance=instance)
    record = next(tmp_path.glob("runtimes/*/runtime.json"))
    assert json.loads(record.read_text()) == saved.to_dict()
    assert json.loads(record.with_name("metadata.json").read_text())["host"] == "ssh://compute"
    runtime.close(instance=instance)
    assert record.exists()
    assert set(calls) == {"ssh://compute"}
    assert {host for host, _ in storage_calls} == {"ssh://storage"}


def test_qemu_records_use_storage_server_and_close_uses_original_root(
    tmp_path, monkeypatch, storage_calls
):
    from envweaver.runtime.qemu import QemuBackend

    monkeypatch.setattr(QemuBackend, "_wait_ready", lambda *a: None)
    root = f"ssh://storage{tmp_path / 'records'}"
    runtime = Runtime(root=root, host="ssh://compute")
    ref = RuntimeRef("qemu", "/disks/base.qcow2", {"disk_dir": "/disks"})
    calls = []

    def call(host, python, payload, *, timeout, ssh_options=None):
        calls.append((host, payload["action"]))
        if payload["action"] == "start":
            return Instance(
                "vm", "qemu", root, {"ref": ref.to_dict(), "ssh": {}, "qmp": "/disks/vm/qmp.sock"}
            ).to_dict()
        if payload["action"] == "save":
            return RuntimeRef("qemu", "/disks/saved.qcow2", ref.config).to_dict()

    monkeypatch.setattr(_remote, "call", call)
    instance = runtime.start(ref=ref)
    descriptor = tmp_path / "records/instances/vm/instance.json"
    record = json.loads(descriptor.read_text())
    assert record["version"] == 1
    assert record["qmp"] == "/disks/vm/qmp.sock"
    assert "root" not in record and "data" not in record
    runtime.save(instance=instance)
    # A different manager must still remove this instance's original record.
    Runtime(root=tmp_path / "other").close(instance=instance)
    assert not descriptor.exists()
    assert len(list((tmp_path / "records").glob("runtimes/*/runtime.json"))) == 1
    assert calls == [("ssh://compute", action) for action in ("start", "save", "close")]
    assert {host for host, _ in storage_calls} == {"ssh://storage"}
    assert not list(tmp_path.rglob("*.qcow2"))
