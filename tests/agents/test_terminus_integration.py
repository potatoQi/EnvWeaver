"""Pinned Harbor, real Docker/tmux, scripted HTTP and optional real model acceptance."""

import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from envweaver import EvalConfig, Terminus2Agent, eval
from tests.agents.test_service_integration import service_task as service_task

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ENVWEAVER_TEST_TERMINUS_IMAGE"), reason="requires Terminus test image"
    ),
]


def response(command="", *, complete=False):
    return json.dumps(
        {
            "analysis": "Test observation.",
            "plan": "Execute the next step.",
            "commands": [{"keystrokes": command + "\n", "duration": 0.1}] if command else [],
            "task_complete": complete,
        }
    )


@contextmanager
def api(script, *, usage=None):
    requests = []
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((body, self.headers.get("Authorization")))
            item = (
                script(body)
                if callable(script)
                else script[min(len(requests) - 1, len(script) - 1)]
            )
            if item is None:
                release.wait(45)
                item = response(complete=True)
            if item == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if item == "truncated_http":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.write(b'{"choices": [')
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            finish = item.get("finish_reason", "stop") if isinstance(item, dict) else "stop"
            if isinstance(item, dict):
                item = item["content"]
            status, content = item if isinstance(item, tuple) else (200, item)
            data = (
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "mock",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": finish,
                        }
                    ],
                    "usage": usage or {
                        "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150
                    },
                }
                if status == 200
                else {
                    "error": {
                        "message": content,
                        "type": "test_error",
                        "code": "context_length_exceeded" if status == 400 else str(status),
                    }
                }
            )
            try:
                encoded = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join()


def make_agent(url, **options):
    return Terminus2Agent(
        model="openai/mock",
        env={"OPENAI_API_KEY": "terminus-test-secret", "LITELLM_LOCAL_MODEL_COST_MAP": "True"},
        options={
            "api_base": url,
            "max_turns": 6,
            "enable_summarize": False,
            "model_info": {
                "max_input_tokens": 32768,
                "max_output_tokens": 4096,
                "input_cost_per_token": 0,
                "output_cost_per_token": 0,
            },
            **options,
        },
    )


START = "python3 /workspace/server.py >/workspace/service.log 2>&1 &"


