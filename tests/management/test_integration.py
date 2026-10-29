"""Opt-in real asset lifecycle; creates and removes only its own resources."""

import io
import json
import os
import subprocess
import tarfile
import time
import uuid

import pytest

from tests.management.helpers import seed_runtime

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from tests.management.helpers import configured_app
from tests.management.test_api import install_task, listing, package, plan

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("ENVWEAVER_MANAGEMENT_INTEGRATION") != "1",
        reason="opt-in real Docker and QEMU asset operations",
    ),
]


def command(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs).stdout.decode().strip()


def finished(client, response):
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        row = client.get("/api/jobs/" + response.json()["id"]).json()
        if row["status"] not in ["pending", "running"]:
            return row
        time.sleep(0.05)
    pytest.fail("Operation timed out")


def test_real_docker_in_use_protection(tmp_path):
    tag = "ew-management-test:" + uuid.uuid4().hex
    content = b"management integration fixture"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("fixture")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    image = command(
        "docker", "--context", "default", "image", "import", "-", tag, input=buffer.getvalue()
    )
    container = None
    try:
        ref = {"backend": "docker", "image": tag, "config": {}, "connection": {}}
        with TestClient(configured_app(tmp_path / "consumer")) as consumer:
            install_task(consumer, package(tmp_path / "task", ref=ref))
            runtime = listing(consumer, "runtime")["items"][0]
            container = command(
                "docker",
                "--context",
                "default",
                "create",
                "--label",
                "envweaver.test=management",
                image,
                "/noop",
            )
            preview = plan(consumer, [runtime["id"]], physical=True)
            failed = finished(
                consumer,
                consumer.post(
                    "/api/delete/confirm",
                    json={"token": preview["token"], "acknowledge_dependencies": True},
                ),
            )
            assert failed["status"] == "partial", failed
            command("docker", "--context", "default", "rm", container)
            container = None
            preview = plan(consumer, [runtime["id"]], physical=True)
            removed = finished(
                consumer,
                consumer.post(
                    "/api/delete/confirm",
                    json={"token": preview["token"], "acknowledge_dependencies": True},
                ),
            )
            assert removed["status"] == "completed", removed
            assert listing(consumer)["items"][0]["state"] == "missing"
    finally:
        if container:
            subprocess.run(
                ["docker", "--context", "default", "rm", "-f", container], capture_output=True
            )
        subprocess.run(["docker", "--context", "default", "image", "rm", tag], capture_output=True)
        subprocess.run(
            ["docker", "--context", "default", "image", "rm", image], capture_output=True
        )


