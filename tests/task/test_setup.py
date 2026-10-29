import json
import subprocess
import sys
from pathlib import Path

import pytest

from envweaver import RuntimeRef, TaskPool, Verifier
from envweaver.errors import RuntimeError


@pytest.fixture
def prepared(tmp_path, runtime, monkeypatch):
    image = tmp_path / "image"
    image.mkdir()
    materials = tmp_path / "materials"
    materials.mkdir()
    (materials / "input.txt").write_text("input")
    (materials / "setup.py").write_text(
        "from pathlib import Path\n"
        "assert Path('materials/input.txt').read_text() == 'input'\n"
        "Path('ready.txt').write_text('ready')\n"
    )
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    (verifier / "run.py").write_text("print('{\"score\": 1}')")

    def execute(instance, command, cwd, timeout):
        result = subprocess.run(
            command,
            cwd=cwd or instance.data["path"],
            timeout=timeout,
            shell=isinstance(command, str),
            text=True,
            capture_output=True,
        )
        return result.returncode, result.stdout, result.stderr

    monkeypatch.setattr(runtime._backends["test"], "exec", execute, raising=False)
    return {
        "ref": RuntimeRef("test", str(image)),
        "context": [{"source": str(materials), "target": "/materials"}],
        "setup": {"command": [sys.executable, "materials/setup.py"], "timeout": 10},
        "verifier": Verifier(entry=verifier / "run.py", runtime=runtime, timeout=10),
    }


def test_setup_runs_after_context_and_finishes_before_return(runtime, prepared):
    instance = runtime.start(**{key: prepared[key] for key in ("ref", "context", "setup")})
    assert (Path(instance.data["path"]) / "ready.txt").read_text() == "ready"
    runtime.close(instance)


@pytest.mark.parametrize("failure", ["exit", "timeout", "transport", "context"])
def test_setup_failures_remove_instance(runtime, prepared, monkeypatch, failure):
    setup = prepared["setup"]
    expected = RuntimeError
    match = "Setup exited with code 7: not ready"
    if failure == "exit":
        setup = {
            "command": [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('not ready'); sys.exit(7)",
            ],
            "timeout": 10,
        }
    elif failure == "timeout":
        setup = {"command": [sys.executable, "-c", "import time; time.sleep(30)"], "timeout": 0.1}
        expected, match = subprocess.TimeoutExpired, "timed out"
    else:

        def fail(*args, **kwargs):
            raise OSError("unavailable")

        monkeypatch.setattr(runtime, "exec" if failure == "transport" else "upload", fail)
        expected, match = OSError, "unavailable"
    with pytest.raises(expected, match=match):
        runtime.start(ref=prepared["ref"], context=prepared["context"], setup=setup)
    assert list(runtime.root.iterdir()) == []


def test_setup_failure_preserves_original_error_when_cleanup_fails(runtime, prepared, monkeypatch):
    def fail(_):
        raise OSError("cleanup unavailable")

    monkeypatch.setattr(runtime._backends["test"], "close", fail)
    with pytest.raises(RuntimeError, match="Setup exited with code 3") as error:
        runtime.start(ref=prepared["ref"], setup={"command": "exit 3", "timeout": 10})
    assert "cleanup unavailable" in error.value.__notes__[0]


@pytest.mark.parametrize(
    "setup",
    [
        {},
        [],
        {"command": "true"},
        {"timeout": 1},
        {"command": [], "timeout": 1},
        {"command": " ", "timeout": 1},
        {"command": [1], "timeout": 1},
        {"command": "a\0b", "timeout": 1},
        {"command": "true", "timeout": 0},
        {"command": "true", "timeout": -1},
        {"command": "true", "timeout": True},
        {"command": "true", "timeout": "1"},
        {"command": "true", "timeout": float("nan")},
        {"command": "true", "timeout": float("inf")},
        {"command": "true", "timeout": 1, "typo": 1},
    ],
)
def test_invalid_setup_rejected_before_start(runtime, prepared, setup):
    with pytest.raises(ValueError, match="setup"):
        runtime.start(ref=prepared["ref"], setup=setup)
    assert list(runtime.root.iterdir()) == []


@pytest.mark.parametrize("has_setup", [False, True])
def test_task_setup_survives_delivery_and_runs_on_each_start(
    tmp_path, runtime, prepared, has_setup
):
    original = runtime.start(ref=prepared["ref"])
    pool = TaskPool(root=tmp_path / "pool", runtime=runtime)
    ref = pool.save(
        cwd="/", instance=original,
        name="ready-task",
        goal=[{"type": "text", "text": "task"}],
        verifier=prepared["verifier"],
        verifier_runtime_ref=prepared["ref"],
        context=prepared["context"],
        setup=prepared["setup"] if has_setup else None,
    )
    assert not (Path(original.data["path"]) / "ready.txt").exists()
    manifest = json.loads(Path(ref).read_text())
    assert manifest["version"] == 1
    assert ("setup" in manifest) == has_setup
    for p in Path(prepared["context"][0]["source"]).iterdir():
        p.unlink()
    task = pool.load(task_ref=ref)
    assert task.setup == (prepared["setup"] if has_setup else None)
    for _ in range(2):
        instance = runtime.start(ref=task.task_runtime_ref, context=task.context, setup=task.setup)
        assert (Path(instance.data["path"]) / "ready.txt").exists() == has_setup
        runtime.close(instance)
    runtime.close(original)
    manifest["setup"] = {"command": "true", "timeout": 0}
    Path(ref).write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="setup.timeout"):
        pool.load(task_ref=ref)


def test_invalid_setup_not_saved(tmp_path, runtime, prepared):
    pool = TaskPool(root=tmp_path / "pool", runtime=runtime)
    with pytest.raises(ValueError, match="setup"):
        pool.save(
            cwd="/", instance=None,
            name="invalid",
            goal=[{"type": "text", "text": "task"}],
            verifier=prepared["verifier"],
            verifier_runtime_ref=prepared["ref"],
            setup={},
        )
    assert not pool.root.exists()