@pytest.mark.parametrize(
    "scenario",
    [
        "completed",
        "invalid_json",
        "retry",
        "step_limit",
        "timeout_request",
        "timeout_command",
        "authentication",
        "context_limit",
        "completed_at_cap",
        "first_completion_at_cap",
        "timeout_ignores_signal",
        "timeout_queued_input",
        "unicode_cwd",
        "disconnect",
        "truncated_http",
        "truncated_output",
    ],
)
def test_native_terminus_eval(service_task, tmp_path, monkeypatch, scenario):
    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.05)
    task = replace(
        service_task,
        task_runtime_ref=replace(
            service_task.task_runtime_ref,
            image=os.environ["ENVWEAVER_TEST_TERMINUS_IMAGE"],
            config={"command": ["sleep", "infinity"], "network": "host"},
        ),
        setup=None,
    )
    success = [response(START), response(complete=True), response(complete=True)]
    scripts = {
        "completed": success,
        "completed_at_cap": success,
        "first_completion_at_cap": [response(START, complete=True)],
        "unicode_cwd": [response(START + " pwd > /workspace/agent-cwd")] + success[1:],
        "invalid_json": ["this is not JSON"] + success,
        "retry": [(429, "rate limited")] + success,
        "disconnect": ["disconnect"] + success,
        "truncated_http": ["truncated_http"] + success,
        "truncated_output": [{"content": '{"analysis": "incomplete', "finish_reason": "length"}]
        + success,
        "step_limit": [response(START)],
        "timeout_request": [response(START), None],
        "timeout_command": [response(START + " sleep 120; touch /workspace/late-cli"), None],
        "timeout_queued_input": [
            response(START + " sleep 120"),
            response("touch /workspace/late-cli"),
            None,
        ],
        "timeout_ignores_signal": [
            response(
                START
                + " python3 -c \"import signal,time,os;from pathlib import Path;signal.signal(signal.SIGINT,signal.SIG_IGN);signal.signal(signal.SIGTERM,signal.SIG_IGN);Path('/workspace/cli.pid').write_text(str(os.getpid()));time.sleep(120);Path('/workspace/late-cli').touch()\""
            ),
            None,
        ],
        "authentication": [(401, "invalid API key")],
        "context_limit": [(400, "maximum context length exceeded")],
    }
    if scenario == "context_limit":
        task = replace(task, setup={"command": START, "timeout": 10})
    if scenario == "unicode_cwd":
        from pathlib import Path

        task = replace(
            task,
            cwd="/workspace/中文 dir",
            setup={"command": "mkdir -p '/workspace/中文 dir'", "timeout": 10},
        )
        check = Path(task.context[0]["source"]) / "check.py"
        check.write_text(
            check.read_text()
            + "\nassert Path('/workspace/agent-cwd').read_text().strip() == '/workspace/中文 dir'\n"
        )
    with api(scripts[scenario]) as (url, requests):
        agent = make_agent(
            url,
            max_turns=(
                1
                if scenario in ("step_limit", "first_completion_at_cap")
                else 3
                if scenario == "completed_at_cap"
                else 6
            ),
        )
        (result,) = eval(
            tasks=task,
            agent=agent,
            config=EvalConfig(agent_timeout=7 if scenario.startswith("timeout") else 60),
            output_dir=tmp_path / "run",
        )
        assert not [e for e in result["errors"] if e["stage"] == "cleanup"], result
        if scenario == "authentication":
            assert result["agent_stop_reason"] == "error", result
            assert result["errors"][0]["type"] == "authentication", result
            assert result["verification_result"] is None
            # Harbor's underlying client may retry an HTTP request before raising.
            assert 1 <= len(requests) <= 3
        else:
            assert result["score_valid"], result
            assert result["verification_result"] == {"score": 1, "http": True, "tcp": True}, result
            reason = (
                "timeout"
                if scenario.startswith("timeout")
                else "step_limit"
                if scenario == "first_completion_at_cap"
                else scenario
                if scenario in ("step_limit", "context_limit")
                else "completed"
            )
            assert result["agent_stop_reason"] == reason, result
        assert requests and all(auth == "Bearer terminus-test-secret" for _, auth in requests)
        artifacts = tmp_path / "run/000001/agent"
        for file in artifacts.rglob("*"):
            if file.is_file():
                assert "terminus-test-secret" not in file.read_text()
        assert (artifacts / "logs/terminus/trajectory.json").is_file()
        if scenario == "completed":
            assert len(requests) == 3
            assert result["agent_result"]["metadata"]["usage"]["n_input_tokens"] == 300


