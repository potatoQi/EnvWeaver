import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from envweaver import Instance, Runtime, RuntimeRef
from envweaver._io import ssh_transport as _ssh_transport
from envweaver._io.ssh_config import docker_environment, flags
from envweaver.runtime import _remote, _ssh
from envweaver.runtime.docker import DockerBackend
from envweaver.runtime.qemu import QemuBackend


@pytest.fixture
def options(tmp_path):
    directory = tmp_path / "experiment ssh"
    directory.mkdir()
    config = directory / "config"
    config.write_text(
        "Host *\n    IdentitiesOnly yes\n    IdentityAgent none\n"
        "    ForwardAgent no\n    ControlMaster no\n    ControlPath none\n"
    )
    known = directory / "known_hosts"
    known.write_text("# experiment hosts\n")
    (directory / "key").write_text("test key placeholder\n")
    return {
        "config_file": str(config),
        "known_hosts_file": str(known),
        "identity_file": str(directory / "key"),
    }


@pytest.mark.skipif(not shutil.which("ssh"), reason="OpenSSH required")
def test_openssh_reads_experiment_configuration(options):
    known = Path(options["known_hosts_file"])
    before = known.read_bytes()
    result = subprocess.run(
        ["ssh", *flags(options), "-G", "example.invalid"],
        capture_output=True,
        text=True,
        check=True,
    )
    values = dict(line.split(" ", 1) for line in result.stdout.splitlines())
    assert values["identityagent"] == "none"
    assert values["identitiesonly"] == "yes"
    assert values["forwardagent"] == "no"
    assert values["controlmaster"] == "false"
    assert values["identityfile"] == options["identity_file"]
    assert options["known_hosts_file"] in values["userknownhostsfile"]
    assert known.read_bytes() == before


@pytest.mark.parametrize(
    "field", ["config_file", "known_hosts_file", "identity_file", "host_key_alias"]
)
@pytest.mark.parametrize("value", [None, "", " \t", "a\nb", "a\x00b", 42])
def test_invalid_ssh_fields_fail_before_connect(field, value):
    with pytest.raises(ValueError, match=field):
        Runtime(ssh={field: value})


def test_paths_are_resolved_without_reading_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = {"identity_file": Path("private_key"), "known_hosts_file": "hosts"}
    runtime = Runtime(ssh=config)
    monkeypatch.chdir(tmp_path.parent)
    assert runtime.ssh == {
        "identity_file": str(tmp_path / "private_key"),
        "known_hosts_file": str(tmp_path / "hosts"),
    }
    assert config["identity_file"] == Path("private_key")
    assert not (tmp_path / "private_key").exists()


def test_missing_private_key_never_falls_back_to_personal_keys(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Missing explicit private key must fail before invoking SSH")

    monkeypatch.setattr(_ssh_transport.subprocess, "run", unexpected)
    with pytest.raises(FileNotFoundError):
        _ssh_transport.ssh(
            "ssh://server", ["true"], {}, ssh_options={"identity_file": str(tmp_path / "missing")}
        )


def test_docker_ssh_wrapper_is_scoped_and_cleans_up(tmp_path, options):
    # Exercise the same PATH lookup Docker uses, without contacting a daemon.
    directory = tmp_path / "bin"
    directory.mkdir()
    executable = directory / "ssh"
    executable.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    executable.chmod(0o700)
    env = {**os.environ, "PATH": str(directory) + os.pathsep + os.environ["PATH"]}
    original_path = os.environ["PATH"]
    with pytest.raises(RuntimeError, match="test cleanup"):
        with docker_environment(env, options) as child_env:
            wrapper = Path(child_env["PATH"].split(os.pathsep)[0]) / "ssh"
            output = subprocess.check_output(
                ["ssh", "server", "docker system dial-stdio"], env=child_env
            )
            assert output.decode().splitlines() == [
                *flags(options),
                "server",
                "docker system dial-stdio",
            ]
            assert os.environ["PATH"] == original_path
            raise RuntimeError("test cleanup")
    assert not wrapper.exists()
    assert env["PATH"].startswith(str(directory))


def test_server_and_guest_use_separate_ssh_options(options, monkeypatch, tmp_path):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'{"result": {}}', stderr=b"")

    monkeypatch.setattr(_ssh_transport.subprocess, "run", run)
    _remote.call("ssh://worker@server:2222", "python3", {}, timeout=10, ssh_options=options)
    assert calls[-1][0][1 : 1 + len(flags(options))] == flags(options)
    guest = {**options, "identity_file": str(tmp_path / "guest_key"), "host_key_alias": "vm-one"}
    Path(guest["identity_file"]).write_text("guest key placeholder\n")
    _ssh.request(
        {
            **guest,
            "host": "127.0.0.1",
            "port": 2200,
            "user": "guest",
            "via": "ssh://worker@server:2222",
            "via_ssh": options,
        },
        "health",
    )
    args = calls[-1][0]
    assert args[1 : 1 + len(flags(guest))] == flags(guest)
    proxy = next(
        item.removeprefix("ProxyCommand=") for item in args if item.startswith("ProxyCommand=")
    )
    assert shlex.split(proxy) == [
        "ssh",
        *flags(options),
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-p",
        "2222",
        "-W",
        "%h:%p",
        "--",
        "worker@server",
    ]
    assert "-J" not in args


