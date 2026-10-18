import json

import pytest

from envweaver import Instance, Runtime, RuntimeRef
from envweaver.errors import RuntimeError
from envweaver.runtime.docker import DockerBackend


def test_save_pins_image_id_without_export_and_close_keeps_asset(tmp_path, monkeypatch):
    backend = DockerBackend(tmp_path)
    instance = Instance(
        "live",
        "docker",
        str(tmp_path),
        {
            "ref": RuntimeRef("docker", "base", {"memory": "4g"}).to_dict(),
            "vnc": {
                "host": "desktop",
                "port": 5901,
                "password_env": "VNC_SECRET",
                "password": "secret",
            },
        },
    )
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        assert args[0] in {"commit", "image", "rm"}
        if args[0] == "commit":
            return "CLI notice\nsha256:saved"
        return "sha256:saved"

    monkeypatch.setattr(backend, "_docker", docker)
    monkeypatch.setattr(backend, "exec", lambda *args: (0, "", ""))
    saved = backend.save(instance)
    assert saved.image == "sha256:saved"
    assert saved.config == {"memory": "4g"}
    assert saved.connection == {"vnc": {"password_env": "VNC_SECRET"}}
    asset = next((tmp_path / "runtimes").iterdir())
    assert {p.name for p in asset.iterdir()} == {"runtime.json", "metadata.json"}
    assert json.loads((asset / "runtime.json").read_text()) == saved.to_dict()
    metadata = json.loads((asset / "metadata.json").read_text())
    assert metadata == {"tag": f"envweaver/snapshot:{asset.name}", "host": None}
    assert calls[0] == (
        "commit",
        "live",
        metadata["tag"],
    )
    assert calls[1] == ("image", "inspect", "--format", "{{.Id}}", metadata["tag"])
    backend.close(instance)
    assert calls[-1] == ("rm", "--force", "live")
    assert (asset / "runtime.json").exists()


@pytest.mark.parametrize("available", [True, False])
def test_start_uses_image_without_importing_archive(tmp_path, monkeypatch, available):
    backend = DockerBackend(tmp_path)
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "run" and not available:
            raise RuntimeError("No such image")
        if args[0] == "inspect":
            return json.dumps([{"State": {"Running": True}, "Mounts": []}])
        return "container-id"

    monkeypatch.setattr(backend, "_docker", docker)
    monkeypatch.setattr(backend, "exec", lambda *args: (0, "", ""))
    ref = RuntimeRef("docker", "snapshot")
    if available:
        assert backend.start(ref).id == "container-id"
    else:
        with pytest.raises(RuntimeError, match="No such image"):
            backend.start(ref)
    assert calls[0][0] == "run"
    assert calls[0][-1] == "snapshot"
    assert not any(c[0] == "load" for c in calls)


