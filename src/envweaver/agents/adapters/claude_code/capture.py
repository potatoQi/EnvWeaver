"""Standalone sandbox launcher. Observe the Messages protocol without changing CLI permissions."""

import http.client
import json
import os
import signal
import ssl
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

_MANAGED_DIR = Path("/etc/claude-code")


class MessageStream:
    """Assemble one Anthropic SSE stream, including its final cumulative usage."""

    def __init__(self):
        self.message = {}
        self.partial = {}
        self.complete = False
        self.error = None

    def accept(self, event):
        kind = event.get("type")
        if kind == "message_start":
            self.message = event["message"]
        elif kind == "content_block_start":
            self.message["content"].append(event["content_block"])
        elif kind == "content_block_delta":
            i, delta = event["index"], event["delta"]
            block = self.message["content"][i]
            field = {
                "text_delta": "text",
                "thinking_delta": "thinking",
                "signature_delta": "signature",
            }.get(delta["type"])
            if field:
                block[field] = block.get(field, "") + delta[field]
            elif delta["type"] == "input_json_delta":
                self.partial.setdefault(i, []).append(delta["partial_json"])
            else:
                block.setdefault("deltas", []).append(delta)
        elif kind == "content_block_stop" and event["index"] in self.partial:
            self.message["content"][event["index"]]["input"] = json.loads(
                "".join(self.partial.pop(event["index"]))
            )
        elif kind == "message_delta":
            self.message.update(event.get("delta", {}))
            self.message.setdefault("usage", {}).update(event.get("usage", {}))
        elif kind == "message_stop":
            self.complete = True
        elif kind == "error":
            self.error = event.get("error")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Relay(BaseHTTPRequestHandler):
    # The client owns credentials and redirects. Never persist HTTP headers.
    def log_message(self, *_):
        pass

    def forward(self):
        if not self.path.startswith("/") or self.headers.get("Transfer-Encoding"):
            self.send_error(400)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        capture = self.command == "POST" and urlsplit(self.path).path.rstrip("/").endswith(
            "/v1/messages"
        )
        key = uuid.uuid4().hex

        def record(**fields):
            with self.server.lock, self.server.log.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"id": key, **fields}, ensure_ascii=False) + "\n")

        if capture:
            try:
                record(request=json.loads(body))
            except ValueError:
                capture = False
        hop = {
            "host",
            "connection",
            "keep-alive",
            "transfer-encoding",
            "te",
            "trailer",
            "upgrade",
            "proxy-authorization",
            "proxy-connection",
        }
        headers = {k: v for k, v in self.headers.items() if k.lower() not in hop}
        headers["Accept-Encoding"] = "identity"
        req = Request(
            self.server.upstream + self.path,
            data=body or None,
            headers=headers,
            method=self.command,
        )
        try:
            response = self.server.opener.open(req, timeout=self.server.timeout_seconds)
        except HTTPError as exc:
            response = exc
        except (OSError, http.client.HTTPException) as exc:
            if capture:
                record(error={"type": "connection_error", "message": type(exc).__name__})
            self.send_error(502)
            return
        with response:
            status = response.status
            self.send_response(status)
            for k, v in response.headers.items():
                if k.lower() not in hop and k.lower() != "content-length":
                    self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            stream = MessageStream()
            raw = bytearray()
            sse = capture and response.headers.get_content_type() == "text/event-stream"
            parse_error = False
            try:
                if sse:
                    data = []
                    for line in response:
                        self.wfile.write(line)
                        if parse_error:
                            continue
                        if line.startswith(b"data:"):
                            data.append(line[5:])
                        elif not line.strip() and data:
                            try:
                                stream.accept(json.loads(b"".join(data)))
                            except (ValueError, KeyError, IndexError, TypeError):
                                parse_error = True
                            data.clear()
                else:
                    while part := response.read1(65536):
                        self.wfile.write(part)
                        if capture:
                            raw.extend(part)
            except (OSError, http.client.HTTPException):
                parse_error = True
            finally:
                if capture:
                    if not sse:
                        try:
                            msg = json.loads(raw)
                            if status < 400 and msg.get("type") == "message":
                                stream.message, stream.complete = msg, True
                            else:
                                stream.error = msg.get("error")
                        except (ValueError, AttributeError):
                            parse_error = True
                    record(
                        status=status,
                        message=stream.message,
                        complete=stream.complete and not parse_error,
                        error=stream.error,
                    )

    do_POST = do_GET = do_HEAD = do_DELETE = do_PUT = do_PATCH = forward


