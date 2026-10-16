import json
import shutil
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from envweaver import Instance, Runtime, RuntimeRef
from envweaver.guest import GuestServer


class FilesystemBackend:
    """A test double, deliberately not a shipped/local 'sandbox' backend."""

    def __init__(self, root):
        self.root = root
        self.fail_save = False

    def start(self, ref):
        directory = self.root / uuid.uuid4().hex
        shutil.copytree(ref.image, directory)
        return Instance(directory.name, "test", str(self.root), {"path": str(directory)})

    def upload(self, instance, local_path, remote_path):
        target = Path(instance.data["path"]) / remote_path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        if Path(local_path).is_dir():
            shutil.copytree(local_path, target, dirs_exist_ok=True)
        else:
            shutil.copy2(local_path, target)

    def save(self, instance, name):
        if self.fail_save:
            raise OSError("injected snapshot failure")
        destination = self.root / uuid.uuid4().hex
        shutil.copytree(instance.data["path"], destination)
        return RuntimeRef("test", str(destination))

    def close(self, instance):
        shutil.rmtree(instance.data["path"])


@pytest.fixture
def runtime(tmp_path):
    backend = FilesystemBackend(tmp_path / "runtime")
    backend.root.mkdir()
    return Runtime(backend.root, backends={"test": backend})


@pytest.fixture
def guest(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVWEAVER_GUEST_TOKEN", "test-secret")
    server = GuestServer(("127.0.0.1", 0), "test-secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    instance = Instance(
        "test-guest", "qemu", str(tmp_path), {"guest_url": f"http://127.0.0.1:{server.server_port}"}
    )
    try:
        yield instance
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def model_server():
    requests = []
    state = {"tool": None, "arguments": {}, "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            self.send_response(state["status"])
            if state["status"] != 200:
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"failure"}}')
                return
            if not request.get("stream"):
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {"choices": [{"message": {"role": "assistant", "content": "done"}}]}
                    ).encode()
                )
                return
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if state["tool"] and len(requests) == 1:
                delta = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": state["tool"],
                                "arguments": json.dumps(state["arguments"]),
                            },
                        }
                    ],
                }
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": "done"}
                finish = "stop"
            for value in [
                {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            ]:
                self.wfile.write(
                    b"data: "
                    + json.dumps(
                        {"id": "test", "object": "chat.completion.chunk", "model": "test", **value}
                    ).encode()
                    + b"\n\n"
                )
            self.wfile.write(b"data: [DONE]\n\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
