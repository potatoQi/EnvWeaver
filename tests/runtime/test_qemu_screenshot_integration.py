"""Exercise real QEMU startup and display without a guest OS or SSH service."""

import io
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from PIL import Image

from envweaver import Instance, Runtime
from envweaver.runtime.qemu import QMP

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not shutil.which("qemu-system-x86_64"), reason="requires QEMU"),
]


@pytest.mark.skipif(not shutil.which("qemu-img"), reason="requires qemu-img")
def test_qemu_restart_retains_ssh_forwarding(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="ew-port-", dir="/tmp") as directory:
        root = Path(directory)
        base = root / "base.qcow2"
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", str(base), "8M"],
            check=True, capture_output=True,
        )
        runtime = Runtime()
        backend = runtime._backend("qemu")
        # There is no guest OS; exercise the real QEMU lifecycle without an SSH probe.
        monkeypatch.setattr(backend, "_check_ssh", False)
        ref = runtime.create(backend="qemu", image=str(base), config={
            "disk_dir": str(root), "memory": 128, "cpus": 1,
            "accel": os.environ.get("ENVWEAVER_TEST_QEMU_ACCEL", "tcg"),
        }, connection={"ssh": {"user": "worker"}})
        instance = runtime.start(ref=ref)
        try:
            port = instance.data["ssh"]["port"]
            assert "port" not in instance.data
            with socket.create_connection(("127.0.0.1", port), timeout=5):
                pass
            descriptor = root / "instance.json"
            runtime.describe(instance=instance, path=descriptor)
            connected = runtime.connect(path=descriptor)
            assert connected.data["ssh"]["port"] == port
            assert "port" not in connected.data

            with QMP(instance.data["qmp"]) as monitor:
                monitor.execute("quit")
            deadline = time.monotonic() + 10
            while backend._running(instance):
                if time.monotonic() >= deadline:
                    pytest.fail("QEMU did not stop before restart")
                time.sleep(0.05)
            backend._launch(instance)
            with socket.create_connection(("127.0.0.1", port), timeout=5):
                pass
        finally:
            if backend._running(instance):
                runtime.close(instance=instance)
        assert not Path(instance.data["owned_directory"]).exists()


def test_qemu_screenshot_returns_png():
    accelerator = os.environ.get("ENVWEAVER_TEST_QEMU_ACCEL", "tcg")
    with tempfile.TemporaryDirectory(prefix="ew-screen-", dir="/tmp") as directory:
        socket = Path(directory) / "qmp.sock"
        process = subprocess.Popen(
            [
                "qemu-system-x86_64", "-machine", "q35", "-accel", accelerator,
                "-m", "128", "-smp", "1", "-nodefaults", "-device", "VGA",
                "-display", "none", "-S", "-qmp", f"unix:{socket},server=on,wait=off",
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while not socket.exists():
                if process.poll() is not None:
                    pytest.fail(process.stderr.read().decode(errors="replace"))
                if time.monotonic() >= deadline:
                    pytest.fail("QEMU did not create its QMP socket")
                time.sleep(0.05)
            with QMP(socket) as monitor:
                if accelerator == "kvm":
                    assert monitor.execute("query-kvm")["enabled"]
                monitor.execute("cont")
            instance = Instance("screenshot-test", "qemu", data={"qmp": str(socket)})
            result = Runtime().screenshot(instance=instance)
            with Image.open(io.BytesIO(result)) as image:
                assert image.format == "PNG"
                assert image.width > 0 and image.height > 0
                image.load()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stderr.close()
