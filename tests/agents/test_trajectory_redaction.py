"""Credential redaction must not rewrite encoded image bytes."""

import base64
import json
import sys
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from envweaver import AgentOutput, PiAgent
from envweaver.agents.trajectory.capture import Media, NativeFiles
from tests.agents.test_cli_agents import LocalTransport


@pytest.fixture
def picture():
    stream = BytesIO()
    Image.new("RGB", (3, 3), "red").save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def output(tmp_path):
    return AgentOutput(
        tmp_path / "output", {"agent": "test", "model": "test", "agent_version": "test"}
    )


@pytest.mark.parametrize("shape", ["url", "pi", "anthropic"])
def test_native_capture_preserves_encoded_media(output, tmp_path, picture, shape):
    encoded = base64.b64encode(picture).decode()
    secret = encoded[:8]
    image = {
        "url": {"image_url": "data:image/png;base64," + encoded},
        "pi": {"type": "image", "mimeType": "image/png", "data": encoded},
        "anthropic": {"type": "base64", "media_type": "image/png", "data": encoded},
    }[shape]
    path = tmp_path / "native.json"
    path.write_text(json.dumps({"image": image, "text": secret, "plain": {"data": secret}}))
    collector = NativeFiles(
        runtime=LocalTransport(), instance=None, python=sys.executable,
        output=output, redact=lambda text: text.replace(secret, "[REDACTED]"),
    )
    assert collector.poll({"probe": str(path)})
    saved = json.loads(collector.files["probe/native.json"])
    assert saved["image"] == image
    assert saved["text"] == saved["plain"]["data"] == "[REDACTED]"
    source = saved["image"].get(
        "image_url", "data:image/png;base64," + saved["image"].get("data", "")
    )
    media = Media(output).image(source)
    assert (output.directory / media["source"]["path"]).read_bytes() == picture


def test_pi_stdout_image_survives_credential_collision(output, picture):
    encoded = base64.b64encode(picture).decode()
    agent = PiAgent(env={"OPENAI_API_KEY": encoded[:8]})
    agent._output = output
    agent._event_line(
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": encoded[:8]},
                        {"type": "image", "mimeType": "image/png", "data": encoded},
                    ],
                },
            }
        )
    )
    assert output.result["output"][0]["text"] == "[REDACTED]"
    assert (output.directory / output.result["output"][1]["path"]).read_bytes() == picture


def test_model_capture_keeps_image_payload(tmp_path, monkeypatch, picture):
    from envweaver.agents.adapters._model_capture import install

    encoded = base64.b64encode(picture).decode()
    monkeypatch.setenv("TEST_API_KEY", encoded[:8])
    module = SimpleNamespace(completion=lambda **kwargs: {"choices": []})
    monkeypatch.setitem(sys.modules, "litellm", module)
    path = tmp_path / "calls.jsonl"
    install(path)
    url = "data:image/png;base64," + encoded
    module.completion(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": encoded[:8]},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ]
    )
    event = json.loads(path.read_text().split("\n")[0])
    parts = event["data"]["request"]["messages"][0]["content"]
    assert parts[0]["text"] == "[REDACTED]"
    assert parts[1]["image_url"]["url"] == url
