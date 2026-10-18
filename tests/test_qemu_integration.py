"""Opt-in lifecycle test against a prepared Linux/Windows guest, local or SSH-hosted."""

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from envweaver import Runtime, RuntimeRef, _ssh_transport

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (
            os.environ.get("ENVWEAVER_TEST_QEMU_REF")
            and os.environ.get("ENVWEAVER_TEST_QEMU_DISK_DIR")
        ),
        reason="requires a prepared QEMU reference and explicit test storage directory",
    ),
]


def test_qemu_save_restore_and_verifier(tmp_path, run_native_verifier):
    ref = RuntimeRef.from_dict(json.loads(Path(os.environ["ENVWEAVER_TEST_QEMU_REF"]).read_text()))
    root = tmp_path / "records"
    disk_dir = Path(os.environ["ENVWEAVER_TEST_QEMU_DISK_DIR"]) / uuid.uuid4().hex
    ref.config["disk_dir"] = str(disk_dir)
    host = os.environ.get("ENVWEAVER_TEST_QEMU_HOST")
    runtime = Runtime(root=root, host=host)
    guest_path = os.environ.get("ENVWEAVER_TEST_QEMU_FILE", "/tmp/envweaver-check.txt")
    ref = runtime.create(backend=ref.backend, image=ref.image, config=ref.config)
    connection = json.loads(os.environ["ENVWEAVER_TEST_QEMU_CONNECTION"])
    original = runtime.start(ref, connection=connection)
    instances = [original]
    try:
        source = tmp_path / "input.txt"
        source.write_text("initial")
        runtime.upload(original, source, guest_path)
        assert runtime.exec(original, "echo ready")[0] == 0
        saved = runtime.save(original)
        assert "host" not in saved.to_dict()
        source.write_text("changed after save")
        runtime.upload(original, source, guest_path)
        runtime.close(original)
        instances.remove(original)
        restored = Runtime(root=root, host=host).start(saved, connection=connection)
        instances.append(restored)
        runtime.download(restored, guest_path, tmp_path / "result.txt")
        assert (tmp_path / "result.txt").read_text() == "initial"
        entry = tmp_path / "run.py"
        entry.write_text(
            """import argparse, json, tempfile
from pathlib import Path
from envweaver import Runtime
p = argparse.ArgumentParser()
p.add_argument('--instance')
runtime = Runtime()
i = runtime.connect(path=p.parse_args().instance)
with tempfile.TemporaryDirectory() as directory:
    target = Path(directory) / 'result.txt'
    runtime.download(i, """
            + repr(guest_path)
            + """, target)
    print(json.dumps({'score': float(target.read_text() == 'initial')}))
"""
        )
        assert run_native_verifier(entry, restored)["score"] == 1
    finally:
        for instance in instances:
            runtime.close(instance)
        # Only delete this test's UUID directory, after every instance has stopped.
        if host:
            source = "import json,sys,shutil; shutil.rmtree(json.load(sys.stdin))"
            _ssh_transport.ssh(host, ["python3", "-c", source], str(disk_dir))
        else:
            shutil.rmtree(disk_dir)
