"""Opt-in acceptance against a real OpenAI-compatible model and installed CLI agents."""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from envweaver import (
    CodexAgent,
    EvalConfig,
    MiniSweAgent,
    PiAgent,
    Runtime,
    RuntimeRef,
    Task,
    Verifier,
    eval,
)
from envweaver.agents.execution import AgentExecution

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not all(
            os.environ.get(k)
            for k in (
                "ENVWEAVER_TEST_MODEL_IMAGE",
                "ENVWEAVER_TEST_MODEL_BASE_URL",
                "ENVWEAVER_TEST_MODEL_KEY",
                "ENVWEAVER_TEST_MODEL_NAME",
            )
        ),
        reason="requires a model endpoint, key, model name, and sandbox image with installed agents",
    ),
]


def make_agent(kind, *, key=None):
    model = os.environ["ENVWEAVER_TEST_MODEL_NAME"]
    env = {
        "ENVWEAVER_MODEL_KEY": key or os.environ["ENVWEAVER_TEST_MODEL_KEY"],
        "OPENAI_API_KEY": key or os.environ["ENVWEAVER_TEST_MODEL_KEY"],
        "OPENAI_API_BASE": os.environ["ENVWEAVER_TEST_MODEL_BASE_URL"],
        "MSWEA_COST_TRACKING": "ignore_errors",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "PI_CODING_AGENT_DIR": "/workspace/config/pi",
    }
    if kind == "codex":
        settings = {
            "model_provider": "local",
            "model_providers.local.name": "Local evaluation endpoint",
            "model_providers.local.base_url": os.environ["ENVWEAVER_TEST_MODEL_BASE_URL"],
            "model_providers.local.env_key": "ENVWEAVER_MODEL_KEY",
            "model_providers.local.wire_api": "responses",
            "model_providers.local.requires_openai_auth": False,
            "model_providers.local.supports_websockets": False,
            "model_context_window": 262144,
            "web_search": "disabled",
        }
        args = [part for k, v in settings.items() for part in ("-c", k + "=" + json.dumps(v))]
        return CodexAgent(model=model, env=env, extra_args=args)
    if kind == "pi":
        return PiAgent(model="local/" + model, env=env, extra_args=["--thinking", "off"])
    return MiniSweAgent(model="openai/" + model, env=env)


@pytest.fixture
def model_task(tmp_path):
    material = tmp_path / "material"
    config = material / "config/pi"
    config.mkdir(parents=True)
    (config / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "local": {
                        "baseUrl": os.environ["ENVWEAVER_TEST_MODEL_BASE_URL"],
                        "api": "openai-completions",
                        "apiKey": "${ENVWEAVER_MODEL_KEY}",
                        "models": [
                            {
                                "id": os.environ["ENVWEAVER_TEST_MODEL_NAME"],
                                "input": ["text", "image"],
                                "reasoning": False,
                                "contextWindow": 262144,
                                "maxTokens": 4096,
                            }
                        ],
                    }
                }
            }
        )
    )
    (material / "solution.py").write_text(
        'def total(records):\n    """Sum price * quantity for records whose active field is True; empty input returns 0."""\n    return 0\n'
    )
    (material / "test_visible.py").write_text(
        "from solution import total\nassert total([{'price':3,'quantity':2,'active':True}])==6\n"
    )
    entry = tmp_path / "checker/run.py"
    entry.parent.mkdir()
    entry.write_text(
        "import argparse,json,tempfile,importlib.util\nfrom pathlib import Path\nfrom envweaver import Runtime\n"
        "p=argparse.ArgumentParser();p.add_argument('--instance');r=Runtime();i=r.connect(path=p.parse_args().instance)\n"
        "with tempfile.TemporaryDirectory() as d:\n"
        " f=Path(d)/'solution.py';r.download(instance=i,source='/workspace/solution.py',target=f)\n"
        " spec=importlib.util.spec_from_file_location('solution',f);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n"
        " cases=[([],0),([{'price':3,'quantity':2,'active':True}],6),([{'price':9,'quantity':5,'active':False}],0),([{'price':2.5,'quantity':4,'active':True},{'price':7,'quantity':3,'active':False}],10)]\n"
        " passed=sum(m.total(rows)==expected for rows,expected in cases)\n"
        " print(json.dumps({'score':passed/len(cases),'checks':{'passed':passed,'total':len(cases)}}))\n"
    )
    runtime = Runtime(root=tmp_path / "records")
    ref = RuntimeRef(
        "docker",
        os.environ["ENVWEAVER_TEST_MODEL_IMAGE"],
        {
            "command": ["sleep", "infinity"],
            "args": [
                "--label",
                "envweaver.model-test=" + os.environ.get("ENVWEAVER_TEST_RUN_ID", "model-tests"),
            ],
        },
    )
    task = Task(
        name="repair-total",
        task_ref="integration",
        metadata={},
        goal=[
            {
                "type": "text",
                "text": "Fix total(records) in /workspace/solution.py according to its docstring. Modify the file, run python /workspace/test_visible.py, and report completion. Use the provided shell/file tools. Do not edit tests. Work without git.",
            }
        ],
        task_runtime_ref=ref,
        verifier_runtime_ref=ref,
        context=[{"source": str(material), "target": "/workspace"}],
        cwd="/workspace",
        setup={"command": "test -f /workspace/solution.py", "timeout": 10},
        verifier=Verifier(entry=entry, runtime=runtime, timeout=60),
    )
    yield task
    assert not list((tmp_path / "records/instances").glob("*/instance.json"))


