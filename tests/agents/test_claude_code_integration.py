"""Exercise the installed Claude CLI against a local Anthropic error response."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from envweaver import ClaudeCodeAgent, EvalConfig, Runtime, RuntimeRef, Task, Verifier, eval

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("scenario", "status", "message", "expected", "verify"),
    [
        ("context", 400, "prompt is too long: 500 tokens > 400 maximum", "context_limit", True),
        ("bad_request", 400, "invalid request parameter", "configuration", False),
        ("bad_key", 401, "invalid x-api-key", "authentication", False),
        ("timeout", 200, "", "timeout", True),
    ],
)
def test_eval_stop_reason(tmp_path, scenario, status, message, expected, verify):
    image = os.environ.get("ENVWEAVER_TEST_CLAUDE_IMAGE")
    if not image:
        pytest.skip("requires a Docker image with Claude Code and EnvWeaver")
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            if scenario == "timeout":
                release.wait(60)
                return
            payload = json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "authentication_error"
                        if status == 401
                        else "invalid_request_error",
                        "message": message,
                    },
                }
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        ref = RuntimeRef("docker", image, {"command": ["sleep", "infinity"], "network": "host"})
        entry = tmp_path / "run.py"
        entry.write_text("print('{\"score\": 0.0}')\n")
        task = Task(
            task_ref="claude/" + scenario,
            name=scenario,
            metadata={},
            context=[],
            cwd="/tmp",
            goal=[{"type": "text", "text": "Say hello."}],
            task_runtime_ref=ref,
            verifier_runtime_ref=ref,
            verifier=Verifier(entry=entry, runtime=Runtime(), timeout=30),
        )
        agent = ClaudeCodeAgent(
            model="claude-sonnet-4-6",
            env={
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "ANTHROPIC_API_KEY": "test-key",
                "CLAUDE_CODE_MAX_RETRIES": "0",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
        )
        (result,) = eval(
            tasks=task,
            agent=agent,
            output_dir=tmp_path / "eval",
            config=EvalConfig(agent_timeout=8 if scenario == "timeout" else 45, max_retries=0),
        )
        assert result["score_valid"] is verify, result["errors"]
        if verify:
            assert result["agent_stop_reason"] == expected, result["errors"]
        else:
            assert result["agent_stop_reason"] == "error"
            assert any(e["type"] == expected for e in result["errors"]), result["errors"]
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join()
