import gc
import json
import shutil
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import pytest

from envweaver import RuntimeRef, TaskPool, Verifier, _ssh_transport
from envweaver._task_store import operate, publish, validate_name
from envweaver.errors import RuntimeError, TaskExistsError


def address(path, host="storage"):
    return f"ssh://{host}" + quote(str(path), safe="/")


@pytest.fixture
def ssh_worker(monkeypatch):
    calls = []

    def ssh(host, command, payload, **kwargs):
        calls.append((host, payload["action"], kwargs))
        result = subprocess.run(
            [sys.executable, *command[1:]], input=json.dumps(payload).encode(), capture_output=True
        )
        if result.returncode:
            raise RuntimeError(result.stderr.decode())
        return result.stdout

    monkeypatch.setattr(_ssh_transport, "ssh", ssh)
    return calls


@pytest.fixture
def remote_task(tmp_path, runtime, ssh_worker):
    base = tmp_path / "base"
    base.mkdir()
    (base / "state").write_text("initial")
    ref = runtime.create(backend="test", image=str(base), connection={"profile": "task-worker"})
    instance = runtime.start(ref=ref)
    materials = tmp_path / "materials"
    materials.mkdir()
    entry = materials / "verify/run.py"
    entry.parent.mkdir()
    entry.write_text("print('{\"score\": 1}')\n")
    entry.chmod(0o750)
    image = materials / "goal.png"
    image.write_bytes(b"image")
    inputs = materials / "inputs"
    inputs.mkdir()
    (inputs / "binary").write_bytes(bytes(range(256)))
    (inputs / "empty").mkdir()
    pool = TaskPool(root=address(tmp_path / "remote '中文 #"), runtime=runtime)
    args = dict(
        instance=instance,
        name="任务 one #100%",
        goal=[{"type": "text", "text": "整理 DATA"}, {"type": "image", "path": str(image)}],
        verifier=Verifier(entry=entry, runtime=runtime, timeout=60),
        verifier_runtime_ref=RuntimeRef("test", ref.image, connection={"profile": "evaluator"}),
        context=[{"source": str(inputs), "target": "/data"}],
        metadata={"software": "Office", "rank": 1},
        setup={"command": "echo prepared", "timeout": 10},
    )
    yield pool, args, materials
    runtime.close(instance=instance)


def test_remote_task_roundtrip_and_material_lifetime(remote_task, runtime, ssh_worker):
    pool, args, materials = remote_task
    saved = pool.save(**args)
    assert saved == pool.root + "/tasks/" + quote(args["name"], safe="") + "/task.json"
    assert pool.search(filters={"software": "Office", "rank": 1}) == [saved]
    assert pool.search(filters={"missing": None}) == []
    assert pool.search(filters={"rank": 2}) == []
    assert pool.search(filters={"software": "absent"}) == []
    assert [action for _, action, _ in ssh_worker][-4:] == ["list"] * 4
    shutil.rmtree(materials)
    task = pool.load(task_ref=saved)
    assert task.task_ref == saved
    assert task.setup == args["setup"]
    assert task.task_runtime_ref.connection == {"profile": "task-worker"}
    assert task.verifier_runtime_ref.connection == {"profile": "evaluator"}
    assert Path(task.goal[1]["path"]).read_bytes() == b"image"
    assert Path(task.context[0]["source"]).joinpath("empty").is_dir()
    assert Path(task.context[0]["source"]).joinpath("binary").read_bytes() == bytes(range(256))
    entry = Path(task.verifier.entry)
    assert entry.stat().st_mode & 0o777 == 0o750
    assert task.verifier.runtime is runtime
    restored = runtime.start(ref=task.task_runtime_ref, context=task.context)
    try:
        assert Path(restored.data["path"]).joinpath("data/binary").read_bytes() == bytes(range(256))
    finally:
        runtime.close(instance=restored)
    # Keeping the verifier remains useful even after its Task and Pool are released.
    checker = task.verifier
    del task, pool
    gc.collect()
    assert entry.exists()
    result = subprocess.run([sys.executable, str(entry)], capture_output=True, check=True)
    assert json.loads(result.stdout) == {"score": 1}
    del checker
    gc.collect()
    assert not entry.parent.parent.exists()


