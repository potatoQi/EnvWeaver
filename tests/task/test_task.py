import json
import shutil
from pathlib import Path

import pytest

from envweaver import Runtime, RuntimeRef, TaskPool, Verifier
from envweaver.errors import TaskExistsError


def verification(entry, runtime):
    return Verifier(entry=entry, timeout=30, runtime=runtime)


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
        verification(package / "run.py", runtime),
        RuntimeRef("test", "verifier-base"),
        [{"source": str(inputs), "target": "/work"}],
        {"software": "Office", "rank": 2},
        cwd="/",
    )
    assert Path(ref) == pool.root / "unique/task.json"
    with pytest.raises(TaskExistsError):
        pool.save(
            instance,
            "unique",
            goal,
            verification(package / "run.py", runtime),
            RuntimeRef("test", "verifier-base"),
            cwd="/",
        )
    assert runtime.close(instance) is None
    shutil.rmtree(original)
    shutil.rmtree(package)
    shutil.rmtree(inputs)
    image.unlink()
    task = pool.load(ref)
    assert task.task_runtime_ref.connection == {}
    assert task.verifier_runtime_ref.connection == {}
    assert Path(task.goal[1]["path"]).read_bytes() == b"reference"
    assert Path(task.verifier.entry).with_name("private.txt").read_text() == "answer"
    restored = runtime.start(task.task_runtime_ref, task.context)
    root = Path(restored.data["path"])
    assert (root / "state.txt").read_text() == "broken"
    assert (root / "work" / "sales.csv").read_text() == "a,b\n1,2"
    assert not (root / "private.txt").exists()
    (root / "work" / "sales.csv").write_text("changed")
    other = runtime.start(task.task_runtime_ref, task.context)
    assert (Path(other.data["path"]) / "work/sales.csv").read_text() == "a,b\n1,2"
    assert pool.search(filters={"software": "Office", "rank": 2}) == [ref]
    assert pool.search(filters={"missing": None}) == []
    assert pool.search(filters={"software": "absent"}) == []
    assert pool.search(filters={"software": "Office", "rank": 3}) == []
    assert pool.search(filters={}) == [ref]


def test_failed_snapshot_never_publishes_task(tmp_path, runtime):
    original, package, *_ = materials(tmp_path)
    instance = runtime.start(RuntimeRef("test", str(original)))
    pool = TaskPool(tmp_path / "pool", runtime=runtime)
    runtime._backends["test"].fail_save = True
    with pytest.raises(OSError, match="snapshot"):
        pool.save(
            instance,
            "failed",
            [{"type": "text", "text": "task"}],
            verification(package / "run.py", runtime),
            RuntimeRef("test", "verifier-base"),
            cwd="/",
        )
    assert pool.search() == []
    runtime._backends["test"].fail_save = False
    assert pool.save(
        instance,
        "failed",
        [{"type": "text", "text": "task"}],
        verification(package / "run.py", runtime),
        RuntimeRef("test", "verifier-base"),
        cwd="/",
    )


def test_pool_requires_runtime(tmp_path):
    with pytest.raises(TypeError, match="runtime"):
        TaskPool(root=tmp_path / "pool")


def test_load_rejects_escaping_assets(tmp_path, runtime):
    original, package, *_ = materials(tmp_path)
    instance = runtime.start(RuntimeRef("test", str(original)))
    pool = TaskPool(tmp_path / "pool", runtime=runtime)
    ref = pool.save(
        instance,
        "bad",
        [{"type": "text", "text": "task"}],
        verification(package / "run.py", runtime),
        RuntimeRef("test", "verifier-base"),
        cwd="/",
    )
    data = json.loads(Path(ref).read_text())
    data["verifier"]["entry"] = str(package / "run.py")
    Path(ref).write_text(json.dumps(data))
    with pytest.raises(ValueError, match="escapes"):
        pool.load(ref)


def test_verifier_cannot_recursively_archive_the_pool(tmp_path, runtime):
    original, package, *_ = materials(tmp_path)
    instance = runtime.start(RuntimeRef("test", str(original)))
    pool = TaskPool(package / "pool", runtime=runtime)
    with pytest.raises(ValueError, match="separate verifier package"):
        pool.save(
            instance,
            "recursive",
            [{"type": "text", "text": "task"}],
            verification(package / "run.py", runtime),
            RuntimeRef("test", "verifier-base"),
            cwd="/",
        )
    assert pool.search() == []
    assert not list(pool.root.glob("*.lock"))


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


