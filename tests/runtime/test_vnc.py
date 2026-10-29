"""Native RFB screenshot without installing EnvWeaver in a desktop."""

import importlib.util
import io
import socket
import struct
import threading

import pytest
from PIL import Image

from envweaver.runtime import _vnc


@pytest.mark.skipif(importlib.util.find_spec("vncdotool") is None, reason="requires desktop extra")
def test_screenshot_uses_native_vnc():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    errors = []

    def serve():
        def read(stream, size):
            result = stream.read(size)
            if len(result) != size:
                raise EOFError()
            return result

        try:
            connection, _ = server.accept()
            connection.settimeout(10)
            with connection, connection.makefile("rwb", buffering=0) as stream:
                stream.write(b"RFB 003.008\n")
                read(stream, 12)
                stream.write(b"\x01\x01")  # one security type: none
                assert read(stream, 1) == b"\x01"
                stream.write(b"\0" * 4)
                read(stream, 1)
                pixel_format = struct.pack("!BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
                stream.write(
                    struct.pack("!HH", 2, 2) + pixel_format + struct.pack("!I", 4) + b"test"
                )
                while True:
                    kind = read(stream, 1)[0]
                    if kind == 0:
                        pixel_format = read(stream, 19)[3:]
                    elif kind == 2:
                        count = struct.unpack("!xH", read(stream, 3))[0]
                        read(stream, count * 4)
                    elif kind == 3:
                        read(stream, 9)
                        bpp, _, big, _, _, _, _, red, green, blue = struct.unpack(
                            "!BBBBHHHBBBxxx", pixel_format
                        )
                        value = (255 << red) | (128 << green) | (32 << blue)
                        pixel = value.to_bytes(bpp // 8, "big" if big else "little")
                        stream.write(
                            struct.pack("!BBH", 0, 0, 1)
                            + struct.pack("!HHHHi", 0, 0, 2, 2, 0)
                            + pixel * 4
                        )
                    else:
                        raise AssertionError(f"Unexpected RFB message {kind}")
        except EOFError:
            pass
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        result = _vnc.screenshot({"host": "127.0.0.1", "port": server.getsockname()[1]})
        image = Image.open(io.BytesIO(result)).convert("RGB")
        assert image.size == (2, 2)
        assert image.getpixel((0, 0)) == (255, 128, 32)
    finally:
        server.close()
        thread.join(timeout=12)
    assert not errors