def test_duplicate_reserved_before_snapshot(remote_task, runtime, monkeypatch):
    pool, args, _ = remote_task
    saved = pool.save(**args)
    monkeypatch.setattr(runtime, "save", lambda *a, **k: pytest.fail("duplicate snapshot"))
    with pytest.raises(TaskExistsError):
        pool.save(**args)
    assert pool.search() == [saved]


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("first", ["directory", "zip"])
@pytest.mark.parametrize("second", ["directory", "zip"])
def test_duplicate_names_across_formats(remote_task, runtime, monkeypatch, remote, first, second):
    pool, args, _ = remote_task
    if not remote:
        pool = TaskPool(root=unquote(urlsplit(pool.root).path), runtime=runtime)
    saved = pool.save(**args, format=first)
    monkeypatch.setattr(runtime, "save", lambda *a, **k: pytest.fail("must not save snapshot"))
    with pytest.raises(TaskExistsError):
        pool.save(**args, format=second)
    assert pool.search() == [saved]
    assert pool.load(saved).name == args["name"]


@pytest.mark.parametrize("remote", [False, True])
def test_search_mixed_formats(remote_task, runtime, monkeypatch, remote):
    pool, args, _ = remote_task
    if not remote:
        pool = TaskPool(root=unquote(urlsplit(pool.root).path), runtime=runtime)
    directory = pool.save(**args)
    zipped = pool.save(**{**args, "name": "zip-task", "metadata": {"kind": "zip"}}, format="zip")
    monkeypatch.setattr(pool, "_load_archive", lambda *a: pytest.fail("search must not extract"))
    assert set(pool.search()) == {directory, zipped}
    assert set(pool.search(filters={})) == {directory, zipped}
    assert pool.search(filters={"kind": "zip"}) == [zipped]
    assert pool.search(filters={"software": "Office"}) == [directory]
    assert pool.search(filters={"kind": "missing"}) == []