@pytest.mark.parametrize("interrupted", [False, True])
def test_native_usage_and_trajectory_are_consistent(service_task, tmp_path, monkeypatch, interrupted):
    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.05)
    task = replace(
        service_task,
        task_runtime_ref=replace(
            service_task.task_runtime_ref,
            image=os.environ["ENVWEAVER_TEST_TERMINUS_IMAGE"],
            config={"command": ["sleep", "infinity"], "network": "host"},
        ),
        setup=None,
    )
    script = [response(START), None] if interrupted else [
        response(START), response(complete=True), response(complete=True)
    ]
    with api(script, usage={
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "prompt_tokens_details": {"cached_tokens": 20},
    }) as (url, requests):
        (result,) = eval(
            tasks=task,
            agent=make_agent(url, model_info={
                "max_input_tokens": 32768,
                "max_output_tokens": 4096,
                "input_cost_per_token": 0.000002,
                "output_cost_per_token": 0.000004,
            }),
            config=EvalConfig(agent_timeout=7 if interrupted else 60),
            output_dir=tmp_path / "metrics",
        )
    assert result["score_valid"] and result["verification_result"]["score"] == 1, result
    assert result["agent_stop_reason"] == ("timeout" if interrupted else "completed"), result
    completed_requests = 1 if interrupted else 3
    assert len(requests) == completed_requests + int(interrupted)
    usage = result["agent_result"]["metadata"]["usage"]
    assert usage["n_input_tokens"] == completed_requests * 100
    assert usage["n_output_tokens"] == completed_requests * 50
    assert usage["n_cache_tokens"] == completed_requests * 20
    assert usage["cost_usd"] == pytest.approx(completed_requests * 0.0004)
    directory = tmp_path / "metrics/000001/agent"
    native = json.loads((directory / "logs/terminus/trajectory.json").read_text())
    for recorded, total in (
        ("total_prompt_tokens", "n_input_tokens"),
        ("total_completion_tokens", "n_output_tokens"),
        ("total_cached_tokens", "n_cache_tokens"),
        ("total_cost_usd", "cost_usd"),
    ):
        assert native["final_metrics"][recorded] == pytest.approx(usage[total])
    events = [json.loads(line) for line in (directory / "logs/events.jsonl").read_text().splitlines()]
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert [event["data"] for event in events if event["type"] == "terminus.step"] == native["steps"]
    saved = json.loads((directory / "result.json").read_text())
    assert saved == result["agent_result"]
    artifacts = saved["artifacts"]
    for path in [artifacts["trajectory"], *artifacts["logs"], *artifacts["files"]]:
        assert (directory / path).is_file(), path


def test_context_summarization_preserves_trajectories(service_task, tmp_path, monkeypatch):
    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.05)
    task = replace(
        service_task,
        task_runtime_ref=replace(
            service_task.task_runtime_ref,
            image=os.environ["ENVWEAVER_TEST_TERMINUS_IMAGE"],
            config={"command": ["sleep", "infinity"], "network": "host"},
        ),
        setup=None,
    )
    script = [
        response(START),
        (400, "maximum context length exceeded"),
        "The HTTP and TCP services are already running. Leave them running.",
        "Is /workspace/ready.json present?",
        "Yes, the services have been started.",
        response(complete=True),
        response(complete=True),
    ]
    with api(script) as (url, requests):
        (result,) = eval(
            tasks=task,
            agent=make_agent(
                url, enable_summarize=True, proactive_summarization_threshold=0,
                trajectory_config={"linear_history": True},
            ),
            config=EvalConfig(agent_timeout=90),
            output_dir=tmp_path / "summary",
        )
    assert result["agent_stop_reason"] == "completed", result
    assert result["score_valid"] and result["verification_result"]["score"] == 1, result
    assert not result["errors"], result
    assert len(requests) == len(script)
    usage = result["agent_result"]["metadata"]["usage"]
    assert usage["metadata"]["summarization_count"] == 1
    assert usage["n_input_tokens"] == 600
    assert usage["n_output_tokens"] == 300
    directory = tmp_path / "summary/000001/agent"
    native_dir = directory / "logs/terminus"
    native = {path.name: json.loads(path.read_text()) for path in native_dir.glob("*.json")}
    assert len(native) == 5  # Initial segment, continuation, and three summary calls.
    assert native["trajectory.json"]["continued_trajectory_ref"] == "trajectory.cont-1.json"
    assert native["trajectory.cont-1.json"]["final_metrics"]["total_prompt_tokens"] == 600
    events = [json.loads(line) for line in (directory / "logs/events.jsonl").read_text().splitlines()]
    recorded = [event["data"] for event in events if event["type"] == "terminus.step"]
    for trajectory in native.values():
        assert all(step in recorded for step in trajectory["steps"])
    for path in native_dir.iterdir():
        assert str(path.relative_to(directory)) in result["agent_result"]["artifacts"]["logs"]


