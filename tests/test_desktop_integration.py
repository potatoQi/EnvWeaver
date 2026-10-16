"""Opt-in X11 test using a prepared desktop image and optional offline kernel wheels."""

import base64
import dataclasses
import os
import subprocess
import time
from pathlib import Path

import pytest

import envweaver
from envweaver import Runtime, RuntimeRef
from envweaver.transport import GuestClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ENVWEAVER_TEST_DESKTOP"),
        reason="requires prepared Linux X11 desktop image",
    ),
]


def test_real_desktop_ipython_screenshot_and_mouse(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVWEAVER_GUEST_TOKEN", "isolated-desktop-test")
    runtime = Runtime(tmp_path / "assets")
    instance = runtime.start(
        RuntimeRef(
            "docker",
            os.environ["ENVWEAVER_TEST_DESKTOP"],
            {
                "args": ["--publish", "127.0.0.1::8765"],
                "memory": "2g",
                "cpus": 2,
                "shm_size": "256m",
                "env": {"XDG_SESSION_TYPE": "x11"},
            },
        )
    )
    try:
        runtime.upload(instance, Path(envweaver.__file__).parent, "/tmp/envweaver-src/envweaver")
        if os.environ.get("ENVWEAVER_TEST_KERNEL_WHEELS"):
            runtime.upload(instance, os.environ["ENVWEAVER_TEST_KERNEL_WHEELS"], "/tmp/wheels")
            code, out, err = runtime.exec(
                instance,
                "python3 -m pip install --no-index --find-links=/tmp/wheels jupyter-client ipykernel pillow",
                timeout=60,
            )
            assert code == 0, out + err
        code, out, err = runtime.exec(
            instance,
            "PYTHONPATH=/tmp/envweaver-src ENVWEAVER_GUEST_TOKEN=isolated-desktop-test nohup python3 -m envweaver.guest --host 0.0.0.0 >/tmp/envweaver-guest.log 2>&1 </dev/null &",
        )
        assert code == 0, out + err
        port = (
            subprocess.check_output(["docker", "port", instance.id, "8765/tcp"], text=True)
            .strip()
            .split(":")[-1]
        )
        instance = dataclasses.replace(
            instance, data={**instance.data, "guest_url": "http://127.0.0.1:" + port}
        )
        client = GuestClient(instance)
        deadline = time.monotonic() + 30
        while True:
            try:
                client.health()
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise AssertionError(runtime.exec(instance, "cat /tmp/envweaver-guest.log"))
                time.sleep(0.5)
        result = client.ipython(
            "import pyautogui as gui\nprint(gui.size())\ndisplay(gui.screenshot())",
            "desktop-test",
            30,
        )
        assert not result["isError"], result
        images = [item for item in result["content"] if item["type"] == "image"]
        assert images
        (tmp_path / "desktop.png").write_bytes(base64.b64decode(images[0]["data"]))
        result = client.ipython("gui.moveTo(400, 300)\nprint(gui.position())", "desktop-test", 30)
        assert not result["isError"], result
        assert "400" in result["content"][0]["text"]
        client.close_session("desktop-test")
    finally:
        assert runtime.close(instance)[0]
