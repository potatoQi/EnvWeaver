"""Real CLI supervision and Eval against HTTP/TCP services; no model by default."""

import json
import os
import subprocess
from dataclasses import replace

import pytest

from envweaver import CodexAgent, EvalConfig, Runtime, RuntimeRef, Task, Verifier, eval

pytestmark = pytest.mark.integration

SERVER = '''import json, socket, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')
    def log_message(self, *args): pass
http = HTTPServer(('127.0.0.1', 0), Handler)
tcp = socket.socket()
tcp.bind(('127.0.0.1', 0))
tcp.listen()
def serve_tcp():
    while True:
        connection, _ = tcp.accept()
        with connection: connection.sendall(b'OK')
threading.Thread(target=http.serve_forever, daemon=True).start()
threading.Thread(target=serve_tcp, daemon=True).start()
Path('/workspace/ready.json').write_text(json.dumps({'http':http.server_port, 'tcp':tcp.getsockname()[1]}))
while True:
    print('background service log', flush=True)
    time.sleep(0.03)
'''

CLI = '''#!/usr/local/bin/python3
import json, os, signal, subprocess, sys, time
from pathlib import Path
if '--version' in sys.argv:
    print('service-test-cli'); raise SystemExit(0)
subprocess.Popen(['python3', '/workspace/server.py'], start_new_session=os.environ['DETACHED']=='1')
while not Path('/workspace/ready.json').exists(): time.sleep(0.01)
Path('/workspace/cli.pid').write_text(str(os.getpid()))
mode = os.environ['BEHAVIOR']
if mode == 'unresponsive':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode in ('timeout', 'unresponsive'):
    time.sleep(120)
    Path('/workspace/late-cli').touch()
elif mode == 'error':
    raise SystemExit(7)
elif mode == 'limit':
    print(json.dumps({'type':'turn.failed','error':{'message':'Your input exceeds the context window of this model'}}), flush=True)
else:
    print(json.dumps({'type':'turn.completed','usage':{}}), flush=True)
'''

CHECK = '''import json, os, socket, urllib.request
from pathlib import Path
ports = json.loads(Path('/workspace/ready.json').read_text())
with urllib.request.urlopen('http://127.0.0.1:%s/' % ports['http'], timeout=3) as response:
    http_ok = response.read() == b'OK'
with socket.create_connection(('127.0.0.1', ports['tcp']), timeout=3) as connection:
    tcp_ok = connection.recv(2) == b'OK'
assert not Path('/workspace/late-cli').exists()
pidfile = Path('/workspace/cli.pid')
if pidfile.exists():
    try: os.kill(int(pidfile.read_text()), 0)
    except ProcessLookupError: pass
    else: raise AssertionError('CLI is still running')
print(json.dumps({'score':float(http_ok and tcp_ok), 'http':http_ok, 'tcp':tcp_ok}))
'''


@pytest.fixture
def service_task(tmp_path):
    image = os.environ.get("ENVWEAVER_TEST_SERVICE_IMAGE")
    if not image:
        pytest.skip("requires ENVWEAVER_TEST_SERVICE_IMAGE with EnvWeaver installed")
    material = tmp_path / "material"
    material.mkdir()
    (material / "server.py").write_text(SERVER)
    (material / "check.py").write_text(CHECK)
    binary = material / "codex"
    binary.write_text(CLI)
    binary.chmod(0o755)
    entry = tmp_path / "verifier/run.py"
    entry.parent.mkdir()
    entry.write_text(
        "import argparse, json\nfrom envweaver import Runtime\n"
        "p=argparse.ArgumentParser();p.add_argument('--instance');r=Runtime()\n"
        "i=r.connect(path=p.parse_args().instance)\n"
        "code,out,err=r.exec(instance=i,command=['python3','/workspace/check.py'],timeout=10)\n"
        "assert code == 0, err\nprint(out)\n"
    )
    instances = []

    class TrackingRuntime(Runtime):
        def _start(self, *args, **kwargs):
            instance = super()._start(*args, **kwargs)
            instances.append(instance)
            return instance

    runtime = TrackingRuntime()
    ref = RuntimeRef("docker", image, {
        "command": ["sleep", "infinity"],
        "env": {"PATH": "/usr/local/bin:/usr/bin:/bin"},
    })
    task = Task(
        task_ref="integration/service-lifetime", metadata={},
        name="service-lifetime", goal=[{"type": "text", "text": "start services"}],
        task_runtime_ref=ref, verifier_runtime_ref=ref,
        context=[{"source": str(material), "target": "/workspace"}],
        setup={"command": "cp /workspace/codex /usr/local/bin/codex", "timeout": 10},
        cwd="/workspace", verifier=Verifier(entry=entry, runtime=runtime, timeout=30),
    )
    yield task
    remaining = []
    for instance in instances:
        check = subprocess.run(
            ["docker", "--context", "default", "inspect", instance.id], capture_output=True,
        )
        if check.returncode == 0:
            remaining.append(instance.id)
            runtime.close(instance=instance)
    assert not remaining, f"Eval left containers behind: {remaining}"