@pytest.mark.skipif(
    not os.environ.get("ENVWEAVER_TEST_TERMINUS_MODEL_URL"), reason="real model opt-in"
)
def test_real_terminus_service(service_task, tmp_path, monkeypatch):
    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.2)
    task = replace(
        service_task,
        task_runtime_ref=replace(
            service_task.task_runtime_ref,
            image=os.environ["ENVWEAVER_TEST_TERMINUS_IMAGE"],
            config={"command": ["sleep", "infinity"], "network": "host"},
        ),
        setup=None,
        goal=[
            {
                "type": "text",
                "text": "Start /workspace/server.py as a persistent background service. "
                "It provides HTTP and TCP; verify /workspace/ready.json exists. "
                "Do not modify server.py or check.py. Leave the services running when finished.",
            }
        ],
    )
    agent = Terminus2Agent(
        model="openai/" + os.environ["ENVWEAVER_TEST_TERMINUS_MODEL"],
        env={
            "OPENAI_API_KEY": os.environ.get("ENVWEAVER_TEST_TERMINUS_MODEL_KEY", "unused"),
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        },
        options={
            "api_base": os.environ["ENVWEAVER_TEST_TERMINUS_MODEL_URL"],
            "max_turns": 12,
            "temperature": 0,
            "model_info": {
                "max_input_tokens": 262144,
                "max_output_tokens": 4096,
                "input_cost_per_token": 0,
                "output_cost_per_token": 0,
            },
            "llm_call_kwargs": {"max_tokens": 4096},
        },
    )
    (result,) = eval(
        tasks=task,
        agent=agent,
        config=EvalConfig(agent_timeout=240),
        output_dir=tmp_path / "real-run",
    )
    assert result["agent_stop_reason"] == "completed", result
    assert result["score_valid"], result
    assert result["verification_result"] == {"score": 1, "http": True, "tcp": True}, result
    assert not result["errors"], result


def test_four_concurrent_trials_are_isolated(service_task, tmp_path, monkeypatch):
    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.1)
    ref = replace(
        service_task.task_runtime_ref,
        image=os.environ["ENVWEAVER_TEST_TERMINUS_IMAGE"],
        config={"command": ["sleep", "infinity"], "network": "host"},
    )
    tasks = [
        replace(service_task, name=f"terminus-{i}", task_runtime_ref=ref, setup=None)
        for i in range(4)
    ]

    def respond(body):
        return (
            response(complete=True)
            if any(m["role"] == "assistant" for m in body["messages"])
            else response(START)
        )

    with api(respond) as (url, requests):
        results = eval(
            tasks=tasks,
            agent=make_agent(url),
            concurrency=4,
            config=EvalConfig(agent_timeout=90),
            output_dir=tmp_path / "batch",
        )
    assert [r["name"] for r in results] == [t.name for t in tasks]
    assert all(
        r["score_valid"] and r["verification_result"]["score"] == 1 and not r["errors"]
        for r in results
    ), results
    assert len(requests) == 12
    sessions = [
        json.loads((tmp_path / f"batch/{i:06}/agent/logs/terminus/trajectory.json").read_text())[
            "session_id"
        ]
        for i in range(1, 5)
    ]
    assert len(set(sessions)) == 4


