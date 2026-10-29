"""Screenshots from a real Xvfb/x11vnc desktop, including verifier forwarding."""

import io
import json
import os
import secrets
import socket

import pytest
from PIL import Image

from envweaver import Runtime, RuntimeRef, Verifier
from envweaver.errors import RuntimeError as TransportError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ENVWEAVER_TEST_DESKTOP_IMAGE"),
        reason="requires an image with Xvfb, xlogo, x11vnc and EnvWeaver",
    ),
]


@pytest.mark.parametrize("authenticated", [False, True])
def test_real_desktop_screenshot_and_verifier(tmp_path, monkeypatch, authenticated):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    password = secrets.token_hex(4)
    name = "ENVWEAVER_TEST_CLIENT_VNC_PASSWORD"
    monkeypatch.setenv(name, password)
    runtime = Runtime()
    image = os.environ["ENVWEAVER_TEST_DESKTOP_IMAGE"]
    command = (
        "Xvfb :99 -screen 0 640x480x24 >/tmp/xvfb.log 2>&1 & "
        "while [ ! -e /tmp/.X11-unix/X99 ]; do sleep .1; done; "
        "xlogo -display :99 -bg '#ff0000' -fg '#00ff00' -geometry 200x160+20+20 >/tmp/xlogo.log 2>&1 & "
        "exec x11vnc -display :99 -rfbport 5900 -forever -shared "
        + ('-passwd "$EW_SERVER_PASSWORD"' if authenticated else '-nopw')
    )
    ref = RuntimeRef("docker", image, {
        "command": ["sh", "-c", command], "args": ["-p", f"127.0.0.1:{port}:5900"],
        "env": {"EW_SERVER_PASSWORD": password} if authenticated else {},
    }, {"vnc": {"password_env": name}} if authenticated else {})
    connection = {"vnc": {"host": "127.0.0.1", "port": port}}
    instance = None
    verifier_instance = None
    try:
        instance = runtime.start(ref=ref, connection=connection, setup={
            "command": ["python3", "-c", "import socket,time\nfor _ in range(100):\n try:\n  socket.create_connection(('127.0.0.1',5900),timeout=1).close(); break\n except OSError: time.sleep(.1)\nelse: raise RuntimeError('VNC not ready')"],
            "timeout": 20,
        })
        png = runtime.screenshot(instance=instance)
        with Image.open(io.BytesIO(png)) as screenshot:
            assert screenshot.format == "PNG"
            assert screenshot.size == (640, 480)
            assert screenshot.convert("RGB").getpixel((25, 25)) == (255, 0, 0)
        record = tmp_path / "instance.json"
        runtime.describe(instance=instance, path=record)
        assert password not in record.read_text()
        reconnected = runtime.connect(path=record)
        with Image.open(io.BytesIO(runtime.screenshot(instance=reconnected))) as screenshot:
            assert screenshot.size == (640, 480)
        if authenticated:
            monkeypatch.delenv(name)
            with pytest.raises(ValueError, match=name):
                runtime.screenshot(instance=instance)
            monkeypatch.setenv(name, "wrongpwd")
            with pytest.raises(TransportError):
                runtime.screenshot(instance=instance)
            monkeypatch.setenv(name, password)
        entry = tmp_path / "verify/run.py"
        entry.parent.mkdir()
        entry.write_text(
            "import argparse,io,json,os\nfrom PIL import Image\nfrom envweaver import Runtime\n"
            "p=argparse.ArgumentParser(); p.add_argument('--instance'); r=Runtime(); i=r.connect(path=p.parse_args().instance)\n"
            f"assert {name!r} not in os.environ\n"
            "image=Image.open(io.BytesIO(r.screenshot(instance=i))); print(json.dumps({'score':float(image.size==(640,480))}))\n"
        )
        verifier_instance = runtime.start(ref=RuntimeRef("docker", image, {"command": ["sleep", "infinity"]}))
        result = Verifier(entry=entry, runtime=runtime, timeout=30).run(
            verifier_instance=verifier_instance, task_instance=instance,
        )
        assert result == {"score": 1}
        assert json.loads(record.read_text())["vnc"]["host"] == "127.0.0.1"
    finally:
        for item in (verifier_instance, instance):
            if item is not None:
                runtime.close(instance=item)