def test_docker_reconnect_remembers_server_options(tmp_path, options, monkeypatch):
    native = {
        "Id": "container",
        "Image": "sha256:base",
        "State": {"Running": True},
        "Config": {"Cmd": ["sleep", "infinity"]},
        "HostConfig": {},
        "Mounts": [],
    }
    calls = []

    def docker(self, *args, **kwargs):
        calls.append(kwargs)
        if args[0] == "inspect":
            return json.dumps([native])
        return '{"exit_code": 0, "stdout": "ok", "stderr": ""}'

    monkeypatch.setattr(DockerBackend, "_docker", docker)
    descriptor = tmp_path / "instance.json"
    descriptor.write_text(
        json.dumps(
            {
                "version": 1,
                "backend": "docker",
                "host": "ssh://server",
                "container_id": "container",
                "host_ssh": options,
            }
        )
    )
    instance = Runtime().connect(path=descriptor)
    assert calls[0]["ssh"] == options
    Runtime(ssh={"identity_file": str(tmp_path / "wrong")}).exec(instance, "true")
    assert calls[-1]["ssh"] == options
    Runtime().describe(instance=instance, path=descriptor)
    assert json.loads(descriptor.read_text())["host_ssh"] == options
    override = {"config_file": str(tmp_path / "other")}
    assert Runtime(ssh=override).connect(path=descriptor).data["host_ssh"] == {**options, **override}
    reset = Runtime(ssh={}).connect(path=descriptor)
    assert reset.data["host_ssh"] == {}
    assert calls[-1]["ssh"] == {}
    assert json.loads(descriptor.read_text())["host_ssh"] == options


def test_qemu_remote_calls_keep_instance_server_options(tmp_path, options, monkeypatch):
    calls = []

    def call(host, python, payload, **kwargs):
        calls.append((host, kwargs))
        return {}

    monkeypatch.setattr(_remote, "call", call)
    backend = QemuBackend(ssh={"identity_file": str(tmp_path / "other")})
    instance = Instance("vm", "qemu", data={"host_ssh": options}, host="ssh://server")
    backend._call_remote("close", instance.host, {}, instance=instance.to_dict())
    assert calls[-1][1]["ssh_options"] == options


def test_remote_records_use_default_ssh(tmp_path, options, monkeypatch):
    from envweaver._io import storage as _storage

    calls = []

    def ssh(host, command, payload, **kwargs):
        calls.append((host, kwargs))
        return b"null"

    monkeypatch.setattr(_ssh_transport, "ssh", ssh)
    _storage.write_records("ssh://server/records", "one", {})
    assert calls[-1] == ("ssh://server", {})


def test_guest_options_do_not_change_saved_runtime_config(tmp_path, options, monkeypatch):
    def start(self, ref, connection):
        assert connection["ssh"]["known_hosts_file"] == options["known_hosts_file"]
        return Instance("vm", "qemu", data={"ref": ref.to_dict(), "ssh": connection["ssh"]})

    monkeypatch.setattr(QemuBackend, "_start", start)
    runtime = Runtime(ssh=options)
    ref = RuntimeRef("qemu", "/base.qcow2", {"disk_dir": "/disks"})
    instance = runtime.start(ref=ref, connection={"ssh": {"user": "worker", **options}})
    assert instance.data["host_ssh"] == options
    assert instance.data["ref"]["config"] == {"disk_dir": "/disks"}
    assert ref.config == {"disk_dir": "/disks"}


