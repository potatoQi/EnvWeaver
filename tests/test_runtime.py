from pathlib import Path

import pytest

from envweaver import Runtime, RuntimeRef


@pytest.mark.parametrize("host", [None, "ssh://server"])
def test_create_checks_docker_image_on_selected_host_without_start(tmp_path, monkeypatch, host):
    from envweaver.errors import RuntimeError
    from envweaver.runtime.docker import DockerBackend

    calls = []

    def docker(self, *args, **kwargs):
        calls.append((args, kwargs))
        if args[-1] == "missing":
            raise RuntimeError("No such image")
        return "sha256:pinned"

    monkeypatch.setattr(DockerBackend, "_docker", docker)
    manager = Runtime(root=tmp_path / "records", host=host)
    config = {"command": ["sleep", "infinity"]}
    ref = manager.create(
        backend="docker",
        image="python:3.11",
        config=config,
        connection={"vnc": {"password_env": "DESKTOP_SECRET"}},
    )
    assert ref == RuntimeRef(
        "docker", "sha256:pinned", config, {"vnc": {"password_env": "DESKTOP_SECRET"}}
    )
    assert ref.config is not config
    assert calls == [(("image", "inspect", "--format", "{{.Id}}", "python:3.11"), {"host": host})]
    assert not manager.root.exists()
    with pytest.raises(RuntimeError, match="No such image"):
        manager.create(backend="docker", image="missing")


def test_create_checks_qemu_files_without_starting_or_allocating_disk(tmp_path, monkeypatch):
    from envweaver.runtime.qemu import QemuBackend

    def unexpected(*args, **kwargs):
        pytest.fail("create must not start QEMU or allocate disks")

    monkeypatch.setattr(QemuBackend, "_run", unexpected)
    monkeypatch.chdir(tmp_path)
    Path("base.qcow2").write_bytes(b"disk")
    Path("firmware.fd").write_bytes(b"firmware")
    config = {"disk_dir": str(tmp_path / "disks"), "firmware": "firmware.fd"}
    manager = Runtime(root="records")
    ref = manager.create(backend="qemu", image="base.qcow2", config=config)
    assert ref.image == str(tmp_path / "base.qcow2")
    assert ref.config["firmware"] == str(tmp_path / "firmware.fd")
    assert config["firmware"] == "firmware.fd"
    assert not Path("disks").exists()
    assert not manager.root.exists()
    with pytest.raises(FileNotFoundError):
        manager.create(backend="qemu", image="missing.qcow2", config=config)
    with pytest.raises(FileNotFoundError):
        manager.create(backend="qemu", image="base.qcow2", config={**config, "nvram": "missing.fd"})


def test_create_remote_qemu_checks_remote_files(tmp_path, monkeypatch):
    from envweaver.runtime import _remote

    calls = []

    def remote(host, python, payload, *, timeout):
        calls.append((host, python, payload))
        assert payload["action"] == "create"
        return payload["ref"]

    monkeypatch.setattr(_remote, "call", remote)
    manager = Runtime(root=tmp_path / "records", host="ssh://server")
    ref = manager.create(
        backend="qemu",
        image="/server-only/base.qcow2",
        config={"disk_dir": "/disks"},
        connection={"ssh": {"user": "worker", "python": "python"}},
    )
    assert ref.image == "/server-only/base.qcow2"
    assert calls[0][:2] == ("ssh://server", "python3")
    assert not manager.root.exists()
    with pytest.raises(ValueError, match="absolute host paths"):
        manager.create(backend="qemu", image="relative.qcow2", config={"disk_dir": "/disks"})
    # The remote dispatcher checks real files and writes no local records.
    disk = tmp_path / "base.qcow2"
    disk.write_bytes(b"disk")
    payload = calls[0][2]
    payload["ref"]["image"] = str(disk)
    assert _remote.dispatch(payload)["image"] == str(disk)
    assert (
        _remote.dispatch(payload)["connection"]
        == ref.connection
        == {"ssh": {"user": "worker", "python": "python"}}
    )
    disk.unlink()
    with pytest.raises(FileNotFoundError):
        _remote.dispatch(payload)


