"""Forward a controller-local TCP port to the CUA service of one Eval instance."""

import socketserver
import subprocess
import threading
from contextlib import contextmanager

from ....runtime import _ssh

# Executed only inside Docker. QEMU uses OpenSSH's native direct TCP channel.
_DOCKER_RELAY = """
import os, selectors, socket, sys
with socket.create_connection(('127.0.0.1', int(sys.argv[1]))) as sock:
    with selectors.DefaultSelector() as sel:
        sel.register(0, selectors.EVENT_READ)
        sel.register(sock, selectors.EVENT_READ)
        while True:
            for key, _ in sel.select():
                if key.fileobj == 0:
                    data = os.read(0, 65536)
                    if not data:
                        sys.exit(0)
                    sock.sendall(data)
                else:
                    data = sock.recv(65536)
                    if not data:
                        sys.exit(0)
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
"""


class _Forwarder(socketserver.TCPServer):
    def process_request(self, sock, address):
        self.children = {child for child in self.children if child.poll() is None}
        try:
            child = subprocess.Popen(
                self.command, stdin=sock, stdout=sock, stderr=subprocess.DEVNULL, env=self.env
            )
            self.children.add(child)
        finally:
            sock.close()


@contextmanager
def forward(command, env=None):
    with _Forwarder(("127.0.0.1", 0), None) as server:
        server.children, server.command, server.env = set(), command, env
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            thread.join()
            for child in server.children:
                child.kill()
                child.wait()


@contextmanager
def desktop(runtime, instance, port):
    if instance.backend == "qemu":
        args = _ssh.command_args(instance.data["ssh"], [])
        # Keep jump-host and identity options, replace the remote command with -W.
        with forward([*args[:-3], "-W", f"127.0.0.1:{port}", *args[-3:-1]]) as url:
            yield url
    elif instance.backend == "docker":
        backend = runtime._backend("docker")
        with backend._connection(instance.host, instance.data["host_ssh"]) as (args, env):
            command = [
                "docker",
                *args,
                "exec",
                "-i",
                instance.id,
                instance.data["python"],
                "-c",
                _DOCKER_RELAY,
                str(port),
            ]
            with forward(command, env) as url:
                yield url
    else:
        raise ValueError("AleClawAgent requires a Docker or QEMU task instance")
