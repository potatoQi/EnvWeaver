import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote

import pytest

from envweaver import Instance, Runtime, TaskPool, Verifier
from envweaver._io import ssh_transport as _ssh_transport
from envweaver._io.files import unpack
from envweaver._io.storage import local_source, local_target
from envweaver.errors import RuntimeError


def address(path, host="files"):
    return f"ssh://user@{host}:2222" + quote(str(path), safe="/")


@pytest.fixture
def ssh_files(monkeypatch):
    calls = []

    def ssh(host, command, payload):
        calls.append((host, payload["action"]))
        result = subprocess.run(
            [sys.executable, *command[1:]], input=json.dumps(payload).encode(), capture_output=True
        )
        if result.returncode:
            raise RuntimeError(result.stderr.decode())
        return result.stdout

    monkeypatch.setattr(_ssh_transport, "ssh", ssh)
    return calls


def test_instance_record_ssh_roundtrip(tmp_path, runtime, ssh_files, monkeypatch):
    runtime.ssh = {"config_file": str(tmp_path / "ssh_config")}
    original_ssh = _ssh_transport.ssh

    def ssh(host, command, payload, **kwargs):
        assert kwargs == {}
        return original_ssh(host, command, payload)

    monkeypatch.setattr(_ssh_transport, "ssh", ssh)
    original = Instance("one", "test", data={"path": "/work"}, host="ssh://sandbox")
    path = tmp_path / "records '中文 #/instance.json"
    runtime.describe(instance=original, path=address(path, "records"))
    assert path.stat().st_mode & 0o777 == 0o600
    connected = runtime.connect(path=address(path, "records"))
    assert connected.id == original.id
    assert connected.host == "ssh://sandbox"
    assert connected.data == {"path": "/work", "_connected": True}
    assert ssh_files == [
        ("ssh://user@records:2222", "write"),
        ("ssh://user@records:2222", "read"),
    ]


@pytest.mark.parametrize("content", [None, "not JSON", '{"version": 99, "backend": "test"}'])
def test_invalid_remote_record_does_not_connect(tmp_path, runtime, ssh_files, monkeypatch, content):
    path = tmp_path / "instance.json"
    if content is not None:
        path.write_text(content)
    monkeypatch.setattr(runtime, "_backend", lambda *args: pytest.fail("must not connect"))
    with pytest.raises(RuntimeError if content is None else ValueError):
        runtime.connect(path=address(path))


def test_invalid_record_does_not_overwrite_remote_file(tmp_path, runtime, ssh_files):
    path = tmp_path / "instance.json"
    path.write_text("original")
    instance = Instance("one", "test", data={"value": float("nan")})
    with pytest.raises(ValueError):
        runtime.describe(instance=instance, path=address(path))
    assert path.read_text() == "original"
    assert not ssh_files


@pytest.mark.parametrize("directory", [False, True])
def test_ssh_roundtrip_merge_and_temporary_cleanup(tmp_path, ssh_files, directory):
    source = tmp_path / "source '中文 #.bin"
    target = tmp_path / "target"
    body = bytes(range(256)) * 20
    if directory:
        source.mkdir()
        (source / "empty").mkdir()
        (source / ".data").write_bytes(body)
        target.mkdir()
        (target / "keep").write_text("untouched")
        (target / ".data").write_text("old")
    else:
        source.write_bytes(body)
        target.write_text("old")
    with local_source(address(source)) as local:
        assert local.name == source.name
        with local_target(address(target, "outputs")) as destination:
            if directory:
                shutil.copytree(local, destination)
            else:
                shutil.copy2(local, destination)
        assert not destination.exists()
    assert not local.exists()
    assert (target / ".data" if directory else target).read_bytes() == body
    if directory:
        assert (target / "empty").is_dir()
        assert (target / "keep").read_text() == "untouched"
    assert ssh_files == [("ssh://user@files:2222", "read"), ("ssh://user@outputs:2222", "write")]


def test_failed_download_keeps_destination_and_cleans_staging(tmp_path, ssh_files):
    target = tmp_path / "target"
    target.write_text("original")
    with pytest.raises(OSError, match="interrupted"):
        with local_target(address(target)) as stage:
            stage.write_text("incomplete")
            raise OSError("interrupted")
    assert not stage.exists()
    assert target.read_text() == "original"
    assert not ssh_files