@pytest.mark.parametrize("remote", [False, True])
def test_concurrent_formats_share_one_name(remote_task, remote):
    pool, args, _ = remote_task
    if not remote:
        pool = TaskPool(root=unquote(urlsplit(pool.root).path), runtime=pool.runtime)

    def save(format):
        try:
            return pool.save(**args, format=format)
        except TaskExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as threads:
        results = list(threads.map(save, ["directory", "zip"]))
    assert len([result for result in results if result]) == 1
    assert pool.search() == [result for result in results if result]


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("format", ["directory", "zip"])
def test_concurrent_saves_one_winner(remote_task, remote, format):
    pool, args, _ = remote_task
    if not remote:
        pool = TaskPool(root=unquote(urlsplit(pool.root).path), runtime=pool.runtime)

    def save():
        try:
            return pool.save(**args, format=format)
        except TaskExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as threads:
        results = list(threads.map(lambda _: save(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert pool.search() == [result for result in results if result]
    root = Path(unquote(urlsplit(pool.root).path)) if remote else pool.root
    assert (root / "archives" / f"{args['name']}.zip").exists() == (format == "zip")


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("format", ["directory", "zip"])
def test_task_zip_roundtrip(remote_task, runtime, tmp_path, remote, format):
    pool, args, materials = remote_task
    root = Path(unquote(urlsplit(pool.root).path))
    if not remote:
        pool = TaskPool(root=root, runtime=runtime)
    saved = pool.save(**args, format=format)
    zipped = root / "archives" / f"{args['name']}.zip"
    assert zipped.exists() == (format == "zip")
    assert pool.search() == [saved]
    if format == "directory":
        assert not zipped.parent.exists()
        return
    assert not (root / "tasks" / args["name"]).exists()
    assert saved == (address(zipped) if remote else str(zipped))
    with zipfile.ZipFile(zipped) as package:
        assert json.loads(package.read("task.json"))["name"] == args["name"]
        assert "verifier/run.py" in package.namelist()
        assert "context/0/inputs/empty/" in package.namelist()
    shutil.rmtree(materials)
    # A delivered ZIP can be read independently of the original Pool.
    reader = TaskPool(root=tmp_path / "other-pool", runtime=runtime)
    reference = address(zipped) if remote else str(zipped)
    task = reader.load(reference)
    assert task.task_ref == reference
    assert task.setup == args["setup"]
    assert task.goal[0] == args["goal"][0]
    assert Path(task.goal[1]["path"]).read_bytes() == b"image"
    assert Path(task.context[0]["source"]).joinpath("binary").read_bytes() == bytes(range(256))
    entry = Path(task.verifier.entry)
    assert entry.stat().st_mode & 0o777 == 0o750
    checker = task.verifier
    del task
    gc.collect()
    assert json.loads(subprocess.check_output([sys.executable, checker.entry])) == {"score": 1}
    del checker
    gc.collect()
    assert not entry.exists()


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("kind", ["corrupt", "escape", "missing", "invalid-manifest"])
def test_invalid_zip(tmp_path, runtime, ssh_worker, remote, kind):
    path = tmp_path / "task.zip"
    if kind == "corrupt":
        path.write_bytes(b"not zip")
    else:
        with zipfile.ZipFile(path, "w") as package:
            if kind == "escape":
                package.writestr("../escaped", "bad")
            elif kind == "invalid-manifest":
                package.writestr("task.json", "invalid JSON")
    pool = TaskPool(root=tmp_path / "pool", runtime=runtime)
    with pytest.raises((zipfile.BadZipFile, ValueError, FileNotFoundError)):
        pool.load(address(path) if remote else path)
    assert not (tmp_path / "escaped").exists()


def test_zip_publish_failure_leaves_no_task(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "task.json").write_text("{}")
    (tmp_path / "tasks").mkdir()
    rename = Path.rename

    def fail(path, target):
        if str(target).endswith(".zip"):
            raise OSError("cannot publish ZIP")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail)
    with pytest.raises(OSError, match="cannot publish"):
        publish(stage, tmp_path, "task", format="zip")
    assert not list((tmp_path / "archives").iterdir())
    assert not list((tmp_path / "tasks").iterdir())


@pytest.mark.parametrize("remote", [False, True])
def test_zip_write_failure_does_not_publish_task(remote_task, remote):
    pool, args, _ = remote_task
    root = Path(unquote(urlsplit(pool.root).path))
    if not remote:
        pool = TaskPool(root=root, runtime=pool.runtime)
    root.mkdir(parents=True)
    (root / "archives").write_text("blocked")
    with pytest.raises(OSError):
        pool.save(**args, format="zip")
    assert pool.search() == []
    assert not list((root / "tasks").iterdir())
    (root / "archives").unlink()
    assert pool.save(**args, format="zip")


@pytest.mark.parametrize("format", ["tar", "both", "", None, True, []])
def test_invalid_format_fails_before_work(tmp_path, runtime, format):
    pool = TaskPool(root=tmp_path / "pool", runtime=runtime)
    with pytest.raises(ValueError, match="format"):
        pool.save(
            instance=None,
            name="task",
            goal=[],
            verifier=None,
            verifier_runtime_ref=None,
            format=format,
        )
    assert not pool.root.exists()


@pytest.mark.parametrize("failure", ["snapshot", "publish"])
def test_failed_save_releases_name_without_publishing(remote_task, runtime, monkeypatch, failure):
    pool, args, _ = remote_task
    original = pool._remote
    if failure == "snapshot":
        runtime._backends["test"].fail_save = True
    else:

        def dispatch(action, **kwargs):
            if action == "publish":
                # Exercise failure inside the remote worker, before atomic rename.
                kwargs["body"] = "invalid archive"
            return original(action, **kwargs)

        monkeypatch.setattr(pool, "_remote", dispatch)
    with pytest.raises((OSError, ValueError, RuntimeError)):
        pool.save(**args)
    root = Path(unquote(urlsplit(pool.root).path))
    assert list((root / "tasks").iterdir()) == []
    assert pool.search() == []
    runtime._backends["test"].fail_save = False
    monkeypatch.setattr(pool, "_remote", original)
    assert pool.save(**args)


def test_wrong_pool_missing_and_escaping_references(remote_task, tmp_path):
    pool, args, _ = remote_task
    saved = pool.save(**args)
    for wrong in [
        saved.replace("ssh://storage", "ssh://elsewhere"),
        address(tmp_path / "other"),
        tmp_path,
    ]:
        with pytest.raises(ValueError, match="different Pool"):
            pool.load(wrong)
    with pytest.raises(OSError):
        pool.load(pool.root + "/tasks/missing/task.json")
    directory = Path(unquote(urlsplit(saved).path)).parent
    outside = tmp_path / "outside"
    shutil.copytree(directory, outside)
    (directory.parent / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="different Pool"):
        pool.load(pool.root + "/tasks/escape/task.json")
    assert pool.search() == [saved]
    data = json.loads((directory / "task.json").read_text())
    data["verifier"]["entry"] = str(outside / "verifier/run.py")
    (directory / "task.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="escapes"):
        pool.load(saved)


def test_empty_pool_and_ssh_options(tmp_path, runtime, ssh_worker):
    runtime.ssh = {"config_file": str(tmp_path / "ssh-config")}
    pool = TaskPool(root=address(tmp_path / "not-created"), runtime=runtime)
    assert pool.search() == []
    assert ssh_worker == [("ssh://storage", "list", {"ssh_options": runtime.ssh})]
    with pytest.raises(ValueError, match="Unsupported"):
        TaskPool(root="s3://bucket/tasks", runtime=runtime)


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize(
    "name",
    [
        None,
        "",
        " ",
        ".",
        "..",
        ".hidden",
        "a/b",
        "a\\b",
        "../escape",
        "a:b",
        "a*",
        "a?",
        'a"',
        "a<",
        "a>",
        "a|",
        "a\x00",
        "a\n",
        "a\x7f",
        " leading",
        "trailing ",
        "trailing.",
        "CON",
        "nul.txt",
        "COM1",
        "LPT9.log",
        "x" * 250,
        "中" * 84,
    ],
)
def test_invalid_name_fails_before_any_work(tmp_path, runtime, monkeypatch, remote, name):
    root = tmp_path / "pool"
    pool = TaskPool(root=address(root) if remote else root, runtime=runtime)
    monkeypatch.setattr(pool, "_remote", lambda *a, **k: pytest.fail("must not access SSH"))
    monkeypatch.setattr(runtime, "save", lambda *a, **k: pytest.fail("must not save sandbox"))
    with pytest.raises(ValueError, match="Task name"):
        pool.save(instance=None, name=name, goal=[], verifier=None, verifier_runtime_ref=None)
    assert not root.exists()


def test_remote_worker_rejects_path_as_name(tmp_path):
    for action in ("reserve", "publish", "release"):
        with pytest.raises(ValueError, match="Task name"):
            operate({"root": str(tmp_path), "action": action, "name": "../escape"})
    assert not (tmp_path / "tasks").exists()


@pytest.mark.parametrize("name", ["任务 one #100%", "a.b", "x" * 249, "中" * 83])
def test_valid_directory_names(name):
    validate_name(name)


def test_symlink_pool_path_roundtrip(remote_task, tmp_path, runtime):
    pool, args, _ = remote_task
    saved = pool.save(**args)
    root = Path(unquote(urlsplit(pool.root).path))
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    other = TaskPool(root=address(alias), runtime=runtime)
    matches = other.search()
    assert matches == [address(alias / "tasks" / args["name"] / "task.json")]
    assert other.load(matches[0]).name == pool.load(saved).name
