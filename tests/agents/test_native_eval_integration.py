"""Real native CLIs, Docker sandboxes and Verifier against a local scripted API."""

import json
import os
from dataclasses import replace
from itertools import product
from pathlib import Path

import pytest

from envweaver import CodexAgent, EvalConfig, MiniSweAgent, PiAgent, eval
from tests.agents.test_native_lifecycle_integration import start_api
from tests.agents.test_service_integration import service_task as service_task

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind,scenario", [
    *product(["codex", "pi", "mini"], ["normal", "stop_request"]),
    ("pi", "context_text"),
])
def test_native_eval(service_task, tmp_path, monkeypatch, kind, scenario):
    if not os.environ.get("ENVWEAVER_TEST_NATIVE_IMAGE"):
        pytest.skip("requires ENVWEAVER_TEST_NATIVE_IMAGE with all three native CLIs installed")
    command = (
        "python3 -c \"import subprocess,time;from pathlib import Path;"
        "subprocess.Popen(['python3','/workspace/server.py'],stdin=subprocess.DEVNULL,"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True);"
        "time.sleep(0.2)\""
    )
    server, worker, requests, blocked, release = start_api(kind, scenario, command)
    url = f"http://127.0.0.1:{server.server_port}/v1"
    config_path = Path(service_task.context[0]["source"]) / "models.json"
    config_path.write_text(json.dumps({"providers": {"mock": {
        "baseUrl": url, "api": "openai-completions", "apiKey": "mock-key",
        "models": [{"id": "mock", "input": ["text"], "reasoning": False,
                    "contextWindow": 32768, "maxTokens": 1024}],
    }}}))
    ref = replace(service_task.task_runtime_ref, image=os.environ["ENVWEAVER_TEST_NATIVE_IMAGE"], config={
        "command": ["sleep", "infinity"], "network": "host",
    })
    task = replace(service_task, task_runtime_ref=ref, setup=(
        {"command": command, "timeout": 10} if scenario == "context_text" else None
    ))
    env = {
        "PI_CODING_AGENT_DIR": "/workspace", "OPENAI_API_KEY": "mock-key", "OPENAI_API_BASE": url,
        "MSWEA_COST_TRACKING": "ignore_errors", "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    }
    if kind == "codex":
        settings = {"model_provider": "mock", "model_providers.mock.name": "Mock",
                    "model_providers.mock.base_url": url, "model_providers.mock.wire_api": "responses",
                    "model_providers.mock.requires_openai_auth": False, "web_search": "disabled"}
        agent = CodexAgent(model="mock", env=env, extra_args=[
            part for k,v in settings.items() for part in ("-c", k + "=" + json.dumps(v))])
    elif kind == "pi":
        agent = PiAgent(model="mock/mock", env=env, extra_args=["--thinking", "off"])
    else:
        agent = MiniSweAgent(model="openai/mock", env=env)
    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.05)
    try:
        result, = eval(
            tasks=task, agent=agent,
            config=EvalConfig(agent_timeout=8 if scenario == "stop_request" else 30, max_retries=0),
            output_dir=tmp_path / "run",
        )
        assert result["verification_result"] == {"score": 1, "http": True, "tcp": True}, result
        assert not any(error["stage"] == "cleanup" for error in result["errors"]) and result["score_valid"], result
        assert result["agent_stop_reason"] == {
            "normal": "completed", "stop_request": "timeout", "context_text": "context_limit",
        }[scenario]
        assert len(requests) == (1 if scenario == "context_text" else 2)
        if scenario == "stop_request":
            assert blocked.is_set()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join()