def check_artifacts(directory, result):
    assert result["errors"] == [], result
    agent = result["agent_result"]
    assert isinstance(agent["output"], list)
    assert agent["metadata"]["model"]
    assert json.loads((directory / "agent/result.json").read_text()) == agent
    from envweaver.trajectory.atif import validate_trajectory

    trajectory = json.loads((directory / "agent" / agent["artifacts"]["trajectory"]).read_text())
    validate_trajectory(trajectory)
    steps = [step for step in trajectory["steps"] if step["source"] == "agent"]
    assert steps and all(step.get("extra", {}).get("context", {}).get("path") for step in steps)
    for log in agent["artifacts"]["logs"]:
        assert (directory / "agent" / log).exists()


@pytest.mark.parametrize("kind", ["codex", "pi", "mini"])
def test_real_agent_repairs_code(model_task, tmp_path, kind):
    output = tmp_path / "run"
    (result,) = eval(
        tasks=model_task,
        agent=make_agent(kind),
        config=EvalConfig(agent_timeout=240),
        output_dir=output,
    )
    check_artifacts(output / "000001", result)
    assert result["verification_result"]["score"] == 1, result


@pytest.mark.parametrize("kind", ["codex", "pi"])
def test_real_agent_reads_goal_image(model_task, tmp_path, kind):
    path = tmp_path / "goal.png"
    picture = Image.new("RGB", (600, 180), "white")
    ImageDraw.Draw(picture).text(
        (35, 35), "7319", fill="black", font=ImageFont.truetype("DejaVuSans.ttf", 90)
    )
    picture.save(path)
    Path(model_task.verifier.entry).write_text(
        "import argparse,json,tempfile\nfrom pathlib import Path\nfrom envweaver import Runtime\n"
        "p=argparse.ArgumentParser();p.add_argument('--instance');r=Runtime();i=r.connect(path=p.parse_args().instance)\n"
        "with tempfile.TemporaryDirectory() as d:\n"
        " f=Path(d)/'answer';r.download(instance=i,source='/workspace/answer.txt',target=f)\n"
        " print(json.dumps({'score':float(f.read_text().strip()=='7319')}))\n"
    )
    task = replace(
        model_task,
        name="image",
        goal=[
            {
                "type": "text",
                "text": "Read the four digits in the attached picture and write ONLY those digits to /workspace/answer.txt using your tools. Do not solve any coding task. Then report completion.",
            },
            {"type": "image", "path": str(path)},
        ],
    )
    output = tmp_path / "run"
    (result,) = eval(
        tasks=task, agent=make_agent(kind), config=EvalConfig(agent_timeout=240), output_dir=output
    )
    check_artifacts(output / "000001", result)
    assert result["verification_result"]["score"] == 1, result