@pytest.mark.parametrize(
    "backend,operational",
    [
        ("docker", {"startup_timeout": 90}),
        (
            "qemu",
            {
                "disk_dir": "/producer/disks",
                "save_mode": "consolidated",
                "remote_python": "/producer/venv/python",
                "binary": "/producer/qemu",
                "image_binary": "/producer/qemu-img",
                "startup_timeout": 90,
                "shutdown_timeout": 60,
            },
        ),
        ("custom", {}),
    ],
)
def test_task_keeps_runtime_requirements_without_producer_settings(
    tmp_path, runtime, monkeypatch, backend, operational
):
    import copy

    original, package, *_ = materials(tmp_path)
    instance = runtime.start(RuntimeRef("test", str(original)))
    if backend == "docker":
        required = {
            "python": "/opt/python/bin/python3",
            "command": ["sleep", "infinity"],
            "env": {"LANG": "C.UTF-8"},
            "user": "1000:1000",
            "memory": "4g",
            "cpus": 2,
            "network": "bridge",
            "shm_size": "1g",
            "gpus": "all",
            "args": ["--hostname", "desktop"],
        }
    elif backend == "qemu":
        required = {
            "memory": 8192,
            "cpus": 4,
            "disk_interface": "virtio",
            "network_device": "virtio-net-pci",
            "format": "qcow2",
            "machine": "q35",
            "accel": "kvm",
            "firmware": "/assets/code.fd",
            "nvram": "/assets/vars.fd",
            "args": ["-vga", "std"],
        }
    else:
        # These names may have different meanings for custom backends.
        required = {
            "disk_dir": "/custom",
            "python": "custom",
            "binary": "custom",
            "save_mode": "custom",
        }
    saved = RuntimeRef(backend, "/asset", {**required, **operational})
    original_config = copy.deepcopy(saved.config)
    monkeypatch.setattr(runtime, "save", lambda *a, **k: saved)
    pool = TaskPool(tmp_path / "pool", runtime=runtime)
    path = pool.save(
        instance,
        "task",
        [{"type": "text", "text": "goal"}],
        verification(package / "run.py", runtime),
        RuntimeRef("test", "verifier-base"),
        cwd="/",
    )
    manifest = json.loads(Path(path).read_text())
    assert manifest["task_runtime_ref"] == {
        "backend": backend,
        "image": "/asset",
        "name": "/asset",
        "config": required,
        "connection": {},
        "metadata": {},
    }
    assert pool.load(path).task_runtime_ref.config == required
    assert saved.config == original_config
    assert "instance.json" not in {p.name for p in Path(path).parent.rglob("*")}


