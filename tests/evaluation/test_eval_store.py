import json
from dataclasses import replace
from pathlib import Path

import pytest

from envweaver import eval
from envweaver._io.output_lock import output_lock
from envweaver.evaluation.store import EvalStore, inspect_task
from tests.evaluation.test_eval import GoodAgent, config
from tests.evaluation.test_eval import task as task_fixture
from tests.io.test_output_addresses import address
from tests.io.test_output_addresses import ssh_files as ssh_files_fixture

task = task_fixture
ssh_files = ssh_files_fixture


@pytest.mark.parametrize("remote", [False, True])
def test_live_batch_cannot_be_resumed_concurrently(tmp_path, task, ssh_files, remote):
    output = tmp_path / "batch"
    target = address(output) if remote else output
    with EvalStore(target, False):
        before = (output / "manifest.json").read_bytes()
        with pytest.raises(FileExistsError, match="Another eval"):
            eval(tasks=task, agent=GoodAgent(), config=config(), output_dir=target, resume=True)
        assert (output / "manifest.json").read_bytes() == before
        assert not task.verifier.runtime.starts
    result, = eval(tasks=task, agent=GoodAgent(), config=config(), output_dir=target, resume=True)
    assert result["score_valid"]


def test_output_lock_released_on_exception(tmp_path):
    with pytest.raises(RuntimeError, match="cancel"):
        with output_lock(tmp_path / ".lock"):
            raise RuntimeError("cancel")
    with output_lock(tmp_path / ".lock") as process:
        assert process.poll() is None


def test_lost_lock_stops_manifest_writes(tmp_path):
    with EvalStore(tmp_path / "batch", False) as store:
        before = (store.path / "manifest.json").read_bytes()
        store._writer.kill()
        store._writer.wait()
        with pytest.raises(OSError, match="lock was lost"):
            store.exhausted(0)
        assert (store.path / "manifest.json").read_bytes() == before
    with EvalStore(tmp_path / "batch", True):
        pass


def test_failed_resume_releases_lock(tmp_path):
    output = tmp_path / "batch"
    with EvalStore(output, False):
        pass
    path = output / "manifest.json"
    original = path.read_text()
    path.write_text("{}")
    with pytest.raises(ValueError, match="Invalid Eval manifest"):
        with EvalStore(output, True):
            pass
    path.write_text(original)
    with EvalStore(output, True):
        pass


@pytest.mark.parametrize("invalid", [
    None, [], {}, {"format": 1}, {"format": True, "config": "", "tasks": {}, "count": 0},
    {"format": 1, "config": "", "tasks": {"0": {}}, "count": 1},
])
def test_invalid_manifest_is_not_overwritten(tmp_path, invalid):
    output = tmp_path / "batch"
    output.mkdir()
    path = output / "manifest.json"
    path.write_text(json.dumps(invalid))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        with EvalStore(output, True):
            pass
    assert path.read_bytes() == before


def test_missing_resume_does_not_create_directory(tmp_path):
    output = tmp_path / "missing"
    with pytest.raises(ValueError, match="existing"):
        EvalStore(output, True)
    assert not output.exists()


def test_resume_before_task_count_is_known(tmp_path, task):
    output = tmp_path / "batch"
    with EvalStore(output, False) as batch:
        batch.register(1, task)

    with EvalStore(output, True) as batch:
        with pytest.raises(ValueError, match="Task count differs"):
            batch.exhausted(1)
        batch.exhausted(2)
        # A worker for the first task may register after the iterator ends.
        batch.register(0, task)


@pytest.mark.parametrize("target", ["result", "attempt"])
@pytest.mark.parametrize("invalid_errors", [
    None, {}, "failed", ["failure"],
    [{"stage": "agent", "type": "misspelled", "message": "bad type"}],
    [{"stage": "agent", "type": "transport", "message": "bad field", "kind": "transport"}],
    [{"stage": "invalid", "type": "transport", "message": "bad stage"}],
    [{"stage": "agent", "type": "transport", "message": None}],
])
def test_resume_rejects_invalid_error_lists_without_rewriting_results(tmp_path, task, target, invalid_errors):
    output = tmp_path / "batch"
    kwargs = dict(tasks=task, agent=GoodAgent(), config=config(), output_dir=output)
    eval(**kwargs)
    path = output / "manifest.json"
    data = json.loads(path.read_text())
    entry = data["tasks"]["0"]
    result = entry["result"] if target == "result" else entry["attempts"][0]["result"]
    result["errors"] = invalid_errors
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Invalid Eval manifest"):
        eval(**kwargs, resume=True)
    assert path.read_bytes() == before
    assert len(task.verifier.runtime.starts) == 2


def test_input_checks_do_not_read_the_same_address_twice(task, monkeypatch):
    import envweaver.evaluation.store as store

    original = store._dispatch
    calls = []

    def dispatch(address, payload):
        calls.append(str(address))
        return original(address, payload)

    task = replace(task, goal=[{"type": "image", "path": task.context[0]["source"]}])
    monkeypatch.setattr(store, "_dispatch", dispatch)
    _, error = inspect_task(task)
    assert error is None
    assert len(calls) == len(set(calls))
    assert set(calls) == {
        task.context[0]["source"], str(task.verifier.entry), str(Path(task.verifier.entry).parent),
    }


def test_configuration_redacts_nested_secrets_without_mutating_inputs():
    from envweaver import CodexAgent
    from envweaver.evaluation.store import configuration

    agent = CodexAgent(model="model", env={"API_KEY": "agent-secret"},
                       extra_args=["--credential", "cli-secret"])
    settings = config(task_runtime_config={"env": {"TOKEN": "runtime-secret"}},
                      verifier_connection={"nested": {"password": "connection-secret"}})
    settings.verifier_env["REQUIRED"] = "verifier-secret"
    record = configuration(agent, settings)
    serialized = json.dumps(record)
    for secret in ("agent-secret", "cli-secret", "runtime-secret", "connection-secret", "verifier-secret"):
        assert secret not in serialized
    assert record["values"]["agent"]["config"]["model"] == "model"
    assert record["values"]["eval"]["setup_timeout"] == 600
    assert settings.task_runtime_config["env"]["TOKEN"] == "runtime-secret"
    assert agent._env["API_KEY"] == "agent-secret"
    agent._env["API_KEY"] = "execution-only"
    assert configuration(agent, settings) == record
    rotated_agent = CodexAgent(model="model", env={"API_KEY": "rotated-secret"},
                               extra_args=["--credential", "cli-secret"])
    rotated = configuration(rotated_agent, settings)
    assert rotated["values"] == record["values"]
    assert rotated["fingerprint"] != record["fingerprint"]