@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.parametrize("behavior", ["normal", "timeout", "limit", "error"])
def test_service_survives_until_verification(service_task, tmp_path, monkeypatch, detached, behavior):
    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.05)
    result, = eval(
        tasks=service_task,
        agent=CodexAgent(env={"DETACHED": str(int(detached)), "BEHAVIOR": behavior}),
        config=EvalConfig(agent_timeout=3 if behavior == "timeout" else 30, max_retries=0),
        output_dir=tmp_path / "run",
    )
    assert not any(error["stage"] == "cleanup" for error in result["errors"]), result
    if behavior == "error":
        assert result["verification_result"] is None
        assert result["agent_stop_reason"] == "error"
    else:
        assert result["verification_result"] == {"score": 1, "http": True, "tcp": True}, result
        assert result["score_valid"]
        assert result["agent_stop_reason"] == {
            "normal": "completed", "timeout": "timeout", "limit": "context_limit",
        }[behavior]
    artifacts = tmp_path / "run/000001/agent"
    assert (artifacts / "logs/events.jsonl").is_file()
    assert (artifacts / "result.json").is_file()



def test_unresponsive_cli_skips_verification_and_closes_instance(service_task, tmp_path, monkeypatch):
    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.05)
    monkeypatch.setattr("envweaver.evaluation.runner._CLEANUP_TIMEOUT", 2)
    result, = eval(
        tasks=service_task,
        agent=CodexAgent(env={"DETACHED": "1", "BEHAVIOR": "unresponsive"}),
        config=EvalConfig(agent_timeout=3, max_retries=0), output_dir=tmp_path / "run",
    )
    assert result["agent_stop_reason"] == "timeout"
    assert result["verification_result"] is None
    assert not result["score_valid"]
    assert any(error["message"].startswith("stop:") for error in result["errors"] if error["stage"] == "cleanup")


def test_verifier_failure_still_closes_services(service_task, tmp_path, monkeypatch):
    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.05)
    entry = service_task.verifier.entry
    entry.write_text(entry.read_text() + "raise RuntimeError('verification failure')\n")
    result, = eval(
        tasks=service_task, agent=CodexAgent(env={"DETACHED": "1", "BEHAVIOR": "normal"}),
        config=EvalConfig(max_retries=0), output_dir=tmp_path / "run",
    )
    assert result["errors"][-1]["stage"] == "verify"
    assert result["verification_result"] is None
    assert not any(error["stage"] == "cleanup" for error in result["errors"])

def test_service_with_real_model(service_task, tmp_path):
    """One opt-in model attempt, using the provided local proxy and native Codex binary."""
    required = ("ENVWEAVER_TEST_SERVICE_CODEX", "ENVWEAVER_TEST_MODEL_BASE_URL",
                "ENVWEAVER_TEST_MODEL_KEY", "ENVWEAVER_TEST_MODEL_NAME")
    if not all(os.environ.get(name) for name in required):
        pytest.skip("requires an explicitly configured model and native Codex binary")
    ref = replace(service_task.task_runtime_ref, config={
        **service_task.task_runtime_ref.config, "network": "host",
    })
    task = replace(
        service_task, task_runtime_ref=ref,
        context=service_task.context + [{
            "source": os.environ["ENVWEAVER_TEST_SERVICE_CODEX"],
            "target": "/usr/local/bin/codex",
        }],
        setup=None,
        goal=[{"type": "text", "text": (
            "Start /workspace/server.py as a persistent background service using Python subprocess.Popen "
            "with start_new_session=True and stdin/stdout/stderr redirected to DEVNULL. "
            "Wait for /workspace/ready.json, then run python3 /workspace/check.py to confirm both services. "
            "Leave the service running and finish. Do not modify any files. No further inspection is needed."
        )}],
    )
    settings = {
        "model_provider": "shadow",
        "model_providers.shadow.name": "Shadow",
        "model_providers.shadow.base_url": os.environ["ENVWEAVER_TEST_MODEL_BASE_URL"],
        "model_providers.shadow.env_key": "SHADOW_API_KEY",
        "model_providers.shadow.wire_api": "responses",
        "model_providers.shadow.requires_openai_auth": False,
        "web_search": "disabled",
        "model_reasoning_effort": "low",
    }
    agent = CodexAgent(
        model=os.environ["ENVWEAVER_TEST_MODEL_NAME"],
        env={"SHADOW_API_KEY": os.environ["ENVWEAVER_TEST_MODEL_KEY"]},
        extra_args=[part for key, value in settings.items() for part in ("-c", key + "=" + json.dumps(value))],
    )
    result, = eval(
        tasks=task, agent=agent, config=EvalConfig(agent_timeout=180, max_retries=0),
        output_dir=tmp_path / "model-run",
    )
    assert result["errors"] == [], result
    assert result["verification_result"] == {"score": 1, "http": True, "tcp": True}, result
    assert result["score_valid"]