def test_cli_connection_is_explicit_and_does_not_change_process_environment(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace

    from envweaver.runtime import docker

    monkeypatch.setenv("DOCKER_HOST", "ssh://wrong-server")
    monkeypatch.setenv("DOCKER_CONTEXT", "wrong-context")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(docker.subprocess, "run", run)
    backend = DockerBackend(tmp_path)
    backend._docker("info", host="ssh://user@server-a:2222")
    backend._docker("info", host="ssh://server-b")
    backend._docker("info")
    assert calls[0][0] == ["docker", "--host", "ssh://user@server-a:2222", "info"]
    assert calls[1][0] == ["docker", "--host", "ssh://server-b", "info"]
    assert calls[2][0] == ["docker", "--context", "default", "info"]
    assert all(
        not any(k in kwargs["env"] for k in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY"))
        for _, kwargs in calls
    )
    assert os.environ["DOCKER_HOST"] == "ssh://wrong-server"


def test_every_operation_uses_instance_host_even_with_different_manager(tmp_path, monkeypatch):
    from envweaver import Runtime

    calls = []

    def docker(self, *args, host=None, **kwargs):
        calls.append((args, host))
        if args[0] == "exec":
            if args[2] == "python3" and len(args) == 6:
                command = json.loads(args[-1])["command"]
                return json.dumps(
                    {
                        "exit_code": 1 if command.startswith("test -d") else 0,
                        "stdout": "",
                        "stderr": "",
                    }
                )
            return "0:0"
        return "sha256:saved"

    monkeypatch.setattr(DockerBackend, "_docker", docker)
    instance = Instance(
        "remote",
        "docker",
        str(tmp_path),
        {"ref": RuntimeRef("docker", "base").to_dict()},
        host="ssh://server-a",
    )
    manager = Runtime(root=tmp_path, host="ssh://server-b")
    source = tmp_path / "input.txt"
    source.write_text("data")
    manager.exec(instance, "true")
    manager.upload(instance, source, "/input.txt")
    manager.download(instance, "/input.txt", tmp_path / "output.txt")
    saved = manager.save(instance)
    assert "host" not in saved.to_dict()
    record = next((tmp_path / "runtimes").glob("*/runtime.json"))
    assert json.loads(record.read_text()) == saved.to_dict()
    assert json.loads(record.with_name("metadata.json").read_text())["host"] == instance.host
    manager.close(instance)
    assert {host for _, host in calls} == {"ssh://server-a"}
    assert {args[0] for args, _ in calls} >= {"exec", "cp", "commit", "image", "rm"}


@pytest.mark.parametrize(
    "mounts,tmpfs",
    [([{"Type": "volume", "Destination": "/inputs"}], {}), ([], {"/scratch": "size=16m"})],
)
def test_connected_mounts_allow_commands_but_not_save(tmp_path, monkeypatch, mounts, tmpfs):
    runtime = Runtime()
    backend = runtime._backend("docker")
    native = {
        "Id": "external",
        "Image": "sha256:base",
        "State": {"Running": True},
        "Mounts": mounts,
        "Config": {"Cmd": ["sleep", "infinity"]},
        "HostConfig": {"Tmpfs": tmpfs},
    }

    def docker(*args, **kwargs):
        assert args == ("inspect", "external")  # Never reach commit for mounted data.
        return json.dumps([native])

    monkeypatch.setattr(backend, "_docker", docker)
    monkeypatch.setattr(backend, "exec", lambda *a, **k: (0, "result", ""))
    record = tmp_path / "instance.json"
    record.write_text(json.dumps({"version": 1, "backend": "docker", "container_id": "external"}))
    instance = runtime.connect(path=record)
    assert runtime.exec(instance, "cat /inputs/result") == (0, "result", "")
    with pytest.raises(ValueError, match="returned by connect"):
        runtime.save(instance)


@pytest.mark.parametrize("python", [None, "/opt/venv/bin/python"])
def test_connect_does_not_reconstruct_startup_options(tmp_path, monkeypatch, python):
    runtime = Runtime()
    backend = runtime._backend("docker")
    native = {
        "Id": "external",
        "Image": "sha256:base",
        "State": {"Running": True},
        "Config": {"Env": ["SECRET=do-not-copy"]},
        "HostConfig": {
            "DeviceRequests": [{"Driver": "custom-device-driver"}],
            "PortBindings": {"80/tcp": [{"HostPort": "8080"}]},
        },
    }
    monkeypatch.setattr(backend, "_docker", lambda *a, **k: json.dumps([native]))
    monkeypatch.setattr(backend, "exec", lambda *a, **k: (0, "", ""))
    record = {"version": 1, "backend": "docker", "container_id": "external"}
    if python:
        record["python"] = python
    path = tmp_path / "instance.json"
    path.write_text(json.dumps(record))
    instance = runtime.connect(path=path)
    runtime.describe(instance=instance, path=path)
    assert json.loads(path.read_text()) == {**record, "host": None}
    assert instance.data["ref"]["config"] == ({"python": python} if python else {})
    with pytest.raises(ValueError, match="returned by connect"):
        runtime.save(instance)
