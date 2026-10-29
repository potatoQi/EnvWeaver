"""Exercise real qcow2 files without booting a guest OS."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from envweaver.runtime import qemu

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (shutil.which("qemu-img") and shutil.which("qemu-io")),
        reason="requires qemu-img and qemu-io",
    ),
]


def run(*args):
    return subprocess.check_output(list(map(str, args)), text=True)


@pytest.mark.parametrize("base_format", ["qcow2", "raw"])
@pytest.mark.parametrize("mode", ["incremental", "consolidated"])
def test_shared_base_frozen_save_and_instance_cleanup(base_format, mode, monkeypatch):
    # Keep QMP paths short, including on macOS.
    with tempfile.TemporaryDirectory(prefix="ew-disk-", dir="/tmp") as directory:
        root = Path(directory)
        base = root / f"base.{base_format}"
        run("qemu-img", "create", "-f", base_format, base, "32M")
        run("qemu-io", "-f", base_format, "-c", "write -P 0x11 0 16M", base)
        backend = qemu.QemuBackend(root / "records")
        monkeypatch.setattr(backend, "_launch", lambda instance: None)
        monkeypatch.setattr(backend, "_shutdown", lambda instance: None)

        class QMP:
            def __init__(self, path):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, command):
                assert command == "quit"

        monkeypatch.setattr(qemu, "QMP", QMP)
        monkeypatch.chdir(root)
        original = backend.start(
            backend.create(
                image=base.name,
                config={"format": base_format, "disk_dir": str(root / "disks"), "save_mode": mode},
            ),
            connection={"ssh": {"user": "worker"}},
        )
        monkeypatch.chdir("/tmp")
        assert original.data["ref"]["image"] == str(base)
        disk = original.data["disk"]
        run("qemu-io", "-f", "qcow2", "-c", "write -P 0x22 1M 64k", disk)
        run("qemu-io", "-f", "qcow2", "-c", "write -P 0x66 4M 64k", disk)
        run("qemu-io", "-f", "qcow2", "-c", "write -z 2M 64k", disk)
        saved = backend.save(original)
        chain = json.loads(run("qemu-img", "info", "--backing-chain", "--output=json", saved.image))
        assert len(chain) == 2
        assert chain[0]["full-backing-filename"] == str(base)
        assert chain[0]["actual-size"] < 2 * 1024 * 1024
        run("qemu-img", "compare", disk, saved.image)

        # Continuing the producer must not change the saved Task.
        run("qemu-io", "-f", "qcow2", "-c", "write -P 0x33 1M 64k", disk)
        run("qemu-io", "-f", "qcow2", "-c", "read -P 0x22 1M 64k", saved.image)
        again = backend.save(original)
        again_chain = json.loads(
            run("qemu-img", "info", "--backing-chain", "--output=json", again.image)
        )
        assert len(again_chain) == 2
        backend.close(original)
        assert not Path(disk).parent.exists()

        first = backend.start(saved, connection={"ssh": {"user": "worker"}})
        second = backend.start(saved, connection={"ssh": {"user": "worker"}})
        run("qemu-io", "-f", "qcow2", "-c", "write -P 0x44 1M 64k", first.data["disk"])
        run("qemu-io", "-f", "qcow2", "-c", "write -P 0x55 3M 64k", first.data["disk"])
        run("qemu-io", "-f", "qcow2", "-c", "read -P 0x22 1M 64k", second.data["disk"])
        run("qemu-io", "-f", "qcow2", "-c", "read -P 0 2M 64k", second.data["disk"])
        derived = backend.save(first)
        run("qemu-img", "compare", first.data["disk"], derived.image)
        backend.close(first)
        backend.close(second)
        derived_chain = json.loads(
            run("qemu-img", "info", "--backing-chain", "--output=json", derived.image)
        )
        assert len(derived_chain) == (2 if mode == "consolidated" else 3)
        assert derived_chain[0]["full-backing-filename"] == (
            str(base) if mode == "consolidated" else saved.image
        )
        # Consolidation must preserve inherited zero blocks and new changes without changing A.
        run("qemu-io", "-f", "qcow2", "-c", "read -P 0 2M 64k", derived.image)
        run("qemu-io", "-f", "qcow2", "-c", "read -P 0x55 3M 64k", derived.image)
        run("qemu-io", "-f", "qcow2", "-c", "read -P 0x22 1M 64k", saved.image)
        if mode == "consolidated":
            Path(saved.image).unlink()
        run("qemu-io", "-f", "qcow2", "-c", "read -P 0x44 1M 64k", derived.image)
        run("qemu-io", "-f", "qcow2", "-c", "read -P 0x66 4M 64k", derived.image)
        third = backend.start(derived, connection={"ssh": {"user": "worker"}})
        run("qemu-io", "-f", "qcow2", "-c", "write -P 0x77 5M 64k", third.data["disk"])
        latest = backend.save(third)
        run("qemu-img", "compare", third.data["disk"], latest.image)
        backend.close(third)
        latest_chain = json.loads(
            run("qemu-img", "info", "--backing-chain", "--output=json", latest.image)
        )
        assert len(latest_chain) == (2 if mode == "consolidated" else 4)
        run("qemu-io", "-f", base_format, "-c", "read -P 0x11 0 16M", base)
        assert Path(saved.image).is_file() == (mode == "incremental")
        assert not list((root / "disks/instances").iterdir())
        assert not list((root / "records/instances").iterdir())
        assert not list((root / "records").rglob("*.qcow2"))
        assert not list((root / "disks").rglob("*.json"))
