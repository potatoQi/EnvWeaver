"""Resolve each Task's verifier environment without sharing or persisting secrets."""

import json
from copy import copy
from dataclasses import replace

import pytest

from envweaver import EvalConfig, eval
from tests.evaluation.test_eval import GoodAgent
from tests.evaluation.test_eval import task as task_fixture

task = task_fixture


@pytest.mark.parametrize("process_value,override,expected", [
    ("process-secret", {}, "process-secret"),
    ("process-secret", {"REQUIRED": "override-secret"}, "override-secret"),
    (None, {"REQUIRED": "explicit-secret"}, "explicit-secret"),
    ("", {}, ""),
    ("process-secret", {"REQUIRED": ""}, ""),
])
def test_required_environment_and_explicit_overrides(tmp_path, task, monkeypatch, process_value, override, expected):
    monkeypatch.delenv("REQUIRED", raising=False)
    if process_value is not None:
        monkeypatch.setenv("REQUIRED", process_value)
    monkeypatch.setenv("EW_UNRELATED", "unrelated-secret")
    config = EvalConfig(verifier_env={**override, "EXTRA": "explicit-extra-secret"})
    original = dict(config.verifier_env)
    output = tmp_path / "batch"

    (result,) = eval(tasks=task, agent=GoodAgent(), config=config, output_dir=output)

    assert result["errors"] == []
    assert task.verifier.calls[0]["env"] == {"REQUIRED": expected, "EXTRA": "explicit-extra-secret"}
    assert config.verifier_env == original
    assert task.verifier.required_env == ["REQUIRED"]
    for filename in ("manifest.json", "results.json", "000001/result.json"):
        saved = (output / filename).read_text()
        for secret in ("process-secret", "override-secret", "explicit-secret", "unrelated-secret", "explicit-extra-secret"):
            assert secret not in saved
    settings = json.loads((output / "manifest.json").read_text())["tasks"]["0"]["attempts"][0]["configuration"]["values"]["eval"]
    assert settings["verifier_env"] == {name: "[REDACTED]" for name in original}


def test_batch_resolves_different_requirements_and_isolates_missing_values(tmp_path, task, monkeypatch):
    monkeypatch.setenv("EW_FIRST", "first-secret")
    monkeypatch.setenv("EW_SECOND", "second-secret")
    monkeypatch.delenv("EW_MISSING", raising=False)
    monkeypatch.setenv("EW_UNRELATED", "unrelated-secret")
    tasks = []
    for name in ("EW_MISSING", "EW_FIRST", "EW_SECOND"):
        verifier = copy(task.verifier)
        verifier.required_env = [name]
        verifier.calls = []
        tasks.append(replace(task, name=name, verifier=verifier))

    results = eval(tasks=tasks, agent=GoodAgent(), output_dir=tmp_path / "batch", concurrency=3)

    assert results[0]["errors"] == [{
        "stage": "preflight", "type": "configuration",
        "message": "Missing verifier environment variables: EW_MISSING",
    }]
    assert tasks[0].verifier.calls == []
    for item, result, value in zip(tasks[1:], results[1:], ("first-secret", "second-secret"), strict=True):
        assert result["errors"] == []
        assert item.verifier.calls[0]["env"] == {item.name: value}
    assert len(task.verifier.runtime.starts) == 4
    assert not task.verifier.runtime.active


def test_no_required_names_passes_only_explicit_values(tmp_path, task, monkeypatch):
    task.verifier.required_env = []
    monkeypatch.setenv("EW_UNRELATED", "unrelated-secret")
    (result,) = eval(tasks=task, agent=GoodAgent(), output_dir=tmp_path / "batch")
    assert result["errors"] == []
    assert task.verifier.calls[0]["env"] == {}


def test_environment_is_resolved_before_agent_runs(tmp_path, task, monkeypatch):
    monkeypatch.setenv("REQUIRED", "preflight-value")

    class ChangingAgent(GoodAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            monkeypatch.setenv("REQUIRED", "later-value")
            return super().run(goal=goal, runtime=runtime, instance=instance, output=output, cwd=cwd)

    (result,) = eval(tasks=task, agent=ChangingAgent(), output_dir=tmp_path / "batch")
    assert result["errors"] == []
    assert task.verifier.calls[0]["env"] == {"REQUIRED": "preflight-value"}