@pytest.mark.parametrize("image", ["", " ", None])
def test_create_rejects_empty_image(tmp_path, image):
    with pytest.raises(ValueError, match="image"):
        Runtime(root=tmp_path).create(backend="docker", image=image)


def test_root_is_optional_without_implicit_environment_default(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVWEAVER_HOME", str(tmp_path / "implicit"))
    assert Runtime().root is None
    assert Runtime(root=None).root is None
    for root in ("", "   "):
        with pytest.raises(ValueError, match="root"):
            Runtime(root=root)
    monkeypatch.chdir(tmp_path)
    assert Runtime(root="assets").root == tmp_path / "assets"
    assert not (tmp_path / "implicit").exists()


def test_custom_backends_receive_independent_roots_and_restore_assets(tmp_path, runtime):
    backend_class = type(runtime._backends["test"])
    other = Runtime(root=tmp_path / "other")
    other.register(name="test", backend=backend_class)
    assert runtime._backends["test"].root == runtime.root
    assert other._backends["test"].root == other.root
    assert runtime._backends["test"] is not other._backends["test"]

    image = tmp_path / "image"
    image.mkdir()
    (image / "state.txt").write_text("initial")
    for manager in (runtime, other):
        ref = manager.create(backend="test", image=str(image))
        assert isinstance(ref, RuntimeRef)
        instance = manager.start(ref=ref)
        assert instance.root == str(manager.root)
        saved = manager.save(instance=instance)
        assert Path(saved.image).is_relative_to(manager.root)
        assert manager.close(instance=instance) is None
        restored = manager.start(ref=saved)
        assert (Path(restored.data["path"]) / "state.txt").read_text() == "initial"
        manager.close(instance=restored)


def test_close_preserves_backend_failure(runtime, monkeypatch):
    from envweaver import Instance

    instance = Instance("live", "test", str(runtime.root))
    error = OSError("failed to release instance")

    def fail(instance):
        raise error

    monkeypatch.setattr(runtime._backends["test"], "close", fail)
    with pytest.raises(OSError) as failure:
        runtime.close(instance=instance)
    assert failure.value is error


@pytest.mark.parametrize(
    "host",
    [
        "",
        "casia23",
        "tcp://server:2375",
        "ssh://",
        "ssh://u:secret@server",
        "ssh://server/path",
        "ssh://server:bad",
    ],
)
def test_invalid_hosts_are_rejected(tmp_path, host):
    with pytest.raises(ValueError):
        Runtime(root=tmp_path, host=host).create(backend="docker", image="image")


def test_start_uses_only_runtime_host(tmp_path, monkeypatch):
    from envweaver import Instance
    from envweaver.runtime.docker import DockerBackend

    def start(self, ref):
        return Instance(
            "container", "docker", str(self.root), {"ref": ref.to_dict()}, host=self.host
        )

    monkeypatch.setattr(DockerBackend, "start", start)
    remote = Runtime(root=tmp_path, host="ssh://server-a")
    base = RuntimeRef("docker", "python")
    instance = remote.start(base)
    assert instance.host == "ssh://server-a"
    assert "host" not in base.to_dict()
    path = tmp_path / "instance.json"
    remote.describe(instance=instance, path=path)
    import json

    assert json.loads(path.read_text()) == {
        "version": 1,
        "backend": "docker",
        "container_id": "container",
        "host": "ssh://server-a",
    }
    saved = RuntimeRef("docker", "sha256:saved")
    assert Runtime(root=tmp_path).start(saved).host is None
    assert Runtime(root=tmp_path, host="ssh://server-b").start(saved).host == "ssh://server-b"
    with pytest.raises(ValueError, match="absolute host paths"):
        remote.start(RuntimeRef("qemu", "base.qcow2", {"disk_dir": "/data/disks"}))
    assert (
        Instance.from_dict({"id": "old", "backend": "docker", "root": str(tmp_path)}).host is None
    )
    assert RuntimeRef.from_dict({"backend": "docker", "image": "old"}) == RuntimeRef(
        "docker", "old"
    )
    with pytest.raises(TypeError, match="host"):
        RuntimeRef("docker", "image", host="ssh://server-a")
    with pytest.raises(TypeError, match="host"):
        RuntimeRef.from_dict({"backend": "docker", "image": "image", "host": "ssh://server-a"})


@pytest.mark.parametrize("host", [None, "ssh://server"])
@pytest.mark.parametrize("as_path", [False, True])
def test_start_rejects_json_paths_before_backend_calls(tmp_path, monkeypatch, host, as_path):
    path = tmp_path / "runtime.json"
    path.write_text('{"backend": "docker", "image": "python"}')

    def unexpected_backend(*args):
        pytest.fail("A file path must be rejected before selecting a backend")

    monkeypatch.setattr(Runtime, "_backend", unexpected_backend)
    ref = path if as_path else str(path)
    with pytest.raises(TypeError, match="ref must be a RuntimeRef object"):
        Runtime(root=tmp_path, host=host).start(ref=ref)


def test_registered_backend_can_reconnect_without_storage(tmp_path, runtime):
    image = tmp_path / "base"
    image.mkdir()
    (image / "state").write_text("data")
    original = runtime.start(runtime.create(backend="test", image=str(image)))
    record = tmp_path / "instance.json"
    runtime.describe(original, record)
    other = Runtime()
    other.register(name="test", backend=type(runtime._backends["test"]))
    restored = other.connect(record)
    assert restored.root is None
    assert restored.id == original.id
    assert Path(restored.data["path"], "state").read_text() == "data"
    other.close(restored)


@pytest.mark.parametrize("host", [None, "ssh://server"])
def test_qemu_connection_defaults_merge_and_do_not_mutate(tmp_path, monkeypatch, host):
    from copy import deepcopy

    from envweaver import Instance
    from envweaver.runtime.qemu import QemuBackend

    received = []
    defaults = {"ssh": {"user": "worker", "python": "/opt/python"}}
    overrides = {"ssh": {"user": "evaluator", "identity_file": str(tmp_path / "key")}}
    original = deepcopy(overrides)
    ref = RuntimeRef("qemu", "/images/base.qcow2", {"disk_dir": "/disks"}, defaults)

    def started(ref, connection):
        received.append(deepcopy(connection))
        return Instance("vm", "qemu", data={"ssh": connection["ssh"]})

    monkeypatch.setattr(
        QemuBackend, "_start", lambda self, ref, connection: started(ref, connection)
    )

    def remote(self, action, host, config, **payload):
        assert action == "start"
        return started(RuntimeRef.from_dict(payload["ref"]), payload["connection"]).to_dict()

    monkeypatch.setattr(QemuBackend, "_call_remote", remote)
    monkeypatch.setattr(QemuBackend, "_wait_ready", lambda *_: None)
    manager = Runtime(host=host)
    instance = manager.start(ref=ref, connection=overrides)
    assert received == [
        {
            "ssh": {
                "user": "evaluator",
                "python": "/opt/python",
                "identity_file": str(tmp_path / "key"),
            }
        }
    ]
    assert instance.data["ssh"]["user"] == "evaluator"
    assert ref.connection == defaults == {"ssh": {"user": "worker", "python": "/opt/python"}}
    assert overrides == original


@pytest.mark.parametrize(
    "backend,connection",
    [
        ("qemu", {"ssh": {"identity_file": "/keys/private"}}),
        ("qemu", {"ssh": {"host": "producer"}}),
        ("docker", {"vnc": {"password": "secret"}}),
    ],
)
def test_create_rejects_credentials_and_addresses_before_backend_call(backend, connection):
    with pytest.raises(ValueError, match="fixed"):
        Runtime().create(backend=backend, image="base", connection=connection)
