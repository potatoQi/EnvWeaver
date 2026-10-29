"""Real filesystem, SSH and Docker acceptance; no paid model requests."""

import getpass
import json
import os
import shlex
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from envweaver import EvalConfig, Runtime, RuntimeRef, Task, TrajPool, Verifier, eval
from envweaver._util import read_json
from envweaver.agents.trajectory import add_step, document
from envweaver.management.api import create_app
from tests.agents.helpers import CooperativeAgent
from tests.trajectory.test_management import choose, listing
from tests.trajectory.test_pool import rollout

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ENVWEAVER_TRAJ_INTEGRATION") != "1", reason="opt-in real SSH/Docker"
    ),
]


@pytest.fixture
def ssh_host(tmp_path, monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    hostkey, clientkey = tmp_path / "host-key", tmp_path / "client-key"
    for key in [hostkey, clientkey]:
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
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
                assert server.poll() is None, (tmp_path / "sshd.log").read_text()
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.02)
            wrapper = tmp_path / "bin"
            wrapper.mkdir()
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
            script = wrapper / "ssh"
            script.write_text("#!/bin/sh\nexec " + shlex.join(options) + ' "$@"\n')
            script.chmod(0o700)
            monkeypatch.setenv("PATH", str(wrapper) + os.pathsep + os.environ["PATH"])
            yield f"ssh://{getpass.getuser()}@127.0.0.1:{port}"
        finally:
            server.terminate()
            server.wait(timeout=10)


def test_real_ssh_pool_and_management(tmp_path, ssh_host):
    source = tmp_path / "source"
    rollout(source)
    path = source / "agent/trajectory.json"
    value = read_json(path)
    value["steps"][-1]["metrics"] = {"prompt_tokens": 100, "completion_tokens": 20}
    path.write_text(json.dumps(value))
    pool = TrajPool(ssh_host + str(tmp_path / "remote-pool"))
    ref = pool.save(output_dir=source)
    assert pool.save(output_dir=ssh_host + str(source)) == ref
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(lambda _: pool.save(output_dir=source), range(4))) == [ref] * 4
    stored = tmp_path / "remote-pool" / ref.split("/")[-2] / "result.json"
    assert list((tmp_path / "remote-pool").glob("*/result.json")) == [stored]
    assert read_json(stored)["verification_result"]["score"] == 0
    local = TrajPool(tmp_path / "round-trip")
    result = local.save(output_dir=ref.removesuffix("/result.json"))
    assert read_json(result)["id"] == read_json(stored)["id"]
    with TestClient(create_app()) as client:
        pool_id = choose(client, pool.root)
        row = listing(client, pool_id)["items"][0]
        assert row["input_tokens"] == 100 and row["output_tokens"] == 20
        response = client.post(
            "/api/assets/tags",
            json={"selection": {"kind": "trajectory", "pool_id": pool_id}, "add": ["remote"]},
        )
        assert response.status_code == 200, response.text
        assert "remote" in read_json(stored)["metadata"]["tags"]
        assert (
            client.get(
                f"/api/trajectories/{row['id']}/file", params={"path": "agent/trajectory.json"}
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/trajectories/export", json={"kind": "trajectory", "pool_id": pool_id}
            ).status_code
            == 200
        )


class SandboxRollout(CooperativeAgent):
    def run(self, *, goal, runtime, instance, output, cwd):
        code, stdout, stderr = runtime.exec(
            instance=instance, command=["python3", "-c", "print('sandbox works')"]
        )
        assert code == 0, stderr
        trajectory = document(output.result["metadata"], instance.id)
        add_step(trajectory, "user", goal[0]["text"])
        add_step(trajectory, "agent", stdout)
        output.save_trajectory(trajectory=trajectory)
        return output.finish([{"type": "text", "text": stdout}])


def test_real_docker_eval_to_pool(tmp_path):
    image = "envweaver/tb21-verifier:20261028"
    runtime = Runtime(root=tmp_path / "runtime")
    ref = RuntimeRef(
        "docker",
        image,
        {"command": ["sleep", "infinity"], "args": ["--label", "envweaver.traj-test=acceptance"]},
    )
    entry = tmp_path / "verifier/run.py"
    entry.parent.mkdir()
    entry.write_text('import json\nprint(json.dumps({"score": 1, "detail": "verification ran"}))\n')
    task = Task(
        name="traj-real",
        task_ref="acceptance-task",
        goal=[{"type": "text", "text": "run a command"}],
        cwd="/tmp",
        task_runtime_ref=ref,
        verifier_runtime_ref=ref,
        metadata={},
        context=[],
        verifier=Verifier(entry=entry, runtime=runtime, timeout=30, requires_task_instance=False),
    )
    pool = TrajPool(tmp_path / "pool")
    (result,) = eval(
        tasks=task,
        agent=SandboxRollout(model="deterministic"),
        config=EvalConfig(setup_timeout=120),
        output_dir=tmp_path / "run",
        traj_pool=pool,
    )
    assert result["verification_result"]["score"] == 1
    assert read_json(result["traj_ref"])["agent_result"]["metadata"]["model"] == "deterministic"
    assert not list((tmp_path / "runtime/instances").glob("*/instance.json"))
    containers = subprocess.check_output(
        [
            "docker",
            "--context",
            "default",
            "ps",
            "-aq",
            "--filter",
            "label=envweaver.traj-test=acceptance",
        ],
        text=True,
    )
    assert not containers.strip()