def test_real_batch(model_task, tmp_path):
    tasks = [replace(model_task, name=f"repair-{i}") for i in range(4)]
    results = eval(
        tasks=iter(tasks),
        agent=make_agent("pi"),
        config=EvalConfig(agent_timeout=240),
        output_dir=tmp_path / "batch",
        concurrency=2,
    )
    assert [r["name"] for r in results] == [t.name for t in tasks]
    for index, result in enumerate(results, 1):
        check_artifacts(tmp_path / f"batch/{index:06d}", result)
        assert result["verification_result"]["score"] == 1


@pytest.mark.parametrize("kind", ["codex", "pi", "mini"])
def test_real_agent_stop_preserves_sandbox(model_task, tmp_path, kind):
    runtime = model_task.verifier.runtime
    instance = runtime.start(
        ref=model_task.task_runtime_ref,
        context=model_task.context,
        setup={
            "command": [
                "python3",
                "-c",
                "import subprocess; from pathlib import Path; p=subprocess.Popen(['python3','-c','import time; time.sleep(300)'],start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); Path('/workspace/service.pid').write_text(str(p.pid))",
            ],
            "timeout": 10,
        },
    )
    agent = make_agent(kind)
    agent_execution = AgentExecution(agent)
    output = tmp_path / "agent"
    errors = []
    done = threading.Event()
    worker = None
    try:
        agent_execution.prepare(runtime=runtime, instance=instance, output_dir=output, cwd=model_task.cwd)

        def run():
            try:
                agent_execution.run(
                    goal=[
                        {
                            "type": "text",
                            "text": "Your first action must be running this exact command with your shell tool: python -c \"from pathlib import Path; import time; Path('/workspace/started').write_text('yes'); time.sleep(120); Path('/workspace/late').write_text('bad')\". Wait for it to finish. Do not run it in the background, inspect the workspace, or run any other command.",
                        }
                    ],
                    runtime=runtime,
                    instance=instance,
                    output_dir=output,
                )
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()

        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and not done.is_set():
            if (
                runtime.exec(instance=instance, command="test -f /workspace/started", timeout=5)[0]
                == 0
            ):
                break
            time.sleep(1)
        else:
            pytest.fail(f"Agent did not start the requested tool: {errors}")
        agent_execution.stop(timeout=30)
        worker.join(5)
        assert (
            not worker.is_alive() and len(errors) == 1 and isinstance(errors[0], InterruptedError)
        ), errors
        assert (
            runtime.exec(instance=instance, command="echo alive")[
                1
            ].strip()
            == "alive"
        )
        assert (
            runtime.exec(
                instance=instance,
                command=[
                    "python3",
                    "-c",
                    "import os; from pathlib import Path; os.kill(int(Path('/workspace/service.pid').read_text()), 0)",
                ],
            )[0]
            == 0
        )
        assert (output / "result.json").exists() and (output / "logs/events.jsonl").exists()
    finally:
        try:
            agent_execution.stop(timeout=30)
        finally:
            runtime.close(instance=instance)
            if worker is not None:
                worker.join(5)


def test_real_auth_failure_is_not_zero_score(model_task, tmp_path):
    (result,) = eval(
        tasks=model_task,
        agent=make_agent("pi", key="invalid-test-key"),
        config=EvalConfig(agent_timeout=30),
        output_dir=tmp_path / "run",
    )
    assert result["errors"][-1]["stage"] == "agent", result
    assert result["verification_result"] is None and not any(error["stage"] == "cleanup" for error in result["errors"]), result
    assert (tmp_path / "run/000001/agent/result.json").exists()


def test_real_eval_timeout_preserves_partial_artifacts(model_task, tmp_path):
    task = replace(
        model_task,
        goal=[
            {
                "type": "text",
                "text": "Use your bash tool to run `sleep 120` now and wait for it to finish. Do not run it in the background.",
            }
        ],
    )
    (result,) = eval(
        tasks=task,
        agent=make_agent("pi"),
        config=EvalConfig(agent_timeout=30),
        output_dir=tmp_path / "run",
    )
    assert result["errors"][-1]["stage"] == "agent" and result["errors"][-1]["type"] == "timeout", result
    # The unfinished implementation already passes two of the verifier's four cases.
    assert result["verification_result"]["score"] == 0.5 and not any(error["stage"] == "cleanup" for error in result["errors"]), result
    assert result["score_valid"], result
    directory = tmp_path / "run/000001/agent"
    assert json.loads((directory / "result.json").read_text()) == result["agent_result"]
    events = [
        json.loads(line) for line in (directory / "logs/events.jsonl").read_text().splitlines()
    ]
    assert any(event["type"] == "pi.tool_execution_start" for event in events)


