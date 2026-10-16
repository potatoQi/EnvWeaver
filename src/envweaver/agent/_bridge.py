import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ToolBridge:
    def __init__(self, tools):
        definitions = {tool.name: tool for tool in tools}
        if len(definitions) != len(tools):
            raise ValueError("Tool names must be unique")
        self.tools = tools
        self.token = secrets.token_urlsafe(32)
        token = self.token

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def handle_request(self):
                if not hmac.compare_digest(
                    self.headers.get("Authorization", ""), f"Bearer {token}"
                ):
                    self.send_error(401)
                    return
                try:
                    if self.command == "GET" and self.path == "/tools":
                        result = [tool.definition() for tool in tools]
                    elif self.command == "POST" and self.path == "/call":
                        size = int(self.headers.get("Content-Length", 0))
                        if size < 0 or size > 1024 * 1024:
                            raise ValueError("Tool arguments too large")
                        request = json.loads(self.rfile.read(size))
                        result = definitions[request["name"]].execute(**request["arguments"])
                    else:
                        self.send_error(404)
                        return
                except Exception as error:
                    result = {
                        "content": [{"type": "text", "text": f"{type(error).__name__}: {error}"}],
                        "isError": True,
                    }
                encoded = json.dumps(result, allow_nan=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            do_GET = do_POST = handle_request

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # Do not return while a tool is still mutating the target after pi exits.
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
