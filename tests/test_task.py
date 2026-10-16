import json
import shutil
from pathlib import Path

import pytest

from envweaver import RuntimeRef, TaskPool
from envweaver.errors import TaskExistsError


def materials(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    (original / "state.txt").write_text("broken")
    package = tmp_path / "verifier"
    package.mkdir()
    (package / "run.py").write_text("print('{\"score\": 1}')")
    (package / "private.txt").write_text("answer")
    image = tmp_path / "image.png"
    image.write_bytes(b"reference")
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "sales.csv").write_text("a,b\n1,2")
    return original, package, image, inputs


def test_archive_restore_search_and_unique_names(tmp_path, runtime):
    original, package, image, inputs = materials(tmp_path)
    instance = runtime.start(RuntimeRef("test", str(original)))
    pool = TaskPool(tmp_path / "pool", runtime=runtime)
    goal = [{"type": "text", "text": "修复表格"}, {"type": "image", "path": str(image)}]
    ref = pool.save(
        instance,
        "unique",
        goal,
        package / "run.py",
        [{"source": str(inputs), "target": "/work"}],
        {"software": "Office", "rank": 2},
    )
    with pytest.raises(TaskExistsError):
        pool.save(instance, "unique", goal, package / "run.py")
    assert runtime.close(instance) == (True, None)
    shutil.rmtree(original)
    shutil.rmtree(package)
    shutil.rmtree(inputs)
    image.unlink()
    task = pool.load(ref)
    assert Path(task.goal[1]["path"]).read_bytes() == b"reference"
    assert Path(task.verifier).with_name("private.txt").read_text() == "answer"
    restored = runtime.start(task.runtime, task.context)
    root = Path(restored.data["path"])
    assert (root / "state.txt").read_text() == "broken"
    assert (root / "work" / "sales.csv").read_text() == "a,b\n1,2"
    assert not (root / "private.txt").exists()
    (root / "work" / "sales.csv").write_text("changed")
    other = runtime.start(task.runtime, task.context)
    assert (Path(other.data["path"]) / "work/sales.csv").read_text() == "a,b\n1,2"
    assert pool.search("表格", {"software": "Office"}) == [ref]
    assert pool.search(filters={"missing": None}) == []
    assert pool.search("absent") == []
    assert pool.search(filters={"rank": 3}) == []


def test_failed_snapshot_never_publishes_task(tmp_path, runtime):
    original, package, *_ = materials(tmp_path)
    instance = runtime.start(RuntimeRef("test", str(original)))
    pool = TaskPool(tmp_path / "pool", runtime=runtime)
    runtime._backends["test"].fail_save = True
    with pytest.raises(OSError, match="snapshot"):
        pool.save(instance, "failed", [{"type": "text", "text": "task"}], package / "run.py")
    assert pool.search() == []
    runtime._backends["test"].fail_save = False
    assert pool.save(instance, "failed", [{"type": "text", "text": "task"}], package / "run.py")


def test_load_rejects_escaping_assets(tmp_path, runtime):
    original, package, *_ = materials(tmp_path)
    instance = runtime.start(RuntimeRef("test", str(original)))
    pool = TaskPool(tmp_path / "pool", runtime=runtime)
    ref = pool.save(instance, "bad", [{"type": "text", "text": "task"}], package / "run.py")
    data = json.loads(Path(ref).read_text())
    data["verifier"] = str(package / "run.py")
    Path(ref).write_text(json.dumps(data))
    with pytest.raises(ValueError, match="escapes"):
        pool.load(ref)


def test_verifier_cannot_recursively_archive_the_pool(tmp_path, runtime):
    original, package, *_ = materials(tmp_path)
    instance = runtime.start(RuntimeRef("test", str(original)))
    pool = TaskPool(package / "pool", runtime=runtime)
    with pytest.raises(ValueError, match="separate verifier package"):
        pool.save(instance, "recursive", [{"type": "text", "text": "task"}], package / "run.py")
    assert pool.search() == []
    assert not list((pool.root / "tasks").glob("*.lock"))


def test_context_failure_keeps_original_error_when_cleanup_also_fails(
    tmp_path, runtime, monkeypatch
):
    original, *_ = materials(tmp_path)

    def fail_cleanup(_):
        raise OSError("cleanup unavailable")

    monkeypatch.setattr(runtime._backends["test"], "close", fail_cleanup)
    with pytest.raises(FileNotFoundError) as failure:
        runtime.start(
            RuntimeRef("test", str(original)),
            [
                {"source": str(tmp_path / "missing"), "target": "/input"},
            ],
        )
    assert "cleanup unavailable" in failure.value.__notes__[0]
