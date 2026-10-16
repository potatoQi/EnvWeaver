import json
import os
import sys
from pathlib import Path

import httpx
import pytest

from envweaver import agent, llm
from envweaver.errors import AgentError
from envweaver.tools import Tool


def test_llm_request_and_errors(model_server):
    url, requests, state = model_server
    schema = {
        "type": "object",
        "properties": {"choice": {"type": "string"}},
        "required": ["choice"],
        "additionalProperties": False,
    }
    messages = [{"role": "user", "content": [{"type": "text", "text": "choose"}]}]
    response = llm.call(url, "test", "key", messages, schema)
    assert response["choices"][0]["message"]["content"] == "done"
    assert requests[0]["messages"] == messages
    assert requests[0]["response_format"]["json_schema"]["schema"] == schema
    state["status"] = 401
    with pytest.raises(httpx.HTTPStatusError):
        llm.call(url, "test", "bad", messages)


def test_agent_adapter_and_error_artifacts(tmp_path):
    script = tmp_path / "pi.py"
    script.write_text(
        "import json\nprint(json.dumps({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'error', 'content': []}}))"
    )
    with pytest.raises(AgentError):
        agent.run("pi", "test", tmp_path, config={"command": [sys.executable, str(script)]})
    assert list(tmp_path.glob(".envweaver/*/events.jsonl"))


@pytest.mark.skipif(
    not os.environ.get("ENVWEAVER_TEST_PI"),
    reason="set ENVWEAVER_TEST_PI to an installed pi 1.3.x executable",
)
def test_real_pi_custom_tool_and_model_transport(tmp_path, model_server):
    url, requests, state = model_server
    seen = []
    state.update(tool="probe", arguments={"value": "hello"})
    tool = Tool(
        "probe",
        "Test an infrastructure connection",
        {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
        lambda value: (
            seen.append(value) or {"content": [{"type": "text", "text": "tool succeeded"}]}
        ),
    )
    result = agent.run(
        "pi",
        "Call probe then finish.",
        tmp_path,
        tools=[tool],
        config={
            "command": [os.environ["ENVWEAVER_TEST_PI"]],
            "base_url": url,
            "model_name": "test",
            "api_key": "test",
            "timeout": 45,
        },
    )
    assert result.text == "done"
    assert seen == ["hello"]
    assert len(requests) == 2
    assert Path(result.trajectory).exists()
    assert result.session and Path(result.session).exists()
    assert "test" not in "".join(str(path) for path in tmp_path.glob("**/models.json"))
    resumed = agent.run(
        "pi",
        "Remember the previous probe result.",
        tmp_path,
        tools=[tool],
        config={
            "command": [os.environ["ENVWEAVER_TEST_PI"]],
            "base_url": url,
            "model_name": "test",
            "api_key": "test",
            "timeout": 45,
            "session": result.session,
        },
    )
    assert resumed.text == "done"
    assert "tool succeeded" in json.dumps(requests[-1]["messages"])


@pytest.mark.skipif(not os.environ.get("ENVWEAVER_TEST_PI"), reason="requires pi")
def test_real_pi_ipython_image_roundtrip(tmp_path, guest, model_server):
    url, requests, state = model_server
    state.update(
        tool="ipython",
        arguments={"code": "from PIL import Image\ndisplay(Image.new('RGB', (8, 8), 'red'))"},
    )
    result = agent.run(
        "pi",
        "Display a tiny image, then finish.",
        tmp_path,
        tools=["ipython"],
        instance=guest,
        config={
            "command": [os.environ["ENVWEAVER_TEST_PI"]],
            "base_url": url,
            "model_name": "test",
            "api_key": "test",
            "timeout": 45,
        },
    )
    assert result.text == "done"
    assert len(requests) == 2
    assert "data:image/" in json.dumps(requests[1])
