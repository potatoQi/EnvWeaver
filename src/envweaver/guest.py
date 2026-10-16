"""Optional control service, installed and run inside a prepared target image."""

import argparse
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from ._files import pack, unpack
from ._kernel import Kernel
from .runtime._command import execute


class GuestServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, token, execution_python=None):
        if not token:
            raise ValueError("A guest service token is required")
        self.token = token
        self.execution_python = execution_python
        self.kernels = {}
        self.kernel_lock = threading.Lock()
        super().__init__(address, GuestHandler)

    def kernel(self, session):
        if not isinstance(session, str) or not session:
            raise ValueError("A nonempty session is required")
        with self.kernel_lock:
            if session not in self.kernels:
                self.kernels[session] = Kernel(self.execution_python)
            return self.kernels[session]

    def close_kernel(self, session):
        with self.kernel_lock:
            kernel = self.kernels.pop(session, None)
        if kernel:
            with kernel.lock:
                kernel.close()

    def server_close(self):
        for session in list(self.kernels):
            self.close_kernel(session)
        super().server_close()


class GuestHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _reply(self, status, payload, headers=None):
        if isinstance(payload, dict):
            payload = json.dumps(payload, allow_nan=False).encode()
            headers = {"Content-Type": "application/json", **(headers or {})}
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self):
        expected = f"Bearer {self.server.token}"
        if not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            return self._reply(401, {"error": "unauthorized"})
        parsed = urlsplit(self.path)
        params = parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length", 0))
        if length < 0 or length > 512 * 1024 * 1024:
            return self._reply(413, {"error": "request too large"})
        body = self.rfile.read(length)
        try:
            if parsed.path == "/health" and self.command == "GET":
                result = {"ready": True}
            elif parsed.path == "/exec" and self.command == "POST":
                result = execute(**json.loads(body))
            elif parsed.path == "/files" and self.command == "GET":
                kind, payload = pack(params["path"][0])
                return self._reply(200, payload, {"X-EnvWeaver-Kind": kind})
            elif parsed.path == "/files" and self.command == "POST":
                unpack(self.headers["X-EnvWeaver-Kind"], body, params["path"][0])
                result = {"ok": True}
            elif parsed.path == "/ipython" and self.command == "POST":
                request = json.loads(body)
                timeout = request.get("timeout", 300)
                if not isinstance(timeout, (int, float)) or not 0 < timeout <= 86400:
                    raise ValueError("timeout must be between 0 and 86400 seconds")
                result = self.server.kernel(request["session"]).run(request["code"], timeout)
            elif parsed.path == "/ipython" and self.command == "DELETE":
                self.server.close_kernel(params["session"][0])
                result = {"ok": True}
            else:
                return self._reply(404, {"error": "unknown endpoint"})
            self._reply(200, result)
        except Exception as error:
            self._reply(500, {"error": type(error).__name__, "message": str(error)})

    do_GET = do_POST = do_DELETE = _handle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token-env", default="ENVWEAVER_GUEST_TOKEN")
    parser.add_argument("--execution-python")
    args = parser.parse_args()
    with GuestServer(
        (args.host, args.port), os.environ.get(args.token_env), args.execution_python
    ) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