@pytest.mark.skipif(
    not os.environ.get("ENVWEAVER_TEST_TERMINUS_INSTALL_IMAGE"), reason="dependency install opt-in"
)
def test_install_from_bare_image(tmp_path, monkeypatch):
    from envweaver import Runtime, RuntimeRef
    from envweaver.agents.execution import AgentExecution

    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.2)
    network_env = {
        key: os.environ[key]
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "PIP_INDEX_URL", "UV_INDEX_URL")
        if key in os.environ
    }
    runtime = Runtime(root=tmp_path / "runtime")
    instance = runtime.start(
        ref=RuntimeRef(
            "docker",
            os.environ["ENVWEAVER_TEST_TERMINUS_INSTALL_IMAGE"],
            {"command": ["sleep", "infinity"], "network": "host", "env": network_env},
        )
    )
    agent = Terminus2Agent(
        model="openai/mock",
        env={
            "OPENAI_API_KEY": "test-key",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "ENVWEAVER_TERMINAL_CHECK": "space ' quote $dollar = value",
            **network_env,
        },
        options={
            "api_base": "http://127.0.0.1:1/v1",
            "model_info": {"max_input_tokens": 32768, "max_output_tokens": 4096},
        },
    )
    execution = AgentExecution(agent)
    try:
        existing, _, _ = runtime.exec(
            instance=instance,
            command="command -v tmux >/dev/null && tmux new-session -d -s envweaver_existing",
        )
        execution.prepare(runtime, instance, tmp_path / "output", cwd="/tmp")
        assert agent._agent_python != instance.data.get("python", "python3")
        code, out, err = runtime.exec(
            instance=instance,
            command=[
                agent._agent_python,
                "-c",
                "import sys; from importlib.metadata import version; assert sys.version_info >= (3,12); print(version('harbor'))",
            ],
        )
        assert code == 0 and out.strip() == "0.25.0", err
        code, _, err = runtime.exec(
            instance=instance,
            command=["env", "TMUX_TMPDIR=" + agent._remote, "tmux", "has-session"],
        )
        assert code == 0, err
        code, out, err = runtime.exec(
            instance=instance,
            command=["env", "TMUX_TMPDIR=" + agent._remote, "tmux",
                     "show-environment", "-g", "ENVWEAVER_TERMINAL_CHECK"],
        )
        assert code == 0 and out.strip() == (
            "ENVWEAVER_TERMINAL_CHECK=space ' quote $dollar = value"
        ), err
        if existing == 0:
            assert runtime.exec(
                instance=instance, command=["tmux", "has-session", "-t", "envweaver_existing"],
            )[0] == 0
        assert execution.output.result["metadata"]["harbor_version"] == "0.25.0"
    finally:
        execution.stop()
        runtime.close(instance=instance)


@pytest.mark.skipif(
    not os.environ.get("ENVWEAVER_TEST_TERMINUS_MODEL_URL"), reason="real model opt-in"
)
def test_real_terminus_code_repair(service_task, tmp_path, monkeypatch):
    from pathlib import Path

    from envweaver import Verifier

    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.2)
    material = Path(service_task.context[0]["source"])
    (material / "solution.py").write_text("def total(records):\n    return 0\n")
    entry = tmp_path / "checker/run.py"
    entry.parent.mkdir()
    entry.write_text(
        "import argparse,json,tempfile,importlib.util\nfrom pathlib import Path\nfrom envweaver import Runtime\n"
        "p=argparse.ArgumentParser();p.add_argument('--instance');r=Runtime();i=r.connect(path=p.parse_args().instance)\n"
        "with tempfile.TemporaryDirectory() as d:\n"
        " f=Path(d)/'solution.py';r.download(instance=i,source='/workspace/solution.py',target=f)\n"
        " spec=importlib.util.spec_from_file_location('solution',f);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n"
        " cases=[([],0),([{'price':3,'quantity':2,'active':True}],6),([{'price':9,'quantity':5,'active':False}],0),([{'price':2.5,'quantity':4,'active':True},{'price':7,'quantity':3,'active':False}],10)]\n"
        " print(json.dumps({'score':sum(m.total(rows)==expected for rows,expected in cases)/len(cases)}))\n"
    )
    task = replace(
        service_task,
        name="terminus-code-repair",
        task_runtime_ref=replace(
            service_task.task_runtime_ref,
            image=os.environ["ENVWEAVER_TEST_TERMINUS_IMAGE"],
            config={"command": ["sleep", "infinity"], "network": "host"},
        ),
        setup=None,
        verifier=Verifier(entry=entry, runtime=service_task.verifier.runtime, timeout=30),
        goal=[
            {
                "type": "text",
                "text": "Fix /workspace/solution.py: total(records) must sum price * quantity "
                "for records with active=True, ignoring inactive records. Empty input returns zero. "
                "Preserve the function name. Test your implementation.",
            }
        ],
    )
    agent = Terminus2Agent(
        model="openai/" + os.environ["ENVWEAVER_TEST_TERMINUS_MODEL"],
        env={
            "OPENAI_API_KEY": os.environ.get("ENVWEAVER_TEST_TERMINUS_MODEL_KEY", "unused"),
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        },
        options={
            "api_base": os.environ["ENVWEAVER_TEST_TERMINUS_MODEL_URL"],
            "max_turns": 10,
            "temperature": 0,
            "enable_summarize": False,
            "model_info": {
                "max_input_tokens": 262144,
                "max_output_tokens": 4096,
                "input_cost_per_token": 0,
                "output_cost_per_token": 0,
            },
            "llm_call_kwargs": {"max_tokens": 4096},
        },
    )
    (result,) = eval(
        tasks=task,
        agent=agent,
        config=EvalConfig(agent_timeout=240),
        output_dir=tmp_path / "real-code",
    )
    assert result["agent_stop_reason"] == "completed", result
    assert result["score_valid"] and result["verification_result"] == {"score": 1}, result
    assert not result["errors"], result