@pytest.mark.parametrize("directory", [False, True])
def test_ssh_copy_preserves_executable_permissions(tmp_path, ssh_files, directory):
    source = tmp_path / "source"
    if directory:
        source.mkdir()
        script = source / "run.sh"
    else:
        script = source
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o750)
    with local_source(address(source)) as local:
        with local_target(address(tmp_path / "result")) as target:
            if directory:
                shutil.copytree(local, target)
            else:
                shutil.copy2(local, target)
    output = tmp_path / "result/run.sh" if directory else tmp_path / "result"
    assert output.stat().st_mode & 0o777 == 0o750


def test_source_ending_in_parent_directory_stays_inside_staging(tmp_path, ssh_files):
    folder = tmp_path / "inputs/child"
    folder.mkdir(parents=True)
    (folder.parent / "data").write_text("input")
    with local_source(address(folder / "..")) as local:
        assert local.name == "data"
        assert (local / "data").read_text() == "input"
        assert local.parent.name.startswith("envweaver-files-")
    assert not local.exists()


def test_missing_ssh_source_and_type_conflicts(tmp_path, ssh_files):
    with pytest.raises(RuntimeError, match="FileNotFoundError"):
        with local_source(address(tmp_path / "missing")):
            pytest.fail("Must not yield a missing source")
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(RuntimeError, match="IsADirectoryError"):
        with local_target(address(directory)) as stage:
            stage.write_text("file")
    assert directory.is_dir()
    assert not stage.exists()


def test_unsupported_protocol_is_not_treated_as_local():
    with pytest.raises(ValueError, match="Unsupported file address scheme: s3"):
        with local_source("s3://bucket/input"):
            pytest.fail("S3 is not implemented yet")


@pytest.mark.parametrize("directory", [False, True])
def test_runtime_upload_download_ssh_through_guest(tmp_path, ssh_instance, ssh_files, directory):
    runtime = Runtime(root=tmp_path / "records", ssh={"config_file": str(tmp_path / "sandbox_config")})
    source, inside, output = [tmp_path / name for name in ("source", "inside", "output")]
    if directory:
        source.mkdir()
        (source / "data.bin").write_bytes(b"\x00\xff\r\n")
        (source / "empty").mkdir()
    else:
        source.write_bytes(b"\x00\xff\r\n")
    runtime.upload(instance=ssh_instance, source=address(source), target=str(inside))
    runtime.download(instance=ssh_instance, source=str(inside), target=address(output))
    assert (output / "data.bin" if directory else output).read_bytes() == b"\x00\xff\r\n"
    if directory:
        assert (output / "empty").is_dir()


def test_context_and_task_archive_remote_files(tmp_path, runtime, ssh_files):
    base = tmp_path / "base"
    base.mkdir()
    source = tmp_path / "input with spaces"
    source.mkdir()
    (source / "data").write_text("input")
    context = [{"source": address(source), "target": "/work"}]
    ref = runtime.create(backend="test", image=str(base))
    runtime.ssh = {"config_file": str(tmp_path / "sandbox_config")}
    instance = runtime.start(ref=ref, context=context)
    assert (Path(instance.data["path"]) / "work/data").read_text() == "input"
    verifier = tmp_path / "verify/run.py"
    verifier.parent.mkdir()
    verifier.write_text("print('{\"score\": 1}')")
    pool = TaskPool(root=tmp_path / "tasks", runtime=runtime)
    saved = pool.save(
        cwd="/", verifier_runtime_ref=ref,
        task_runtime_ref=ref,
        name="remote-context",
        goal=[{"type": "text", "text": "Task"}],
        verifier=Verifier(entry=verifier, timeout=60, runtime=runtime),
        context=context,
    )
    shutil.rmtree(source)
    runtime.close(instance)
    task = pool.load(saved)
    assert Path(task.context[0]["source"]).name == "input with spaces"
    restored = runtime.start(ref=task.task_runtime_ref, context=task.context)
    assert (Path(restored.data["path"]) / "work/data").read_text() == "input"
    runtime.close(restored)
    assert ssh_files == [("ssh://user@files:2222", "read")] * 2