def test_real_qemu_backing_chain_and_deletion(tmp_path):
    base, overlay = tmp_path / "base.qcow2", tmp_path / "overlay.qcow2"
    command("qemu-img", "create", "-f", "qcow2", str(base), "8M")
    alias = tmp_path / "base-alias.qcow2"
    alias.symlink_to(base)
    command("qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(alias), str(overlay))
    firmware, nvram = tmp_path / "firmware", tmp_path / "nvram"
    firmware.write_bytes(b"firmware fixture")
    nvram.write_bytes(b"nvram fixture")
    ref = {
        "backend": "qemu",
        "image": str(overlay),
        "config": {"firmware": str(firmware), "nvram": str(nvram)},
        "connection": {"ssh": {"user": "tester"}},
    }
    with TestClient(configured_app(tmp_path / "producer")) as client:
        task_id = install_task(client, package(tmp_path / "task", ref=ref))
        parent = seed_runtime(
            client, json={"name": "base", "ref": {**ref, "image": str(base)}}
        ).json()
        preview = plan(client, [parent["id"]], physical=True)
        assert preview["affected_tasks"] == [{"id": task_id, "name": "sample"}]
        assert len(preview["affected_runtimes"]) == 1
        overlay_record = next(
            row
            for row in listing(client, "runtime")["items"]
            if row["spec"]["image"] == str(overlay)
        )
        preview = plan(client, [overlay_record["id"]], physical=True)
        removed = finished(
            client,
            client.post(
                "/api/delete/confirm",
                json={"token": preview["token"], "acknowledge_dependencies": True},
            ),
        )
        assert removed["status"] == "completed", removed
        assert not overlay.exists()
        assert base.exists() and firmware.exists() and nvram.exists()


def test_real_ssh_pool_operations(tmp_path, monkeypatch):
    import getpass
    import shlex
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    hostkey, clientkey = tmp_path / "host-key", tmp_path / "client-key"
    for key in [hostkey, clientkey]:
        command("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key))
    known = tmp_path / "known_hosts"
    known.write_text(f"[127.0.0.1]:{port} " + hostkey.with_suffix(".pub").read_text())
    config = tmp_path / "sshd_config"
    config.write_text(f"""Port {port}
ListenAddress 127.0.0.1
HostKey {hostkey}
PidFile {tmp_path}/sshd.pid
AuthorizedKeysFile {clientkey}.pub
StrictModes no
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
""")
    with (tmp_path / "sshd.log").open("w") as log:
        server = subprocess.Popen(
            ["/usr/sbin/sshd", "-D", "-e", "-f", str(config)], stdout=log, stderr=log
        )
        try:
            for _ in range(100):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.02)
            wrapper = tmp_path / "bin"
            wrapper.mkdir()
            ssh = wrapper / "ssh"
            options = [
                "/usr/bin/ssh",
                "-i",
                str(clientkey),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                f"UserKnownHostsFile={known}",
                "-o",
                "StrictHostKeyChecking=yes",
            ]
            ssh.write_text("#!/bin/sh\nexec " + shlex.join(options) + ' "$@"\n')
            ssh.chmod(0o700)
            monkeypatch.setenv("PATH", str(wrapper) + os.pathsep + os.environ["PATH"])
            host = f"ssh://{getpass.getuser()}@127.0.0.1:{port}"
            with TestClient(configured_app(tmp_path / "managed")) as client:
                from tests.management.test_pools import add_pool

                remote_root = tmp_path / "remote-tasks"
                package(remote_root / "remote-task", name="remote-task")
                pool_id = add_pool(client, host + str(remote_root))
                rows = listing(client, pool_id=pool_id)["items"]
                assert len(rows) == 1
                row = rows[0]
                response = client.patch(
                    "/api/assets/" + row["id"],
                    json={
                        "revision": row["revision"],
                        "metadata": {"remote": True, "tags": ["remote"]},
                    },
                )
                assert response.status_code == 200, response.text
                assert json.loads((remote_root / "remote-task/task.json").read_text())[
                    "metadata"
                ] == {"remote": True, "tags": ["remote"]}
                # Multiple files travel in one remote write operation.
                from envweaver.management import pools as pool_module

                for name in ("remote-second", "remote-third"):
                    package(remote_root / name, name=name)
                remote_calls = []
                original_ssh = pool_module.ssh

                def counted_ssh(host, command, payload, **kwargs):
                    remote_calls.append(payload["action"])
                    return original_ssh(host, command, payload, **kwargs)

                monkeypatch.setattr(pool_module, "ssh", counted_ssh)
                changed = client.post(
                    "/api/assets/tags",
                    json={
                        "selection": {"kind": "task", "pool_id": pool_id},
                        "add": ["ssh-tag"],
                        "remove": ["remote"],
                    },
                )
                assert changed.status_code == 200, changed.text
                assert changed.json()["updated"] == 3
                assert remote_calls.count("asset_metadata_batch") == 1
                for path in remote_root.glob("*/task.json"):
                    assert json.loads(path.read_text())["metadata"]["tags"] == ["ssh-tag"]
                assert client.get(f"/api/tasks/{row['id']}/files").status_code == 200
                assert (
                    client.get(
                        f"/api/tasks/{row['id']}/file", params={"path": "context/input.txt"}
                    ).content
                    == b"material"
                )
                runtime_root = tmp_path / "remote-runtime"
                runtime_root.mkdir()
                runtime_pool_id = add_pool(client, host + str(runtime_root), kind="runtime")
                created = seed_runtime(
                    client,
                    params={"pool_id": runtime_pool_id},
                    json={
                        "name": "remote-ref",
                        "ref": {"backend": "docker", "image": "test:remote"},
                        "host": host,
                    },
                )
                assert created.status_code == 200, created.text
                record = next(runtime_root.glob("runtimes/*/runtime.json"))
                assert json.loads(record.read_text())["image"] == "test:remote"
                row = created.json()
                tagged = client.patch(
                    f"/api/assets/{row['id']}",
                    json={"revision": row["revision"], "tags": ["ssh-tag"]},
                )
                assert tagged.status_code == 200, tagged.text
                assert json.loads(record.read_text())["metadata"]["tags"] == ["ssh-tag"]
                assert listing(client, "runtime", pool_id=runtime_pool_id)["total"] == 1
                preview = client.post(
                    "/api/delete/plan",
                    json={"selection": {"kind": "task", "pool_id": pool_id}, "physical": True},
                ).json()
                deleted = finished(
                    client, client.post("/api/delete/confirm", json={"token": preview["token"]})
                )
                assert deleted["status"] == "completed", deleted
                assert not (remote_root / "remote-task").exists()
        finally:
            server.terminate()
            server.wait(timeout=10)


def test_real_sdk_save_is_visible_without_import(tmp_path):
    from envweaver import Runtime, RuntimeRef, TaskPool, Verifier
    from tests.management.test_pools import add_pool

    image = "envweaver/tb21-verifier:20261028"
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode:
        pytest.skip("preinstalled Python verifier image is unavailable")
    runtime_root, task_root = tmp_path / "sdk-runtime", tmp_path / "sdk-tasks"
    runtime_root.mkdir()
    task_root.mkdir()
    runtime = Runtime(root=runtime_root)
    entry = tmp_path / "verifier/run.py"
    entry.parent.mkdir()
    entry.write_text("print('{\"score\":1}')")
    instance = None
    try:
        with TestClient(configured_app(tmp_path / "management")) as client:
            tid = add_pool(client, task_root)
            rid = add_pool(client, runtime_root, kind="runtime")
            instance = runtime.start(
                ref=runtime.create(
                    backend="docker",
                    image=image,
                    metadata={"tags": ["sdk"], "custom": {"ok": True}},
                )
            )
            saved = runtime.save(instance=instance)
            assert saved.metadata == {"tags": ["sdk"], "custom": {"ok": True}}
            environment = listing(client, "runtime", pool_id=rid)["items"][0]
            assert environment["tags"] == ["sdk"]
            edited = client.patch(
                f"/api/assets/{environment['id']}",
                json={"revision": environment["revision"], "tags": ["ui"]},
            )
            assert edited.status_code == 200, edited.text
            runtime_record = next(runtime_root.glob("runtimes/*/runtime.json"))
            assert RuntimeRef.from_dict(json.loads(runtime_record.read_text())).metadata[
                "tags"
            ] == ["ui"]
            assert (
                listing(client, "runtime", pool_id=rid)["items"][0]["spec"]["image"] == saved.image
            )
            pool = TaskPool(root=task_root, runtime=runtime)
            path = pool.save(
                instance=instance,
                name="sdk-end-to-end",
                goal=[{"type": "text", "text": "Inspect workspace"}],
                verifier=Verifier(entry=entry, runtime=runtime, timeout=30),
                verifier_runtime_ref=saved,
                cwd="/",
            )
            row = listing(client, pool_id=tid)["items"][0]
            assert row["name"] == pool.load(task_ref=path).name
            changed = client.patch(
                "/api/assets/" + row["id"],
                json={"revision": row["revision"], "metadata": {"checked": True}},
            )
            assert changed.status_code == 200, changed.text
            assert pool.load(task_ref=path).metadata == {"checked": True}
    finally:
        if instance is not None:
            runtime.close(instance=instance)
        for record in runtime_root.glob("runtimes/*/metadata.json"):
            tag = json.loads(record.read_text()).get("tag")
            if tag:
                command("docker", "--context", "default", "image", "rm", tag)