@pytest.mark.parametrize("requires_task_instance", [False, True])
def test_verifier_contract_roundtrip_and_environment_filtering(
    tmp_path, runtime, requires_task_instance
):
    original, package, *_ = materials(tmp_path)
    instance = runtime.start(RuntimeRef("test", str(original)))
    pool = TaskPool(tmp_path / "pool", runtime=runtime)
    verifier_runtime_ref = RuntimeRef(
        "qemu",
        "/assets/windows.qcow2",
        {
            "memory": 4096,
            "cpus": 2,
            "disk_dir": "/producer/disks",
            "remote_python": "/venv/python",
            "save_mode": "consolidated",
        },
    )
    checker = Verifier(
        entry=package / "run.py",
        timeout=42,
        required_env=["LLM_API_KEY"],
        requires_task_instance=requires_task_instance,
        runtime=runtime,
    )
    ref = pool.save(
        instance,
        "windows-checker",
        [{"type": "text", "text": "task"}],
        checker,
        verifier_runtime_ref,
        cwd="/",
    )
    manifest = json.loads(Path(ref).read_text())
    assert manifest["version"] == 1
    assert "runtime" not in manifest
    assert manifest["verifier_runtime_ref"] == {
        "backend": "qemu",
        "image": "/assets/windows.qcow2",
        "name": "/assets/windows.qcow2",
        "config": {"memory": 4096, "cpus": 2},
        "connection": {},
        "metadata": {},
    }
    assert manifest["verifier"] == {
        "entry": "verifier/run.py",
        "timeout": 42,
        "required_env": ["LLM_API_KEY"],
        "requires_task_instance": requires_task_instance,
    }
    assert verifier_runtime_ref.config["disk_dir"] == "/producer/disks"
    shutil.rmtree(package)
    loaded = pool.load(ref)
    assert isinstance(loaded.verifier, Verifier)
    assert loaded.verifier.runtime is runtime
    consumer_runtime = Runtime(host="ssh://consumer")
    consumer_pool = TaskPool(root=pool.root, runtime=consumer_runtime)
    assert consumer_pool.load(ref).verifier.runtime is consumer_runtime
    assert loaded.verifier.timeout == 42
    assert loaded.verifier.required_env == ["LLM_API_KEY"]
    assert loaded.verifier.requires_task_instance is requires_task_instance
    assert loaded.verifier_runtime_ref == RuntimeRef(
        "qemu", "/assets/windows.qcow2", {"memory": 4096, "cpus": 2}
    )
    assert Path(loaded.verifier.entry).is_file()
    # Optional fields can be omitted by an independent producer.
    manifest["verifier"].pop("required_env")
    manifest["verifier"].pop("requires_task_instance")
    manifest.pop("context")
    manifest.pop("metadata")
    Path(ref).write_text(json.dumps(manifest))
    assert pool.load(ref).verifier.required_env == []
    assert pool.load(ref).verifier.requires_task_instance is True
    assert pool.load(ref).context == []
    assert pool.load(ref).metadata == {}
    assert pool.search() == [ref]
    manifest["verifier"]["requires_task_instance"] = "true"
    Path(ref).write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="requires_task_instance"):
        pool.load(ref)
    manifest["verifier"].pop("requires_task_instance")
    # Loaded declarations are validated, not just producer-side Python objects.
    manifest["verifier"]["timeout"] = 0
    Path(ref).write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="timeout"):
        pool.load(ref)


@pytest.mark.parametrize("timeout", [True, False, 0, -1, float("nan"), float("inf"), "300", None])
def test_verifier_rejects_invalid_deadlines(timeout, runtime):
    with pytest.raises(ValueError, match="timeout"):
        Verifier(entry="run.py", timeout=timeout, runtime=runtime)


@pytest.mark.parametrize(
    "required", ["API_KEY", ["KEY=value"], [""], ["KEY", "KEY"], [None], ["1KEY"]]
)
def test_verifier_requires_names_not_values(required, runtime):
    with pytest.raises(ValueError, match="required_env"):
        Verifier(
            entry="run.py",
            timeout=300,
            required_env=required,
            runtime=runtime,
        )


@pytest.mark.parametrize("version", [2, True, 1.0, "1", None, 99])
def test_load_rejects_unsupported_or_invalid_task_versions(tmp_path, runtime, version):
    original, package, *_ = materials(tmp_path)
    instance = runtime.start(RuntimeRef("test", str(original)))
    pool = TaskPool(tmp_path / "pool", runtime=runtime)
    ref = pool.save(
        instance,
        "version",
        [{"type": "text", "text": "task"}],
        verification(package / "run.py", runtime),
        RuntimeRef("test", "verifier-base"),
        cwd="/",
    )
    manifest = json.loads(Path(ref).read_text())
    manifest["version"] = version
    Path(ref).write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="version"):
        pool.load(ref)


def test_invalid_verifier_does_not_create_snapshot(tmp_path, runtime, monkeypatch):
    original, package, *_ = materials(tmp_path)
    instance = runtime.start(RuntimeRef("test", str(original)))
    pool = TaskPool(tmp_path / "pool", runtime=runtime)
    monkeypatch.setattr(runtime, "save", lambda *a, **kw: pytest.fail("must not save snapshot"))
    with pytest.raises(TypeError, match="Verifier"):
        pool.save(
            instance,
            "legacy",
            [{"type": "text", "text": "task"}],
            package / "run.py",
            RuntimeRef("test", "verifier-base"),
            cwd="/",
        )
    with pytest.raises(ValueError, match="run.py"):
        Verifier(entry="check.sh", timeout=30, runtime=runtime)
    checker = Verifier(
        entry=package / "run.py",
        timeout=30,
        runtime=runtime,
    )
    with pytest.raises(TypeError, match="RuntimeRef"):
        pool.save(
            instance, "invalid", [{"type": "text", "text": "task"}], checker, {"backend": "docker"},
            cwd="/",
        )
    with pytest.raises(TypeError):
        pool.save(
            instance,
            "invalid",
            [{"type": "text", "text": "task"}],
            checker,
            RuntimeRef("docker", "checker", {"bad": object()}),
            cwd="/",
        )
    assert pool.search() == []