def test_context_ssh_failure_closes_instance(tmp_path, runtime, ssh_files):
    base = tmp_path / "base"
    base.mkdir()
    ref = runtime.create(backend="test", image=str(base))
    with pytest.raises(RuntimeError, match="FileNotFoundError"):
        runtime.start(ref=ref, context=[{"source": address(tmp_path / "missing"), "target": "/x"}])
    assert list(runtime.root.iterdir()) == []


@pytest.mark.parametrize("remote_pool", [False, True])
def test_task_archives_local_and_ssh_goal_images(tmp_path, runtime, ssh_files, remote_pool):
    base = tmp_path / "base"
    base.mkdir()
    ref = runtime.create(backend="test", image=str(base))
    runtime.ssh = {"config_file": str(tmp_path / "sandbox_config")}
    materials = tmp_path / "materials"
    materials.mkdir()
    local = materials / "local.png"
    remote = materials / "截图 ' #.png"
    local.write_bytes(b"local image")
    remote.write_bytes(bytes(range(256)))
    entry = materials / "run.py"
    entry.write_text("print('{\"score\": 1}')")
    goal = [
        {"type": "text", "text": "Before"},
        {"type": "image", "path": str(local)},
        {"type": "text", "text": "After"},
        {"type": "image", "path": address(remote)},
    ]
    root = tmp_path / "pool"
    pool = TaskPool(root=address(root) if remote_pool else root, runtime=runtime)
    saved = pool.save(
        cwd="/", task_runtime_ref=ref,
        name="images",
        goal=goal,
        verifier=Verifier(entry=entry, runtime=runtime, timeout=10),
        verifier_runtime_ref=ref,
    )
    shutil.rmtree(materials)
    task = pool.load(saved)
    assert task.goal[0] == goal[0] and task.goal[2] == goal[2]
    assert Path(task.goal[1]["path"]).read_bytes() == b"local image"
    assert Path(task.goal[3]["path"]).read_bytes() == bytes(range(256))
    manifest = Path(task.verifier.entry).parent.parent / "task.json"
    archived = json.loads(manifest.read_text())["goal"]
    assert archived[1]["path"] == "goal/1.png"
    assert archived[3]["path"] == "goal/3.png"
    assert goal[3]["path"] == address(remote)


