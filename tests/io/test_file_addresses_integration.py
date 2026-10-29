import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from envweaver import Runtime, TaskPool, Verifier
from envweaver._io import ssh_transport as _ssh_transport
from envweaver._io.storage import local_source, local_target
from envweaver.runtime.docker import DockerBackend
from tests.conftest import run_verifier


@pytest.mark.integration
@pytest.mark.skipif(
    not (os.environ.get("ENVWEAVER_TEST_FILE_HOST") and os.environ.get("ENVWEAVER_TEST_DOCKER")),
    reason="set ENVWEAVER_TEST_FILE_HOST and ENVWEAVER_TEST_DOCKER",
)
def test_real_ssh_context_upload_download(tmp_path):
    host = os.environ["ENVWEAVER_TEST_FILE_HOST"]
    directory = "/tmp/envweaver-files-" + uuid.uuid4().hex
    source = host + directory + "/inputs"
    output = host + directory + "/output"
    runtime = Runtime(root=tmp_path / "records", host=os.environ.get("ENVWEAVER_TEST_DOCKER_HOST"))
    instance = None
    try:
        with local_target(source) as staging:
            staging.mkdir()
            (staging / "binary.dat").write_bytes(bytes(range(256)))
            (staging / "empty").mkdir()
        ref = runtime.create(
            backend="docker",
            image=os.environ["ENVWEAVER_TEST_DOCKER"],
            config={"command": ["sleep", "infinity"]},
            connection={"vnc": {"password_env": "ENVWEAVER_TEST_VNC_PASSWORD"}},
        )
        instance = runtime.start(ref=ref, context=[{"source": source, "target": "/work/inputs"}])
        record = host + directory + "/instance.json"
        runtime.describe(instance=instance, path=record)
        with local_source(record) as downloaded:
            assert downloaded.stat().st_mode & 0o777 == 0o600
        connected = Runtime().connect(path=record)
        assert connected.id == instance.id
        assert connected.host == instance.host
        runtime.upload(instance=instance, source=source + "/binary.dat", target="/work/copied.dat")
        code, _, stderr = runtime.exec(
            instance=connected,
            command='python3 -c "from pathlib import Path; '
            "assert Path('/work/copied.dat').read_bytes() == bytes(range(256)); "
            "assert Path('/work/inputs/empty').is_dir()\"",
        )
        assert code == 0, stderr
        runtime.download(instance=instance, source="/work/inputs", target=output)
        with local_source(output) as downloaded:
            assert (downloaded / "binary.dat").read_bytes() == bytes(range(256))
            assert (downloaded / "empty").is_dir()
        runtime.download(
            instance=instance, source="/work/copied.dat", target=host + directory + "/file"
        )
        with local_source(host + directory + "/file") as downloaded:
            assert downloaded.read_bytes() == bytes(range(256))

        entry = tmp_path / "verify/run.py"
        entry.parent.mkdir()
        entry.write_text(
            "from pathlib import Path\n"
            "from helper import EXPECTED\n"
            "assert Path('expected.txt').read_text() == EXPECTED\n"
            "assert Path('inputs/binary.dat').read_bytes() == bytes(range(256))\n"
            "assert Path('inputs/empty').is_dir()\n"
            "assert Path('copy.dat').read_bytes() == bytes(range(256))\n"
            "assert Path('local.txt').read_text() == 'local input'\n"
            "print('{\"score\": 1}')\n"
        )
        local = tmp_path / "local.txt"
        local.write_text("local input")
        (entry.parent / "helper.py").write_text("EXPECTED = 'sidecar material'")
        (entry.parent / "expected.txt").write_text("sidecar material")
        (entry.parent / "reference.png").write_bytes(bytes(range(256)))
        remote_package = host + directory + "/verify"
        with local_target(remote_package) as staging:
            shutil.copytree(entry.parent, staging)
        shutil.rmtree(entry.parent)
        checker = Verifier(
            entry=remote_package + "/run.py",
            runtime=runtime,
            timeout=60,
            requires_task_instance=False,
        )
        files = [
            {"source": source, "target": "inputs"},
            {"source": source + "/binary.dat", "target": "copy.dat"},
            {"source": local, "target": "local.txt"},
        ]
        assert run_verifier(checker, ref, files=files) == {"score": 1}
        pool = TaskPool(root=host + directory + "/pool", runtime=runtime)
        task_ref = pool.save(
            cwd="/", instance=instance,
            name="任务 materials #100%",
            goal=[
                {"type": "text", "text": "Task"},
                {"type": "image", "path": remote_package + "/reference.png"},
            ],
            verifier=checker,
            verifier_runtime_ref=ref,
        )
        _ssh_transport.ssh(
            host,
            [
                "python3",
                "-c",
                "import json,sys,shutil; shutil.rmtree(json.load(sys.stdin)['path'])",
            ],
            {"path": directory + "/verify"},
        )
        assert task_ref.endswith("/task.json")
        assert pool.search() == [task_ref]
        task = pool.load(task_ref=task_ref)
        assert task.task_runtime_ref.connection == ref.connection
        assert task.verifier_runtime_ref.connection == ref.connection
        assert Path(task.goal[1]["path"]).read_bytes() == bytes(range(256))
        assert run_verifier(task.verifier, task.verifier_runtime_ref, files=files) == {"score": 1}

    finally:
        try:
            if instance is not None:
                runtime.close(instance=instance)
        finally:
            for path in runtime.root.glob("runtimes/*/metadata.json"):
                record = json.loads(path.read_text())
                DockerBackend(host=runtime.host)._docker(
                    "image", "rm", record["tag"], host=runtime.host
                )
            _ssh_transport.ssh(
                host,
                [
                    "python3",
                    "-c",
                    "import json,sys,shutil; "
                    "shutil.rmtree(json.load(sys.stdin)['path'], ignore_errors=True)",
                ],
                {"path": directory},
            )