@pytest.mark.parametrize("backend_name", ["qemu", "docker", "company"])
def test_task_connections_roundtrip_and_start(tmp_path, runtime, backend_name):
    from dataclasses import replace

    from tests.conftest import FilesystemBackend

    class Backend(FilesystemBackend):
        def start(self, ref, connection=None):
            instance = replace(super().start(ref, connection=connection), backend=backend_name)
            return instance

        def save(self, instance):
            return replace(super().save(instance), backend=backend_name)

    runtime.register(backend_name, Backend)
    original, package, *_ = materials(tmp_path)
    connections = {
        "qemu": (
            {"ssh": {"user": "worker", "python": "python"}},
            {"ssh": {"user": "evaluator", "python": "/opt/venv/bin/python"}},
        ),
        "docker": ({}, {"vnc": {"password_env": "VERIFY_VNC_PASSWORD"}}),
        "company": ({"profile": "task-worker"}, {"profile": "evaluator"}),
    }
    task_connection, verifier_connection = connections[backend_name]
    ref = RuntimeRef(backend_name, str(original), connection=task_connection)
    instance = runtime.start(ref)
    pool = TaskPool(tmp_path / "pool", runtime=runtime)
    path = pool.save(
        cwd="/", instance=instance,
        name="connections",
        goal=[{"type": "text", "text": "task"}],
        verifier=verification(package / "run.py", runtime),
        verifier_runtime_ref=replace(ref, connection=verifier_connection),
    )
    manifest = json.loads(Path(path).read_text())
    assert manifest["version"] == 1
    assert "task_connection" not in manifest
    assert "verifier_connection" not in manifest
    assert manifest["task_runtime_ref"]["connection"] == task_connection
    assert manifest["verifier_runtime_ref"]["connection"] == verifier_connection
    task = pool.load(path)
    assert task.task_runtime_ref.connection == task_connection
    assert task.verifier_runtime_ref.connection == verifier_connection
    # Inputs and loaded values do not alias the saved package.
    task_connection.clear()
    verifier_connection.clear()
    task_instance = runtime.start(ref=task.task_runtime_ref)
    verify_instance = runtime.start(ref=task.verifier_runtime_ref)
    assert task_instance.data["connection"] == manifest["task_runtime_ref"]["connection"]
    assert verify_instance.data["connection"] == manifest["verifier_runtime_ref"]["connection"]
    for live in (instance, task_instance, verify_instance):
        runtime.close(live)


@pytest.mark.parametrize(
    "backend,connection,error",
    [
        ("qemu", {"ssh": {"user": "worker", "identity_file": "/producer/private-key"}}, ValueError),
        ("qemu", {"ssh": {"host": "producer", "port": 2222}}, ValueError),
        ("qemu", {"ssh": {"password": "secret"}}, ValueError),
        ("qemu", {"ssh": {"python": ""}}, ValueError),
        ("qemu", {"ssh": "worker"}, ValueError),
        ("docker", {"vnc": {"host": "producer", "port": 5900}}, ValueError),
        ("docker", {"vnc": {"password": "secret"}}, ValueError),
        ("qemu", [], TypeError),
        ("company", {"profile": float("nan")}, ValueError),
    ],
)
def test_invalid_task_connections_fail_before_save(
    tmp_path, runtime, monkeypatch, backend, connection, error
):
    from envweaver import Instance

    _, package, *_ = materials(tmp_path)
    pool = TaskPool(tmp_path / "pool", runtime=runtime)
    monkeypatch.setattr(runtime, "save", lambda *_: pytest.fail("must not save environment"))
    with pytest.raises(error):
        pool.save(
            cwd="/", instance=Instance("live", backend),
            name="invalid",
            goal=[{"type": "text", "text": "task"}],
            verifier=verification(package / "run.py", runtime),
            verifier_runtime_ref=RuntimeRef(backend, "verifier", connection=connection),
        )
    assert not (pool.root / "invalid").exists()