def test_missing_ssh_goal_image_does_not_save_snapshot(tmp_path, runtime, ssh_files, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    ref = runtime.create(backend="test", image=str(base))
    entry = tmp_path / "verify/run.py"
    entry.parent.mkdir()
    entry.write_text("print('{\"score\": 1}')")
    pool = TaskPool(root=tmp_path / "pool", runtime=runtime)
    monkeypatch.setattr(
        runtime, "save", lambda *args, **kwargs: pytest.fail("must not save snapshot")
    )
    with pytest.raises(RuntimeError, match="FileNotFoundError"):
        pool.save(
            cwd="/", task_runtime_ref=ref,
            name="missing-image",
            goal=[{"type": "image", "path": address(tmp_path / "missing.png")}],
            verifier=Verifier(entry=entry, runtime=runtime, timeout=10),
            verifier_runtime_ref=ref,
        )
    assert pool.search() == []
    assert not list(pool.root.iterdir())


@pytest.mark.parametrize("remote_pool", [False, True])
def test_task_archives_remote_verifier_directory(tmp_path, runtime, ssh_files, remote_pool):
    base = tmp_path / "base"
    base.mkdir()
    ref = runtime.create(backend="test", image=str(base))
    runtime.ssh = {"config_file": str(tmp_path / "sandbox_config")}
    source = tmp_path / "remote verifier '中文 #"
    source.mkdir()
    (source / "run.py").write_text("print('{\"score\": 1}')")
    (source / "nested").mkdir()
    (source / "nested/expected.bin").write_bytes(bytes(range(256)))
    (source / "helper.py").write_text("ANSWER = 1")
    (source / "helper.py").chmod(0o750)
    root = tmp_path / "pool"
    pool = TaskPool(root=address(root) if remote_pool else root, runtime=runtime)
    saved = pool.save(
        cwd="/", task_runtime_ref=ref,
        name="remote-verifier",
        goal=[{"type": "text", "text": "Task"}],
        verifier=Verifier(entry=address(source / "run.py"), runtime=runtime, timeout=30),
        verifier_runtime_ref=ref,
    )
    shutil.rmtree(source)
    task = pool.load(saved)
    package = Path(task.verifier.entry).parent
    assert (package / "nested/expected.bin").read_bytes() == bytes(range(256))
    assert (package / "helper.py").stat().st_mode & 0o777 == 0o750
    assert (
        json.loads((package.parent / "task.json").read_text())["verifier"]["entry"]
        == "verifier/run.py"
    )
    assert "ssh://" not in (package.parent / "task.json").read_text()
    assert (
        subprocess.run(
            [sys.executable, task.verifier.entry], capture_output=True, check=True
        ).stdout.strip()
        == b'{"score": 1}'
    )


@pytest.mark.parametrize("outcome", ["success", "crash", "timeout"])
def test_verify_remote_entry_with_sidecars_and_cleanup(tmp_path, ssh_files, monkeypatch, outcome):
    from contextlib import contextmanager

    from envweaver import RuntimeRef
    from envweaver import verifier as module
    from envweaver.errors import VerifierError
    from tests.conftest import run_verifier
    from tests.verifier.test_verifier_instance import LocalTransport

    root = tmp_path / "sandboxes"
    root.mkdir()
    runtime = LocalTransport(root)
    source = tmp_path / "verify '中文 #"
    source.mkdir()
    (source / "nested").mkdir()
    (source / "nested/empty").mkdir()
    (source / "nested/expected.bin").write_bytes(bytes(range(256)))
    (source / "helper.py").write_text("SCORE = 0.5")
    body = (
        "from pathlib import Path\nfrom helper import SCORE\n"
        "assert Path('nested/expected.bin').read_bytes() == bytes(range(256))\n"
        "assert Path('nested/empty').is_dir()\n"
    )
    tail = {
        "success": "import json; print(json.dumps({'score': SCORE}))",
        "crash": "raise RuntimeError('expected failure')",
        "timeout": "import time; time.sleep(30)",
    }[outcome]
    (source / "run.py").write_text(body + tail)
    observed = []
    original = module.local_source

    @contextmanager
    def observe(*args, **kwargs):
        with original(*args, **kwargs) as path:
            observed.append(path)
            yield path

    monkeypatch.setattr(module, "local_source", observe)
    checker = Verifier(
        entry=address(source / "run.py"),
        runtime=runtime,
        timeout=0.3 if outcome == "timeout" else 10,
        requires_task_instance=False,
    )
    if outcome == "success":
        assert run_verifier(checker, RuntimeRef("test", "unused")) == {"score": 0.5}
    else:
        with pytest.raises(VerifierError):
            run_verifier(checker, RuntimeRef("test", "unused"))
    assert observed and all(not path.exists() for path in observed)
    assert (source / "run.py").exists()


@pytest.mark.parametrize(
    "entry",
    [
        "https://server/verify/run.py",
        "ssh://server/verify/check.py",
        "ssh://server/verify/run.py?token=x",
        "ssh://server/verify/run.py#fragment",
    ],
)
def test_invalid_remote_verifier_entry(entry, runtime):
    with pytest.raises(ValueError):
        Verifier(entry=entry, runtime=runtime, timeout=10)


def test_missing_remote_entry_does_not_save_snapshot(tmp_path, runtime, ssh_files, monkeypatch):
    source = tmp_path / "no-entry"
    source.mkdir()
    (source / "helper.py").write_text("ANSWER = 1")
    base = tmp_path / "base"
    base.mkdir()
    ref = runtime.create(backend="test", image=str(base))
    pool = TaskPool(root=tmp_path / "pool", runtime=runtime)
    monkeypatch.setattr(
        runtime, "save", lambda *args, **kwargs: pytest.fail("must not save snapshot")
    )
    with pytest.raises(FileNotFoundError):
        pool.save(
            cwd="/", task_runtime_ref=ref,
            name="missing",
            goal=[{"type": "text", "text": "Task"}],
            verifier=Verifier(entry=address(source / "run.py"), runtime=runtime, timeout=10),
            verifier_runtime_ref=ref,
        )
    assert pool.search() == []
    assert not list(pool.root.iterdir())


def test_archive_path_traversal_rejected(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escape", "bad")
    with pytest.raises(ValueError, match="Unsafe"):
        unpack("directory", buffer.getvalue(), tmp_path / "target")
    assert not (tmp_path / "escape").exists()
