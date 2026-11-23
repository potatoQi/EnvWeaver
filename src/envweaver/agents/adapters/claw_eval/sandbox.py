"""Run the upstream tool server in a sibling container owned by one Eval attempt."""

from functools import partial

from ....models import RuntimeRef

REVISION = "5680b8b11ff2ee5dd2b07b89086a29a5c5c984d7"
_IMAGE = "claw-eval-agent:latest"

# Bind a free loopback port in the task's network namespace. No host port or
# Docker socket is exposed and the two containers have separate filesystems.
_SERVE = """
import socket, sys
from pathlib import Path
import uvicorn
sys.path.insert(0, "/opt/sandbox")
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
sock.listen()
Path("/tmp/claw-port").write_text(str(sock.getsockname()[1]))
uvicorn.run("server:app", fd=sock.fileno())
"""

_WAIT = """
import json, time, urllib.request
from pathlib import Path
deadline = time.monotonic() + 30
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
while time.monotonic() < deadline:
    try:
        url = "http://127.0.0.1:" + Path("/tmp/claw-port").read_text()
        with opener.open(url + "/health", timeout=1) as response:
            if json.load(response)["status"] == "ok":
                print(url)
                break
    except (OSError, ValueError):
        pass
    time.sleep(0.1)
else:
    raise TimeoutError("Claw tool service did not become healthy")
"""


def tool_image_id(docker):
    """Resolve the tag on the target daemon before starting any tool container."""
    image_id = docker("image", "ls", "--quiet", "--no-trunc", _IMAGE)
    if not image_id:
        raise ValueError(
            f"Missing Claw tool image {_IMAGE}. Run on the task's Docker host:\n"
            f"docker build -f Dockerfile.agent -t {_IMAGE} "
            f"'https://github.com/claw-eval/claw-eval.git#{REVISION}'"
        )
    return image_id


def start_tools(runtime, instance):
    if instance.backend != "docker":
        raise ValueError("ClawEvalAgent requires a Docker task sandbox")
    docker = partial(
        runtime._backend("docker")._docker, host=instance.host, ssh=instance.data["host_ssh"]
    )
    image_id = tool_image_id(docker)
    mode = docker(
        "inspect",
        "--format",
        "{{.HostConfig.NetworkMode}}",
        instance.id,
    )
    if mode == "host" or mode.startswith("container:"):
        raise ValueError("ClawEvalAgent requires a task with its own network namespace")
    tools = runtime.start(
        ref=RuntimeRef(
            "docker",
            image_id,
            {
                "network": f"container:{instance.id}",
                "memory": "4g",
                "cpus": 2,
                "args": [
                    "--pull=never",
                    "--label",
                    "envweaver.role=claw-tools",
                    "--entrypoint",
                    "python",
                ],
                "command": ["-c", _SERVE],
            },
        )
    )
    code, url, stderr = runtime.exec(instance=tools, command=["python", "-c", _WAIT], timeout=40)
    if code:
        raise RuntimeError(f"Claw tool service startup failed: {stderr}")
    return url.strip()