def test_load_rejects_deployment_fields_in_connection(tmp_path, runtime):
    original, package, *_ = materials(tmp_path)
    instance = runtime.start(RuntimeRef("test", str(original)))
    pool = TaskPool(tmp_path / "pool", runtime=runtime)
    path = Path(
        pool.save(
            instance,
            "tampered",
            [{"type": "text", "text": "task"}],
            verification(package / "run.py", runtime),
            RuntimeRef("qemu", "disk"),
            cwd="/",
        )
    )
    manifest = json.loads(path.read_text())
    manifest["verifier_runtime_ref"]["connection"] = {"ssh": {"identity_file": "/producer/key"}}
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="connection"):
        pool.load(path)
    runtime.close(instance)


@pytest.mark.parametrize("filters", ["keyword", [], False, 1])
def test_search_rejects_non_dictionary_filters(tmp_path, runtime, monkeypatch, filters):
    pool = TaskPool("ssh://storage/tasks", runtime=runtime)
    monkeypatch.setattr(pool, "_remote", lambda *a: pytest.fail("must validate before reading"))
    with pytest.raises(TypeError, match="filters must be a dictionary"):
        pool.search(filters=filters)


@pytest.mark.parametrize("cwd", ["/workspace/project", r"C:\Tasks", r"\\server\share\project"])
def test_cwd_round_trip(tmp_path, runtime, cwd):
    original, package, _, _ = materials(tmp_path)
    instance = runtime.start(ref=RuntimeRef("test", str(original)))
    pool = TaskPool(root=tmp_path / "pool", runtime=runtime)
    try:
        ref = pool.save(
            instance=instance, name="cwd", goal=[{"type": "text", "text": "solve"}],
            verifier=verification(package / "run.py", runtime),
            verifier_runtime_ref=RuntimeRef("test", "verifier"), cwd=cwd,
        )
        assert pool.load(task_ref=ref).cwd == cwd
        data = json.loads(Path(ref).read_text())
        assert data["version"] == 1
        assert data["cwd"] == cwd
    finally:
        runtime.close(instance=instance)


@pytest.mark.parametrize("cwd", [None, "", "relative/path", r"C:relative", 12, [], " /work", "/work\0"])
def test_invalid_cwd_rejected_on_save_load_and_task(tmp_path, runtime, cwd):
    from dataclasses import replace

    original, package, _, _ = materials(tmp_path)
    instance = runtime.start(ref=RuntimeRef("test", str(original)))
    pool = TaskPool(root=tmp_path / "pool", runtime=runtime)
    kwargs = dict(
        instance=instance, goal=[{"type": "text", "text": "solve"}],
        verifier=verification(package / "run.py", runtime),
        verifier_runtime_ref=RuntimeRef("test", "verifier"),
    )
    try:
        with pytest.raises(ValueError, match="cwd"):
            pool.save(name="invalid", cwd=cwd, **kwargs)
        assert pool.search() == []
        ref = pool.save(cwd="/", name="valid", **kwargs)
        task = pool.load(task_ref=ref)
        with pytest.raises(ValueError, match="cwd"):
            replace(task, cwd=cwd)
        data = json.loads(Path(ref).read_text())
        data["cwd"] = cwd
        Path(ref).write_text(json.dumps(data))
        with pytest.raises(ValueError, match="cwd"):
            pool.load(task_ref=ref)
    finally:
        runtime.close(instance=instance)


def test_cwd_is_required_without_implicit_default(tmp_path, runtime):
    from dataclasses import fields

    from envweaver import Task

    original, package, _, _ = materials(tmp_path)
    instance = runtime.start(ref=RuntimeRef("test", str(original)))
    pool = TaskPool(root=tmp_path / "pool", runtime=runtime)
    kwargs = dict(
        instance=instance, name="required-cwd", goal=[{"type": "text", "text": "solve"}],
        verifier=verification(package / "run.py", runtime),
        verifier_runtime_ref=RuntimeRef("test", "verifier"),
    )
    try:
        with pytest.raises(TypeError, match="cwd"):
            pool.save(**kwargs)
        assert pool.search() == []
        ref = pool.save(cwd="/workspace", **kwargs)
        task = pool.load(task_ref=ref)
        without_cwd = {field.name: getattr(task, field.name) for field in fields(task) if field.name != "cwd"}
        with pytest.raises(TypeError, match="cwd"):
            Task(**without_cwd)
        data = json.loads(Path(ref).read_text())
        del data["cwd"]
        Path(ref).write_text(json.dumps(data))
        with pytest.raises(ValueError, match="cwd"):
            pool.load(task_ref=ref)
    finally:
        runtime.close(instance=instance)