def test_retry_exhaustion_restarts_the_whole_trial(service_task, tmp_path, monkeypatch):
    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.1)
    task = replace(
        service_task,
        task_runtime_ref=replace(
            service_task.task_runtime_ref,
            image=os.environ["ENVWEAVER_TEST_TERMINUS_IMAGE"],
            config={"command": ["sleep", "infinity"], "network": "host"},
        ),
        setup=None,
    )
    first = []

    def respond(body):
        session = body["session_id"]
        if not first:
            first.append(session)
        if session == first[0]:
            return (503, "temporarily unavailable")
        return (
            response(complete=True)
            if any(m["role"] == "assistant" for m in body["messages"])
            else response(START)
        )

    with api(respond) as (url, requests):
        (result,) = eval(
            tasks=task,
            agent=make_agent(url),
            config=EvalConfig(agent_timeout=90, max_retries=1, retry_delay=0.01),
            output_dir=tmp_path / "retry",
        )
    assert result["score_valid"] and result["verification_result"]["score"] == 1, result
    first_result = json.loads((tmp_path / "retry/000001/result.json").read_text())
    assert first_result["errors"][0]["type"] == "provider_transient"
    assert first_result["verification_result"] is None
    assert (tmp_path / "retry/000001/attempt-000002/result.json").is_file()
    assert len({body["session_id"] for body, _ in requests}) == 2


def test_failed_terminal_stop_skips_verifier(service_task, tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.1)
    monkeypatch.setattr("envweaver.evaluation.runner._CLEANUP_TIMEOUT", 2)
    wrapper = Path(service_task.context[0]["source"]) / "tmux"
    wrapper.write_text(
        '#!/bin/sh\nif [ "$1" = list-panes ]; then echo "injected inspection failure" >&2; exit 2; fi\nexec /usr/bin/tmux "$@"\n'
    )
    wrapper.chmod(0o755)
    task = replace(
        service_task,
        task_runtime_ref=replace(
            service_task.task_runtime_ref,
            image=os.environ["ENVWEAVER_TEST_TERMINUS_IMAGE"],
            config={"command": ["sleep", "infinity"], "network": "host"},
        ),
        setup=None,
    )
    with api([response(START + " sleep 120")]) as (url, requests):
        configured = make_agent(url, max_turns=1).config
        configured["env"]["PATH"] = "/workspace:/usr/local/bin:/usr/bin:/bin"
        (result,) = eval(
            tasks=task,
            agent=Terminus2Agent(**configured),
            config=EvalConfig(agent_timeout=45),
            output_dir=tmp_path / "stop-failure",
        )
    assert not result["score_valid"] and result["verification_result"] is None, result
    assert result["agent_stop_reason"] == "error", result
    assert any(e["stage"] == "cleanup" for e in result["errors"]), result
    assert any("injected inspection failure" in e["message"] for e in result["errors"]), result