def capture_upstream(args):
    """Choose an upstream only when the sandbox exposes its route to the launcher."""
    providers = (
        "BEDROCK",
        "VERTEX",
        "FOUNDRY",
        "MANTLE",
        "ANTHROPIC_AWS",
        "ANTHROPIC_GOOGLE_CLOUD",
        "GATEWAY",
    )
    if any(os.getenv("CLAUDE_CODE_USE_" + p, "").lower() in ("1", "true") for p in providers):
        return None
    if explicit := os.getenv("ANTHROPIC_BASE_URL"):
        return explicit
    # Let Claude resolve profiles, policies and settings itself. In particular,
    # substituting a localhost URL can override a gateway or change login behavior.
    if not os.getenv("ANTHROPIC_API_KEY") or any(
        os.getenv(k)
        for k in (
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_API_BASE_URL",
            "CLAUDE_CODE_GATEWAY",
            "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
            "CLAUDE_CODE_MANAGED_SETTINGS_PATH",
        )
    ):
        return None
    if any(
        arg.partition("=")[0]
        in (
            "--settings",
            "--setting-sources",
            "--profile",
            "--plugin-dir",
            "--add-dir",
            "--worktree",
            "-w",
        )
        for arg in args
    ):
        return None
    config = Path(os.getenv("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    cwd = Path.cwd()
    paths = [
        config / "settings.json",
        config / "settings.local.json",
        _MANAGED_DIR / "managed-settings.json",
        _MANAGED_DIR / "managed-settings.d",
        config / "remote-settings.json",
    ]
    for directory in (cwd, *cwd.parents):
        paths.extend(
            directory / ".claude" / name for name in ("settings.json", "settings.local.json")
        )
    for path in paths:
        try:
            path.stat()
        except FileNotFoundError:
            continue
        except OSError:
            pass  # An unreadable settings source cannot establish the default route.
        return None
    return "https://api.anthropic.com"


def main():
    # Claude Code requires the sandbox marker for permission bypass when running as root.
    os.environ.setdefault("IS_SANDBOX", "1")
    cmd = sys.argv[1:]
    upstream = capture_upstream(cmd[1:])
    if upstream is None:
        print(
            "EnvWeaver: leaving routing to Claude Code; Messages requests will not be captured. "
            "For Messages capture, configure ANTHROPIC_BASE_URL in the sandbox environment instead of CLI settings.",
            file=sys.stderr,
            flush=True,
        )
        os.execvp(cmd[0], cmd)
    base = urlsplit(upstream)
    if (
        base.scheme not in ("https", "http")
        or not base.hostname
        or base.query
        or base.fragment
        or base.username
    ):
        raise ValueError("ANTHROPIC_BASE_URL requires an HTTP(S) service base URL")
    with ThreadingHTTPServer(("127.0.0.1", 0), Relay) as server:
        server.upstream = upstream.rstrip("/")
        server.timeout_seconds = float(os.environ.get("API_TIMEOUT_MS", "600000")) / 1000
        handlers = [NoRedirect()]
        if base.hostname in ("localhost", "127.0.0.1", "::1"):
            handlers.append(ProxyHandler({}))
        if ca := os.environ.get("NODE_EXTRA_CA_CERTS"):
            ctx = ssl.create_default_context()
            ctx.load_verify_locations(cafile=ca)
            handlers.append(HTTPSHandler(context=ctx))
        server.opener = build_opener(*handlers)
        server.lock = threading.Lock()
        server.log = Path(__file__).with_name("calls.jsonl")
        server.log.touch(mode=0o600)
        env = {**os.environ, "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}"}
        bypass = ",".join(filter(None, (env.get("no_proxy") or env.get("NO_PROXY"), "127.0.0.1")))
        env.update(no_proxy=bypass, NO_PROXY=bypass)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with subprocess.Popen(cmd, env=env) as child:
            signal.signal(signal.SIGTERM, lambda *_: child.terminate())
            status = child.wait()
        if status == 0 and not server.log.stat().st_size:
            print(
                "EnvWeaver: no Messages requests reached the capture relay. Check whether Claude "
                "settings override ANTHROPIC_BASE_URL.",
                file=sys.stderr,
                flush=True,
            )
        return status


if __name__ == "__main__":
    sys.exit(main())
