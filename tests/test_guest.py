import io
import zipfile

import httpx
import pytest

from envweaver._files import unpack
from envweaver.tools import ipython
from envweaver.transport import GuestClient


def test_command_errors_timeouts_and_file_roundtrip(tmp_path, guest):
    client = GuestClient(guest)
    assert client.exec("printf hello") == (0, "hello", "")
    assert client.exec("exit 7")[0] == 7
    with pytest.raises(TimeoutError):
        client.exec("sleep 30", timeout=0.1)
    source = tmp_path / "source"
    source.mkdir()
    (source / "nested").mkdir()
    (source / "nested" / "图.png").write_bytes(b"\x00\x01\x02")
    (source / "empty").mkdir()
    target = tmp_path / "remote"
    client.upload(source, str(target))
    client.download(str(target), tmp_path / "copy")
    assert (tmp_path / "copy/nested/图.png").read_bytes() == b"\x00\x01\x02"
    assert (tmp_path / "copy/empty").is_dir()
    (source / "nested/图.png").write_bytes(b"new")
    client.upload(source, str(target))
    assert (target / "nested/图.png").read_bytes() == b"new"


def test_service_requires_authentication(guest):
    response = httpx.get(guest.data["guest_url"] + "/health", trust_env=False)
    assert response.status_code == 401


def test_archive_path_traversal_rejected(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escape", "bad")
    with pytest.raises(ValueError, match="Unsafe"):
        unpack("directory", buffer.getvalue(), tmp_path / "target")
    assert not (tmp_path / "escape").exists()


def test_persistent_ipython_images_errors_and_session_isolation(guest):
    tool = ipython(guest)
    other = ipython(guest)
    try:
        assert not tool.execute("number = 41")["isError"]
        result = tool.execute(
            "print(number + 1)\nfrom PIL import Image\ndisplay(Image.new('RGB', (3, 3)))"
        )
        assert "42" in result["content"][0]["text"]
        assert any(block["type"] == "image" for block in result["content"])
        assert other.execute("print(number)")["isError"]
        assert tool.execute("raise ValueError('intentional')")["isError"]
        assert "41" in tool.execute("print(number)")["content"][0]["text"]
    finally:
        tool.close()
        other.close()