def test_real_model_retry_after_rate_limit(model_task, tmp_path, monkeypatch):
    upstream = os.environ["ENVWEAVER_TEST_MODEL_BASE_URL"].rstrip("/")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append(self.path)
            if len(requests) == 1:
                status, content_type = 429, "application/json"
                response = b'{"error":{"message":"injected rate limit","type":"rate_limit_error"}}'
            else:
                request = urllib.request.Request(
                    upstream + self.path.removeprefix("/v1"),
                    data=body,
                    headers={
                        "Authorization": self.headers["Authorization"],
                        "Content-Type": "application/json",
                    },
                )
                try:
                    with urllib.request.urlopen(request, timeout=90) as stream:
                        status, content_type = stream.status, stream.headers["Content-Type"]
                        response = stream.read()
                except urllib.error.HTTPError as error:
                    status, content_type, response = error.code, "application/json", error.read()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("ENVWEAVER_TEST_MODEL_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    ref = replace(model_task.task_runtime_ref, config={**model_task.task_runtime_ref.config, "network": "host"})
    task = replace(model_task, task_runtime_ref=ref)
    output = tmp_path / "retry"
    try:
        result, = eval(
            tasks=task, agent=make_agent("codex"),
            config=EvalConfig(agent_timeout=240, max_retries=2, retry_delay=0.01), output_dir=output,
        )
        assert result["score_valid"] and result["verification_result"]["score"] == 1, result
        entry = json.loads((output / "manifest.json").read_text())["tasks"]["0"]
        assert len(entry["attempts"]) == 2
        first = entry["attempts"][0]["result"]
        assert first["errors"][-1]["type"] == "provider_transient"
        assert first["verification_result"] is None and not any(error["stage"] == "cleanup" for error in first["errors"])
        assert result["output_dir"] == "000001/attempt-000002"
        assert len(requests) > 1
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.parametrize("answer,expected", [("4", 1), ("5", 0)])
def test_real_model_verifier(model_task, tmp_path, answer, expected):
    entry = Path(model_task.verifier.entry)
    entry.write_text(
        "import json,os,urllib.request\nfrom pathlib import Path\n"
        "schema={'type':'object','properties':{'score':{'type':'integer','enum':[0,1]},'reason':{'type':'string'}},'required':['score','reason'],'additionalProperties':False}\n"
        "payload={'model':os.environ['MODEL_NAME'],'messages':[{'role':'user','content':'Grade this answer to 2+2. score=1 only if mathematically correct, else score=0. Answer: '+Path('answer.txt').read_text()+'. Return JSON with score and reason.'}],'max_tokens':128,'temperature':0,'chat_template_kwargs':{'enable_thinking':False},'response_format':{'type':'json_schema','json_schema':{'name':'grade','strict':True,'schema':schema}}}\n"
        "req=urllib.request.Request(os.environ['MODEL_URL']+'/chat/completions',data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+os.environ['MODEL_KEY'],'Content-Type':'application/json'})\n"
        "with urllib.request.urlopen(req,timeout=60) as response:result=json.load(response)\n"
        "print(json.dumps(json.loads(result['choices'][0]['message']['content'])))\n"
    )
    source = tmp_path / "answer.txt"
    source.write_text(answer)
    runtime = model_task.verifier.runtime
    instance = runtime.start(ref=model_task.verifier_runtime_ref)
    try:
        verifier = Verifier(
            entry=entry,
            runtime=runtime,
            timeout=90,
            requires_task_instance=False,
            required_env=["MODEL_URL", "MODEL_KEY", "MODEL_NAME"],
        )
        result = verifier.run(
            verifier_instance=instance,
            files=[{"source": source, "target": "answer.txt"}],
            env={
                "MODEL_URL": os.environ["ENVWEAVER_TEST_MODEL_BASE_URL"],
                "MODEL_KEY": os.environ["ENVWEAVER_TEST_MODEL_KEY"],
                "MODEL_NAME": os.environ["ENVWEAVER_TEST_MODEL_NAME"],
            },
        )
        assert result["score"] == expected and result["reason"], result
    finally:
        runtime.close(instance=instance)