def test_remote_qemu_start_describe_connect_preserves_both_connections(
    tmp_path, options, monkeypatch
):
    calls = []
    guest = {"user": "worker", **options, "host_key_alias": "vm-one"}

    def start(self, ref, connection):
        return Instance(
            "vm",
            "qemu",
            data={
                "ref": ref.to_dict(),
                "qmp": "/disks/vm/qmp.sock",
                "ssh": {**connection["ssh"], "host": "127.0.0.1", "port": 2200},
            },
        )

    def connect(self, record):
        return Instance(
            "vm",
            "qemu",
            data={
                "qmp": record["qmp"],
                "ssh": record["ssh"],
                "ref": RuntimeRef("qemu", "/base", record["startup"]).to_dict(),
            },
        )

    def call(host, python, payload, **kwargs):
        calls.append(kwargs.get("ssh_options", {}))
        return _remote.dispatch(payload)

    monkeypatch.setattr(QemuBackend, "_start", start)
    monkeypatch.setattr(QemuBackend, "_connect", connect)
    monkeypatch.setattr(QemuBackend, "_wait_ready", lambda *a: None)
    monkeypatch.setattr(_ssh, "request", lambda *a, **k: {})
    monkeypatch.setattr(_remote, "call", call)
    runtime = Runtime(root=tmp_path / "records", host="ssh://server", ssh=options)
    instance = runtime.start(
        ref=RuntimeRef("qemu", "/base", {"disk_dir": "/disks"}), connection={"ssh": guest}
    )
    path = tmp_path / "instance.json"
    runtime.describe(instance=instance, path=path)
    record = json.loads(path.read_text())
    assert record["host_ssh"] == options
    assert "via_ssh" not in record["ssh"]
    assert record["ssh"]["identity_file"] == guest["identity_file"]
    restored = Runtime().connect(path=path)
    assert restored.data["host_ssh"] == options
    assert restored.data["ssh"]["via_ssh"] == options
    assert restored.data["ssh"]["host_key_alias"] == "vm-one"
    assert calls == [options, options]
    reset = Runtime(ssh={}).connect(path=path)
    assert reset.data["host_ssh"] == {}
    assert reset.data["ssh"]["via_ssh"] == {}
    assert reset.data["ssh"]["identity_file"] == guest["identity_file"]
    assert calls[-1] == {}
    override = {"known_hosts_file": str(tmp_path / "other_hosts")}
    merged = Runtime(ssh=override).connect(path=path)
    assert merged.data["host_ssh"] == {**options, **override}
    assert merged.data["ssh"]["via_ssh"] == {**options, **override}
    assert merged.data["ssh"]["identity_file"] == guest["identity_file"]
    assert calls[-1] == {**options, **override}
    assert json.loads(path.read_text()) == record


@pytest.mark.parametrize("backend", ["docker", "qemu"])
def test_record_server_uses_default_ssh_independently_of_sandbox(backend, options, monkeypatch):
    file_calls, server_calls = [], []
    ref = RuntimeRef(backend, "/base", {"disk_dir": "/disks"})

    def file_ssh(host, command, payload, **kwargs):
        assert host == "ssh://files"
        assert kwargs == {}
        file_calls.append(payload["action"])
        return b"null"

    def docker(self, *args, **kwargs):
        assert kwargs.get("host") == "ssh://worker"
        assert kwargs.get("ssh", self.ssh) == options
        server_calls.append(args[0])
        if args[0] == "inspect":
            return json.dumps([{"State": {"Running": True}, "Mounts": [], "HostConfig": {}}])
        if args[0] == "exec":
            return '{"exit_code": 0, "stdout": "", "stderr": ""}'
        return "container"

    def qemu(host, python, payload, **kwargs):
        assert host == "ssh://worker"
        assert kwargs["ssh_options"] == options
        server_calls.append(payload["action"])
        if payload["action"] == "start":
            return Instance(
                "vm", "qemu", root="ssh://files/records",
                data={"ref": ref.to_dict(), "ssh": {"user": "guest"}, "qmp": "/disks/qmp"},
            ).to_dict()
        if payload["action"] == "save":
            return ref.to_dict()
        return None

    monkeypatch.setattr(_ssh_transport, "ssh", file_ssh)
    monkeypatch.setattr(DockerBackend, "_docker", docker)
    monkeypatch.setattr(_remote, "call", qemu)
    monkeypatch.setattr(QemuBackend, "_wait_ready", lambda *args: None)
    runtime = Runtime(root="ssh://files/records", host="ssh://worker", ssh=options)
    instance = runtime.start(ref=ref)
    runtime.save(instance=instance)
    runtime.close(instance=instance)
    assert file_calls == (["records"] if backend == "docker" else ["records", "records", "remove"])
    assert server_calls
