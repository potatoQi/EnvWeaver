"""Actual CLI lifecycle against a local scripted API, without paid model calls."""

import json
import os
import shlex
import shutil
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import product
from pathlib import Path

import pytest

from envweaver import CodexAgent, Instance, MiniSweAgent, PiAgent
from envweaver.agents.execution import AgentExecution
from envweaver.errors import AgentError, ErrorType
from tests.agents.test_cli_agents import LocalTransport

pytestmark = pytest.mark.integration


def wait_for(condition, future, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        if future.done():
            future.result()
            pytest.fail("CLI finished before reaching the expected state")
        time.sleep(0.05)
    pytest.fail("CLI did not reach the expected state")


def start_api(kind, scenario, command):
    requests = []
    blocked = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if scenario == "disconnect_after_tool" and len(requests) == 2:
                streaming = kind == "codex" or request.get("stream")
                event = {"type": "response.created", "response": {"id": "broken", "status": "in_progress"}} if kind == "codex" else {
                    "id": "broken", "choices": [{"index": 0, "finish_reason": None,
                                                  "delta": {"role": "assistant", "content": "partial"}}],
                }
                body = f"data: {json.dumps(event)}\n\n".encode() if streaming else b'{"id":'
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if streaming else "application/json")
                self.send_header("Content-Length", str(len(body) + 100))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                self.close_connection = True
                return
            if scenario in ("authentication", "context", "context_text") or (scenario == "retry" and len(requests) == 1) or (
                scenario == "retry_after_tool" and len(requests) == 2
            ):
                status = {"authentication": 401, "context": 400, "context_text": 400}.get(scenario, 503)
                message = {401: "Invalid API key", 400: "context length exceeded", 503: "Service unavailable"}[status]
                if scenario == "context_text":
                    message = "prompt is too long: 40000 tokens > 32768 maximum"
                body = json.dumps({"error": {
                    "message": message, "type": "server_error" if status == 503 else "invalid_request_error",
                    "code": None if scenario == "context_text" else {
                        401: "invalid_api_key", 400: "context_length_exceeded", 503: "service_unavailable",
                    }[status],
                }}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
            else:
                if len(requests) > 1 and scenario.startswith("stop"):
                    blocked.set()
                    release.wait(30)
                if len(requests) > 5:
                    self.send_error(500)
                    return
                first = len(requests) == (2 if scenario == "retry" else 1)
                if kind == "codex":
                    if first:
                        tools = {tool["name"] for tool in request["tools"] if "name" in tool}
                        name = next(n for n in ("exec_command", "shell_command", "shell") if n in tools)
                        args = {"cmd": command, "yield_time_ms": 1000} if name == "exec_command" else (
                            {"command": command, "timeout_ms": 180000} if name == "shell_command"
                            else {"command": ["bash", "-lc", command], "timeout_ms": 180000}
                        )
                        item = {"type": "function_call", "id": "fc1", "call_id": "call1", "name": name,
                                "arguments": json.dumps(args), "status": "completed"}
                    else:
                        item = {"id": "msg1", "type": "message", "role": "assistant", "status": "completed",
                                "content": [{"type": "output_text", "text": "done", "annotations": []}]}
                    events = [
                        {"type": "response.created", "response": {"id": "resp", "status": "in_progress"}},
                        {"type": "response.output_item.done", "output_index": 0, "item": item},
                        {"type": "response.completed", "response": {
                            "id": "resp", "status": "completed", "output": [item],
                            "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
                        }},
                    ]
                    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                else:
                    tool_command = command if first else "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT; echo done"
                    message = {"role": "assistant", "content": "done"}
                    tool = first or kind == "mini"
                    if tool:
                        message["tool_calls"] = [{"id": f"call{len(requests)}", "type": "function", "function": {
                            "name": "exec" if kind == "openclaw" else "bash",
                            "arguments": json.dumps({"command": tool_command}),
                        }}]
                    finish = "tool_calls" if tool else "stop"
                    if request.get("stream"):
                        delta = dict(message)
                        if tool:
                            delta["tool_calls"] = [{"index": 0, **message["tool_calls"][0]}]
                        chunks = [
                            {"id": f"chat{len(requests)}", "object": "chat.completion.chunk", "model": "mock", "choices": [
                                {"index": 0, "delta": delta, "finish_reason": None}]},
                            {"id": f"chat{len(requests)}", "object": "chat.completion.chunk", "model": "mock", "choices": [
                                {"index": 0, "delta": {}, "finish_reason": finish}],
                             "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}},
                        ]
                        body = ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
                        content_type = "text/event-stream"
                    else:
                        body = json.dumps({"id": "chat", "object": "chat.completion", "model": "mock",
                                           "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                                           "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}).encode()
                        content_type = "application/json"
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return server, worker, requests, blocked, release


@pytest.mark.parametrize("kind,scenario", [
    *product(["codex", "pi", "mini"], [
        "normal", "stop_request", "stop_tool", "authentication", "context", "retry",
        "retry_after_tool", "disconnect_after_tool",
    ]),
    ("pi", "context_text"),
])
def test_native_lifecycle(tmp_path, monkeypatch, kind, scenario):
    root_value = os.environ.get("ENVWEAVER_TEST_NATIVE_ROOT")
    if not root_value:
        pytest.skip("requires ENVWEAVER_TEST_NATIVE_ROOT with dependencies/node, npm and venv")
    root = Path(root_value)
    python = root / "venv/bin/python"
    binary = root / "npm/node_modules/.bin" / ("codex" if kind == "codex" else "pi")
    assert python.is_file() and (kind == "mini" or binary.is_file())
    service = tmp_path / "service.py"
    service.write_text(
        "import os,socket,json\nfrom pathlib import Path\n"
        "s=socket.socket();s.bind(('127.0.0.1',0));s.listen()\n"
        "Path('service.json').write_text(json.dumps({'pid':os.getpid(),'port':s.getsockname()[1]}))\n"
        "while True:\n c,_=s.accept()\n with c:c.sendall(b'OK')\n"
    )
    launch = (
        "import subprocess,sys;from pathlib import Path;"
        "p=Path('launches');p.write_text(p.read_text()+'1' if p.exists() else '1');"
        "subprocess.Popen([sys.executable,'service.py'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)"
    )
    command = shlex.join([str(python), "-c", launch])
    if scenario == "stop_tool":
        waiting = (
            "import os,time;from pathlib import Path;"
            "Path('tool.pid').write_text(str(os.getpid()));time.sleep(120);Path('late-tool').touch()"
        )
        command += " && " + shlex.join([str(python), "-c", waiting])
    server, worker, requests, blocked, release = start_api(kind, scenario, command)
    url = f"http://127.0.0.1:{server.server_port}/v1"
    config = tmp_path / "config"
    config.mkdir()
    env = {
        "PATH": os.pathsep.join([str(root / "dependencies/node/bin"), str(root / "npm/node_modules/.bin"),
                                str(root / "venv/bin"), os.environ["PATH"]]),
        "CODEX_HOME": str(config), "PI_CODING_AGENT_DIR": str(config),
        "MSWEA_GLOBAL_CONFIG_DIR": str(config), "MSWEA_COST_TRACKING": "ignore_errors",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True", "OPENAI_API_KEY": "mock-key", "OPENAI_API_BASE": url,
        # Exercise native retry exhaustion without spending minutes in exponential backoff.
        "MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT": "2",
    }
    if kind == "codex":
        settings = {"model_provider": "mock", "model_providers.mock.name": "Mock",
                    "model_providers.mock.base_url": url, "model_providers.mock.wire_api": "responses",
                    "model_providers.mock.requires_openai_auth": False, "web_search": "disabled"}
        agent = CodexAgent(model="mock", env=env, extra_args=[
            part for k,v in settings.items() for part in ("-c", k + "=" + json.dumps(v))])
        agent_execution = AgentExecution(agent)
    elif kind == "pi":
        (config / "models.json").write_text(json.dumps({"providers": {"mock": {
            "baseUrl": url, "api": "openai-completions", "apiKey": "mock-key",
            "models": [{"id": "mock", "input": ["text"], "reasoning": False,
                        "contextWindow": 32768, "maxTokens": 1024}],
        }}}))
        agent = PiAgent(model="mock/mock", env=env, extra_args=["--thinking", "off"])
        agent_execution = AgentExecution(agent)
    else:
        agent = MiniSweAgent(model="openai/mock", env=env)
        agent_execution = AgentExecution(agent)
    # Record the supervised PID while preserving the actual CLI command, including npm wrappers.
    native_command = agent._command
    def recorded_command(prompt, images):
        return ["/bin/sh", "-c", 'echo $$ > cli.pid; exec "$@"', "cli", *native_command(prompt, images)]
    monkeypatch.setattr(agent, "_command", recorded_command)
    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.05)
    runtime = LocalTransport()
    instance = Instance("test", "local", data={"python": str(python)})
    output = tmp_path / "output"
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        agent_execution.prepare(runtime=runtime, instance=instance, output_dir=output, cwd=str(tmp_path))
        future = executor.submit(agent_execution.run, goal=[{"type": "text", "text": "Start the test service."}],
                                 runtime=runtime, instance=instance, output_dir=output)
        if scenario.startswith("stop"):
            marker = tmp_path / "tool.pid"
            wait_for(lambda: marker.exists() if scenario == "stop_tool" else blocked.is_set(), future)
            agent_execution.stop(timeout=10)
            agent_execution.stop(timeout=10)
            with pytest.raises(InterruptedError):
                future.result(timeout=5)
            # Releasing the pending API response must not restart the CLI's Agent loop.
            count = len(requests)
            release.set()
            time.sleep(0.3)
            assert len(requests) == count
        elif scenario == "authentication":
            with pytest.raises(AgentError) as caught:
                future.result(timeout=30)
            assert caught.value.type is ErrorType.AUTHENTICATION
        elif scenario in ("context", "context_text"):
            with pytest.raises(AgentError):
                future.result(timeout=30)
        else:
            result = future.result(timeout=30)
            assert result["output"] and len(requests) == (
                3 if scenario in ("retry", "retry_after_tool", "disconnect_after_tool") else 2
            )
        # CLI wrappers and their real engines carry this attempt's private paths.
        alive = []
        marker = (agent._remote + "/").encode()
        for process in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                if marker in process.read_bytes():
                    alive.append(process.parent.name)
            except (FileNotFoundError, PermissionError):
                pass
        assert not alive, f"Agent control processes remain: {alive}"
        pid = int((tmp_path / "cli.pid").read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        if scenario not in ("authentication", "context", "context_text"):
            assert (tmp_path / "launches").read_text() == "1", "A completed tool was replayed"
            wait_for(lambda: (tmp_path / "service.json").exists(), future)
            info = json.loads((tmp_path / "service.json").read_text())
            with socket.create_connection(("127.0.0.1", info["port"]), timeout=2) as connection:
                assert connection.recv(2) == b"OK"
        assert (output / "result.json").exists()
    finally:
        release.set()
        try:
            agent_execution.stop(timeout=5)
        finally:
            # Only test-owned processes; production never does process-tree cleanup.
            for name in ("cli.pid", "tool.pid", "service.json"):
                path = tmp_path / name
                if path.exists():
                    pid = json.loads(path.read_text())["pid"] if name.endswith("json") else int(path.read_text())
                    try:
                        if Path(f"/proc/{pid}/cwd").resolve() == tmp_path:
                            os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            executor.shutdown(wait=True, cancel_futures=True)
            server.shutdown()
            server.server_close()
            worker.join()
            if agent._remote:
                shutil.rmtree(agent._remote, ignore_errors=True)
