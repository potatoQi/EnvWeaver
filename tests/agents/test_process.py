"""Process protocol boundaries, without a sandbox or model."""

import base64
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from envweaver import CodexAgent
from envweaver.agents.adapters.sandbox import _process


@pytest.mark.parametrize("finished", [True, False])
def test_prepare_poll_wakes_on_exit_or_deadline(tmp_path, monkeypatch, finished):
    clock = [0.0]
    monkeypatch.setattr(_process.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        clock[0] += seconds
        if finished:
            _process.finish(tmp_path, 0)

    monkeypatch.setattr(_process.time, "sleep", sleep)
    result = _process.main("poll", tmp_path, {"wait": 2, "stdout": 0})
    if finished:
        assert result["status"]["code"] == 0
        assert clock[0] == 0.05
    else:
        assert result["status"] is None
        assert clock[0] == 2


@pytest.mark.parametrize("offset", [0, 4, 100])
@pytest.mark.parametrize("exit_during_read", [False, True])
def test_poll_stops_at_exit_boundary(tmp_path, monkeypatch, offset, exit_during_read):
    log = tmp_path / "stdout"
    log.write_bytes(b"donebackground output")
    status = {"code": 0, "offsets": {"stdout": 4, "stderr": 0}}
    status_path = tmp_path / "status.json"
    if not exit_during_read:
        status_path.write_text(json.dumps(status))
    original_open = Path.open

    def open_file(path, *args, **kwargs):
        if path == log and exit_during_read:
            status_path.write_text(json.dumps(status))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    result = _process.main("poll", tmp_path, {"stdout": offset})
    expected = b"done"[offset:]
    assert result["status"] == status
    assert result["streams"]["stdout"] == {
        "offset": offset + len(expected), "data": base64.b64encode(expected).decode(),
    }


@pytest.mark.parametrize("preparing,capture_stdout", [(False, False), (True, False), (True, True)])
def test_cli_drains_terminal_output_without_stop(monkeypatch, preparing, capture_stdout):
    agent = CodexAgent()
    agent._remote = "/agent"
    agent._cwd = "/workspace"
    calls, output = [], []
    body = "完成".encode()

    def rpc(action, directory, payload=None):
        calls.append(action)
        if action == "start":
            return {"started": True}
        assert action == "poll"
        offset = payload["stdout"]
        chunk = body[offset:offset + 2]  # Also exercise a split UTF-8 character.
        return {
            "status": {"code": 0, "offsets": {"stdout": len(body), "stderr": 0}},
            "streams": {
                "stdout": {"offset": offset + len(chunk), "data": base64.b64encode(chunk).decode()},
                "stderr": {"offset": 0, "data": ""},
            },
        }

    monkeypatch.setattr(agent, "_rpc", rpc)
    monkeypatch.setattr(agent, "_poll_native", lambda: pytest.fail("a finished CLI is finalized by run()"))
    monkeypatch.setattr(agent, "_consume", lambda name, text, preparing: output.append(text))
    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0)
    assert agent._execute(
        ["codex"], preparing=preparing, capture_stdout=capture_stdout,
    ) == (0, "完成" if capture_stdout else "")
    assert "".join(output) == "完成"
    assert calls == ["start", "poll", "poll", "poll"]
    assert agent._process_dir is None


@pytest.mark.parametrize("request_body", [None, "{broken", '{"command": ["/missing/cli"], "cwd": "/"}'])
def test_supervisor_start_failure_has_terminal_status(tmp_path, request_body):
    if request_body is not None:
        (tmp_path / "request.json").write_text(request_body)
    _process.supervise(tmp_path)
    result = _process.main("poll", tmp_path, {})
    assert result["status"]["code"] == -1
    assert result["status"]["error"]
    assert not (tmp_path / "request.json").exists()


def test_supervisor_requires_cwd_before_launch(tmp_path, monkeypatch):
    launch = Mock()
    monkeypatch.setattr(_process.subprocess, "Popen", launch)
    (tmp_path / "request.json").write_text(json.dumps({"command": [sys.executable, "-V"]}))
    _process.supervise(tmp_path)
    launch.assert_not_called()
    status = _process.main("poll", tmp_path, {})["status"]
    assert status["code"] == -1
    assert "cwd" in status["error"]
    assert not (tmp_path / "request.json").exists()


def test_supervisor_runs_in_requested_cwd(tmp_path):
    workdir = tmp_path / "task"
    workdir.mkdir()
    (tmp_path / "request.json").write_text(json.dumps({
        "command": [sys.executable, "-c", "from pathlib import Path; print(Path.cwd())"],
        "cwd": str(workdir),
    }))
    _process.supervise(tmp_path)
    response = _process.main("poll", tmp_path, {"stdout": 0})
    assert response["status"]["code"] == 0
    assert base64.b64decode(response["streams"]["stdout"]["data"]).decode().strip() == str(workdir)


@pytest.mark.parametrize("operation", ["launch", "permissions", "write", "replace"])
def test_supervisor_launch_failure_removes_credentials(tmp_path, monkeypatch, operation):
    def fail(*args, **kwargs):
        raise OSError("cannot launch supervisor")

    if operation == "launch":
        monkeypatch.setattr(_process.subprocess, "Popen", fail)
    elif operation == "permissions":
        monkeypatch.setattr(_process.os, "chmod", fail)
    else:
        method = "write_text" if operation == "write" else "replace"
        original = getattr(Path, method)

        def fail_request(path, *args, **kwargs):
            if path.name == "request.tmp":
                if operation == "write":
                    original(path, *args, **kwargs)
                fail()
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, method, fail_request)
    with pytest.raises(OSError, match="cannot launch supervisor"):
        _process.main("start", tmp_path, {"command": ["cli"], "env": {"API_KEY": "secret"}})
    assert not (tmp_path / "request.json").exists()
    assert not (tmp_path / "request.tmp").exists()
    assert _process.main("poll", tmp_path, {})["status"]["code"] == -1
