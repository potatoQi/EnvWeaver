"""Opt-in native CLI retry check against a local HTTP server, without a model."""

import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from envweaver import CodexAgent, Instance
from envweaver.agents.execution import AgentExecution
from tests.agents.test_cli_agents import LocalTransport

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("first_status", [200, 429, 503, 401])
def test_codex_native_recovery(tmp_path, monkeypatch, first_status):
    binary = os.environ.get("ENVWEAVER_TEST_CODEX")
    if not binary:
        pytest.skip("requires ENVWEAVER_TEST_CODEX with a Codex executable path")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append(self.path)
            if first_status != 200 and (len(requests) == 1 or first_status == 401):
                body = json.dumps({"error": {"message": "injected", "type": "server_error"}}).encode()
                self.send_response(first_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Retry-After", "0")
                self.end_headers()
                self.wfile.write(body)
                return
            events = [{"type": "response.created", "response": {"id": "mock", "status": "in_progress"}}]
            if len(requests) > 1:
                item = {
                    "id": "msg", "type": "message", "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "recovered", "annotations": []}],
                }
                events += [
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {
                        "id": "mock", "status": "completed", "output": [item],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    }},
                ]
            body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.01)
    runtime = LocalTransport()
    instance = Instance("test", "local", data={"python": sys.executable})
    (tmp_path / "codex-config").mkdir()
    agent = CodexAgent(
        model="mock-model",
        env={
            "CODEX_HOME": str(tmp_path / "codex-config"),
            "PATH": str(Path(binary).parent) + os.pathsep + os.environ["PATH"],
        },
        extra_args=[
            "-c", 'model_provider="mock"',
            "-c", 'model_providers.mock.name="Mock"',
            "-c", f'model_providers.mock.base_url="http://127.0.0.1:{server.server_port}/v1"',
            "-c", 'model_providers.mock.wire_api="responses"',
            "-c", "model_providers.mock.requires_openai_auth=false",
        ],
    )
    agent_execution = AgentExecution(agent)
    directory = tmp_path / "output"
    watchdog = threading.Timer(45, lambda: agent_execution.stop(timeout=5))
    watchdog.start()
    try:
        agent_execution.prepare(runtime=runtime, instance=instance, output_dir=directory, cwd=str(tmp_path))
        def run():
            return agent_execution.run(
                goal=[{"type": "text", "text": "Reply with one word."}],
                runtime=runtime, instance=instance, output_dir=directory,
            )

        if first_status in (401, 429):
            from envweaver import ErrorType
            from envweaver.errors import AgentError

            with pytest.raises(AgentError) as caught:
                run()
            expected = (
                ErrorType.AUTHENTICATION if first_status == 401
                else ErrorType.PROVIDER_TRANSIENT
            )
            assert caught.value.type is expected
            # Codex's request policy distinguishes 429 from stream retries.
            # Once the CLI returns, Eval decides whether to retry the task.
            assert requests
            return
        result = run()
        assert len(requests) >= 2
        assert result["output"] == [{"type": "text", "text": "recovered"}]
    finally:
        watchdog.cancel()
        agent_execution.stop()
        if agent._remote:
            shutil.rmtree(agent._remote, ignore_errors=True)
        server.shutdown()
        server.server_close()
        worker.join()
