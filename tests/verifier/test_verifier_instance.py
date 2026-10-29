"""Real verifier subprocesses over a local transport double; no sandbox claims."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from envweaver import Instance, Runtime, RuntimeRef, Verifier
from envweaver._io import ssh_transport as _ssh_transport
from envweaver._io.storage import _operate
from envweaver.errors import VerifierError
from envweaver.verifier import _Evaluation
from tests.conftest import run_process, run_verifier, stop_process


class LocalTransport:
    ssh = None

    def __init__(self, root):
        self.root = root
        self.started = []
        self.closed = []
        self.operations = []
        self.runner = Instance("checker", "test", data={"python": sys.executable})
        self.processes = {}

    def start(self, ref, connection=None):
        from dataclasses import replace

        self.started.append((ref, connection))
        self.runner = replace(self.runner, id=f"checker-{len(self.started)}")
        (self.root / self.runner.id).mkdir()
        return self.runner

    def close(self, instance):
        self.closed.append(instance)
        for process in self.processes.get(instance.id, []):
            stop_process(process)
        shutil.rmtree(self.root / instance.id)

    def exec(self, instance, command, cwd=None, timeout=300):
        source = command[-1] if isinstance(command, list) else command
        if not instance.id.startswith("checker-"):
            self.operations.append((instance.id, "exec", command))
            result = run_process(
                command if isinstance(command, list) else [sys.executable, "-c", command],
                cwd=cwd,
                timeout=timeout,
            )
        else:
            if "tempfile.mkdtemp" in source:
                source = source.replace(
                    "tempfile.mkdtemp(", f"tempfile.mkdtemp(dir={str(self.root / instance.id)!r}, "
                )
            process = subprocess.Popen(
                [sys.executable, "-c", source],
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            self.processes.setdefault(instance.id, []).append(process)
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except BaseException:
                stop_process(process)
                raise
            result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        return result.returncode, result.stdout.decode(), result.stderr.decode()

    def upload(self, instance, source, target):
        self.operations.append((instance.id, "upload", str(target)))
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if Path(source).is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)

    def download(self, instance, source, target):
        self.operations.append((instance.id, "download", str(source)))
        self.upload(instance, source, target)

    def screenshot(self, instance):
        self.operations.append((instance.id, "screenshot"))
        return b"PNG-test"


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "sandbox temp"
    root.mkdir()
    materials = tmp_path / "验证 materials"
    materials.mkdir()
    entry = materials / "run.py"
    entry.write_text("print('{\"score\": 1}')")
    runtime = LocalTransport(root)
    checker = Verifier(
        entry=entry,
        timeout=10,
        runtime=runtime,
        requires_task_instance=False,
    )
    return runtime, checker, Instance("candidate", "test")


@pytest.mark.parametrize("required", [None, False, True])
@pytest.mark.parametrize("provided", [False, True])
def test_task_instance_declaration(setup, required, provided):
    runtime, checker, task_instance = setup
    options = {} if required is None else {"requires_task_instance": required}
    checker = Verifier(
        entry=checker.entry, runtime=runtime, timeout=10, **options
    )
    required = True if required is None else required
    assert checker.requires_task_instance is required
    task_instance = task_instance if provided else None
    if required != provided:
        with pytest.raises(ValueError, match="requires_task_instance"):
            checker.run(verifier_instance=runtime.runner, task_instance=task_instance)
        assert not runtime.operations
        assert not runtime.started
        assert not runtime.processes
        return
    checker.entry.write_text(f"""import argparse, json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--instance')
args = parser.parse_args()
assert bool(args.instance) == {required!r}
if args.instance:
    from envweaver import Runtime
    instance = Runtime().connect(path=args.instance)
    assert instance.id == 'candidate'
print(json.dumps({{'score': 1}}))
""")
    assert run_verifier(checker, RuntimeRef("test", "prepared"), task_instance=task_instance) == {
        "score": 1
    }


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_task_instance_declaration_requires_boolean(setup, value):
    runtime, checker, _ = setup
    with pytest.raises(ValueError, match="requires_task_instance"):
        Verifier(entry=checker.entry, runtime=runtime, timeout=10, requires_task_instance=value)


def test_fresh_sandboxes_materials_env_and_forwarded_operations(setup, tmp_path, monkeypatch):
    runtime, checker, target = setup
    checker.requires_task_instance = True
    monkeypatch.setenv("CHECKER_TOKEN", "must-not-be-used")
    checker.required_env.append("CHECKER_TOKEN")
    (checker.entry.parent / "test.bin").write_bytes(b"\x00\xfftest")
    target_file = tmp_path / "task tests" / "test.bin"
    checker.entry.write_text(f"""import argparse, json, os
from pathlib import Path
from envweaver import Runtime
p=argparse.ArgumentParser(); p.add_argument('--instance'); path=p.parse_args().instance
assert os.environ['CHECKER_TOKEN'] == 'provided'
r=Runtime(); i=r.connect(path=path)
r.describe(instance=i, path="forwarded.json")
i=r.connect(path="forwarded.json")
assert not Path('output.bin').exists()
r.upload(instance=i, source='test.bin', target={str(target_file)!r})
r.download(instance=i, source={str(target_file)!r}, target='output.bin')
assert Path('output.bin').read_bytes() == b'\\x00\\xfftest'
assert r.exec(instance=i, command="print('checked')") == (0, 'checked\\n', '')
assert r.screenshot(instance=i) == b'PNG-test'
try: r.close(instance=i)
except ValueError: pass
else: raise AssertionError('must not close task')
print(json.dumps({{'score': 1, 'target': i.id}}))
""")
    assert run_verifier(
        checker,
        RuntimeRef("test", "prepared"),
        task_instance=target,
        env={"CHECKER_TOKEN": "provided"},
    ) == {
        "score": 1,
        "target": "candidate",
    }
    assert not list(runtime.root.iterdir())
    second = Instance("second", "qemu", host="ssh://only-caller-can-reach")
    assert (
        run_verifier(
            checker,
            RuntimeRef("test", "prepared"),
            task_instance=second,
            env={"CHECKER_TOKEN": "provided"},
        )["target"]
        == "second"
    )
    assert len(runtime.started) == 2
    assert len({i.id for i in runtime.closed}) == 2
    assert len(runtime.closed) == len(runtime.started)
    assert target_file.read_bytes() == b"\x00\xfftest"
    assert not (checker.entry.parent / "output.bin").exists()
    assert {item[0] for item in runtime.operations} == {
        "checker-1",
        "checker-2",
        "candidate",
        "second",
    }


@pytest.mark.parametrize(
    "source", ["raise RuntimeError('broken')", "print('not JSON')", "print('{\"score\": 2}')"]
)
@pytest.mark.parametrize("connected", [True, False])
def test_error_then_new_run_and_close(setup, source, connected):
    runtime, checker, target = setup
    checker.requires_task_instance = connected
    target = target if connected else None
    checker.entry.write_text(source)
    with pytest.raises(VerifierError):
        run_verifier(checker, RuntimeRef("test", "prepared"), task_instance=target)
    assert not list(runtime.root.iterdir())
    checker.entry.write_text("print('{\"score\": 0}')")
    assert run_verifier(checker, RuntimeRef("test", "prepared"), task_instance=target) == {
        "score": 0
    }
    assert len(runtime.closed) == len(runtime.started)


@pytest.mark.parametrize("connected", [True, False])
def test_timeout_stops_children_and_new_run(setup, tmp_path, connected):
    runtime, checker, target = setup
    checker.requires_task_instance = connected
    target = target if connected else None
    checker.timeout = 0.7
    pid_file = tmp_path / "child.pid"
    checker.entry.write_text(f"""import subprocess, sys, time
from pathlib import Path
p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path({str(pid_file)!r}).write_text(str(p.pid))
time.sleep(60)
""")
    with pytest.raises(VerifierError, match="timed out"):
        run_verifier(checker, RuntimeRef("test", "prepared"), task_instance=target)
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", pid_file.read_text()], capture_output=True, text=True
    ).stdout.strip()
    assert not state or state.startswith("Z")
    assert not list(runtime.root.iterdir())
    checker.entry.write_text("print('{\"score\": 1}')")
    assert run_verifier(checker, RuntimeRef("test", "prepared"), task_instance=target) == {
        "score": 1
    }


@pytest.mark.parametrize("ambient", [None, "must-not-be-used"])
@pytest.mark.parametrize("env", [None, {}, {"OTHER": "value"}])
def test_required_env_checked_before_sandbox_operations(setup, monkeypatch, ambient, env):
    runtime, checker, target = setup
    checker.requires_task_instance = True
    monkeypatch.delenv("ENVWEAVER_MISSING_TEST_KEY", raising=False)
    if ambient is not None:
        monkeypatch.setenv("ENVWEAVER_MISSING_TEST_KEY", ambient)
    checker.required_env.append("ENVWEAVER_MISSING_TEST_KEY")
    with pytest.raises(ValueError, match="Missing"):
        checker.run(verifier_instance=runtime.runner, task_instance=target, env=env)
    assert not runtime.started
    assert run_verifier(
        checker,
        RuntimeRef("test", "prepared"),
        task_instance=target,
        env={"ENVWEAVER_MISSING_TEST_KEY": "provided"},
    ) == {"score": 1}
    assert runtime.closed == [runtime.runner]


def test_caller_creates_fresh_sandbox_for_every_call(setup):
    runtime, checker, target = setup
    checker.requires_task_instance = True
    checker.entry.write_text("""import json
from pathlib import Path
marker = Path.cwd().parents[1] / 'outside-working-directory'
assert not marker.exists()
marker.write_text('changed sandbox')
print(json.dumps({'score': 1}))
""")
    assert not runtime.started
    assert run_verifier(checker, RuntimeRef("test", "prepared"), task_instance=target) == {
        "score": 1
    }
    assert run_verifier(checker, RuntimeRef("test", "prepared"), task_instance=target) == {
        "score": 1
    }
    assert len(runtime.closed) == 2
    assert len({item.id for item in runtime.closed}) == 2
    assert not list(runtime.root.iterdir())


def test_upload_failure_cleanup(setup, monkeypatch):
    runtime, checker, target = setup
    checker.requires_task_instance = True

    def fail(**kwargs):
        raise OSError("upload failed")

    monkeypatch.setattr(runtime, "upload", fail)
    with pytest.raises(OSError, match="upload failed"):
        run_verifier(checker, RuntimeRef("test", "prepared"), task_instance=target)
    assert not list(runtime.root.iterdir())
    assert len(runtime.closed) == len(runtime.started)


def test_dispatch_rejects_unapproved_operation(setup, tmp_path):
    import time

    runtime, checker, target = setup
    with pytest.raises(ValueError, match="Unsupported"):
        _Evaluation(checker, runtime.runner)._dispatch(
            target, {"operation": "close", "arguments": {}}, tmp_path, time.monotonic() + 1
        )
    assert not runtime.closed


def test_forwarded_command_timeout_is_bounded_by_verification(setup):
    runtime, checker, target = setup
    checker.requires_task_instance = True
    checker.entry.write_text("""import argparse, json
from envweaver import Runtime
p=argparse.ArgumentParser(); p.add_argument('--instance'); r=Runtime(); i=r.connect(path=p.parse_args().instance)
try: r.exec(instance=i, command='import time; time.sleep(60)', timeout=0.1)
except TimeoutError: print(json.dumps({'score': 0.5}))
else: raise AssertionError('missing timeout')
""")
    assert run_verifier(checker, RuntimeRef("test", "prepared"), task_instance=target) == {
        "score": 0.5
    }


@pytest.mark.parametrize("directory", ["C:/Temp/verification", "/tmp/verification"])
def test_verifier_uses_backend_python_and_paths(setup, monkeypatch, directory):
    from pathlib import PurePosixPath

    runtime, checker, target = setup
    checker.requires_task_instance = True
    connection = {"custom": "connection"}
    sources, uploads = [], []

    def execute(instance, command, timeout=300):
        source = command[-1]
        assert instance == runtime.runner
        sources.append(source)
        if "tempfile.mkdtemp" in source:
            return 0, directory, ""
        if "subprocess.call" in source:
            return 0, '{"score": 1}', ""
        if "requests = []" in source:
            return 0, "[]", ""
        return 0, "", ""

    monkeypatch.setattr(runtime, "exec", execute)
    monkeypatch.setattr(runtime, "upload", lambda **kwargs: uploads.append(kwargs["target"]))
    assert run_verifier(
        checker, RuntimeRef("test", "prepared"), connection=connection, task_instance=target
    ) == {"score": 1}
    remote = PurePosixPath(directory)
    assert uploads == [str(remote / "verifier"), str(remote / "control")]
    assert runtime.started == [(RuntimeRef("test", "prepared"), connection)]
    assert len(runtime.closed) == len(runtime.started)
    assert any(repr(str(remote / "control/instance.json")) in s for s in sources)


def test_large_binary_response_and_directory_transfer(setup, tmp_path):
    runtime, checker, target = setup
    checker.requires_task_instance = True
    directory = checker.entry.parent / "tests"
    directory.mkdir()
    import os

    (directory / "large.bin").write_bytes(os.urandom(100_000))
    target_path = tmp_path / "injected"
    checker.entry.write_text(f"""import argparse, json
from pathlib import Path
from envweaver import Runtime
p=argparse.ArgumentParser(); p.add_argument('--instance'); r=Runtime(); i=r.connect(path=p.parse_args().instance)
r.upload(instance=i, source='tests', target={str(target_path)!r})
r.download(instance=i, source={str(target_path)!r}, target='received')
assert Path('received/large.bin').read_bytes() == Path('tests/large.bin').read_bytes()
print(json.dumps({{'score': 1}}))
""")
    assert run_verifier(checker, RuntimeRef("test", "prepared"), task_instance=target) == {
        "score": 1
    }


def test_connect_expired_verification(setup, tmp_path):
    import json

    from envweaver import Runtime
    from envweaver.errors import RuntimeError as RuntimeFailure

    path = tmp_path / "instance.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "backend": "_verifier",
                "id": "a",
                "directory": str(tmp_path / "gone"),
                "timeout": 1,
            }
        )
    )
    with pytest.raises(RuntimeFailure, match="ended"):
        Runtime().connect(path=path)


@pytest.mark.parametrize("connected", [True, False])
def test_files_env_optional_instance_and_isolation(setup, tmp_path, monkeypatch, connected):
    import os

    runtime, checker, target = setup
    checker.requires_task_instance = connected
    monkeypatch.setenv("REQUIRED_SETTING", "fallback")
    monkeypatch.delenv("PER_RUN_VALUE", raising=False)
    checker.required_env.append("REQUIRED_SETTING")
    source = tmp_path / "patch with spaces.patch"
    source.write_text("patch contents")
    folder = tmp_path / "input folder"
    folder.mkdir()
    (folder / "binary").write_bytes(b"\x00\xff")
    checker.entry.write_text("""import argparse, json, os
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--instance'); args=p.parse_args()
assert not Path('scratch').exists()
Path('scratch').write_text('temporary')
if args.instance:
 from envweaver import Runtime
 r=Runtime(); i=r.connect(path=args.instance)
 assert r.exec(instance=i, command="print('connected')")[1].strip() == 'connected'
else:
 assert not (Path.cwd().parent / 'control/instance.json').exists()
 assert not (Path.cwd().parent / 'mailbox').exists()
if 'PER_RUN_VALUE' in os.environ:
 assert Path('inputs/submission.patch').read_text() == 'patch contents'
 assert Path('inputs/copy.patch').read_text() == 'patch contents'
 assert Path('inputs/assets/binary').read_bytes() == bytes([0, 255])
else:
 assert not Path('inputs').exists()
print(json.dumps({'score': 1, 'setting': os.environ['REQUIRED_SETTING'],
                  'value': os.environ.get('PER_RUN_VALUE')}))
""")
    first = run_verifier(
        checker,
        RuntimeRef("test", "prepared"),
        task_instance=target if connected else None,
        files=[
            {"source": source, "target": "inputs/submission.patch"},
            {"source": source, "target": "inputs/copy.patch"},
            {"source": folder, "target": "inputs/assets"},
        ],
        env={"REQUIRED_SETTING": "override", "PER_RUN_VALUE": "秘密 ' \n \\"},
    )
    assert first == {"score": 1, "setting": "override", "value": "秘密 ' \n \\"}
    with pytest.raises(ValueError, match="REQUIRED_SETTING"):
        run_verifier(
            checker,
            RuntimeRef("test", "prepared"),
            task_instance=target if connected else None,
        )
    assert run_verifier(
        checker,
        RuntimeRef("test", "prepared"),
        task_instance=target if connected else None,
        env={"REQUIRED_SETTING": "second-run"},
    ) == {
        "score": 1,
        "setting": "second-run",
        "value": None,
    }
    assert os.environ["REQUIRED_SETTING"] == "fallback"
    assert "PER_RUN_VALUE" not in os.environ
    assert not list(runtime.root.iterdir())
    assert not (checker.entry.parent / "inputs").exists()
    if not connected:
        assert all(op[0].startswith("checker-") for op in runtime.operations)


def test_relative_and_absolute_file_destinations(setup, tmp_path):
    runtime, checker, _ = setup
    source = tmp_path / "answer.txt"
    source.write_text("answer")
    folder = tmp_path / "assets"
    folder.mkdir()
    (folder / "binary").write_bytes(b"\x00\xff")
    destination = tmp_path / "absolute sandbox path"
    destination.mkdir()
    answer = destination / "answer.txt"
    answer.write_text("old content")
    checker.entry.write_text(f"""import json
from pathlib import Path
assert Path('inputs/answer.txt').read_text() == 'answer'
assert Path({str(answer)!r}).read_text() == 'answer'
assert Path({str(destination / "assets/binary")!r}).read_bytes() == bytes([0, 255])
print(json.dumps({{'score': 1}}))
""")
    assert run_verifier(
        checker,
        RuntimeRef("test", "prepared"),
        files=[
            {"source": source, "target": "inputs/answer.txt"},
            {"source": source, "target": str(answer)},
            {"source": folder, "target": str(destination / "assets")},
        ],
    ) == {"score": 1}
    assert any(op[1:] == ("upload", str(answer)) for op in runtime.operations)


@pytest.mark.parametrize("target", ["C:/Tasks/answer.txt", r"C:\Tasks\answer.txt"])
def test_windows_absolute_destination_is_forwarded(setup, tmp_path, monkeypatch, target):
    runtime, checker, _ = setup
    source = tmp_path / "answer.txt"
    source.write_text("answer")
    upload = runtime.upload
    targets = []

    def capture(instance, source, target):
        if target.startswith("C:/"):
            targets.append(target)
        else:
            upload(instance, source, target)

    monkeypatch.setattr(runtime, "upload", capture)
    assert run_verifier(
        checker, RuntimeRef("test", "prepared"), files=[{"source": source, "target": target}]
    ) == {"score": 1}
    assert targets == ["C:/Tasks/answer.txt"]


@pytest.mark.parametrize(
    "name", ["../escape", "C:relative", "a\\b", "", ".", "a/../b", "/a/../b", "run.py"]
)
def test_reject_invalid_input_destination(setup, tmp_path, name):
    runtime, checker, _ = setup
    source = tmp_path / "input"
    source.write_text("data")
    with pytest.raises(ValueError):
        run_verifier(
            checker, RuntimeRef("test", "prepared"), files=[{"source": source, "target": name}]
        )
    assert not list(runtime.root.iterdir())
    assert run_verifier(
        checker,
        RuntimeRef("test", "prepared"),
    ) == {"score": 1}


def test_input_collisions_missing_file_and_symlink(setup, tmp_path):
    runtime, checker, _ = setup
    source = tmp_path / "input"
    source.write_text("data")
    (checker.entry.parent / "link").symlink_to(tmp_path, target_is_directory=True)
    for files in (
        [{"source": source, "target": "a"}, {"source": source, "target": "a"}],
        [{"source": source, "target": "a"}, {"source": source, "target": "a/b"}],
        [{"source": source, "target": "link/escape"}],
        [{"source": source, "target": "run.py/extra"}],
    ):
        with pytest.raises(ValueError):
            run_verifier(checker, RuntimeRef("test", "prepared"), files=files)
    with pytest.raises(FileNotFoundError):
        run_verifier(
            checker,
            RuntimeRef("test", "prepared"),
            files=[{"source": tmp_path / "missing", "target": "submission.patch"}],
        )
    assert not list(runtime.root.iterdir())


@pytest.mark.parametrize(
    "files",
    [{}, {"answer": "input"}, ["input"], [{}], [{"source": "input"}], [{"target": "answer"}]],
)
def test_invalid_file_mapping(setup, files):
    runtime, checker, _ = setup
    with pytest.raises(ValueError, match="source.*target"):
        checker.run(verifier_instance=runtime.runner, files=files)
    assert not runtime.operations


@pytest.mark.parametrize("failure", [None, "missing", "timeout"])
def test_ssh_files_use_runtime_transfer_and_cleanup(setup, tmp_path, monkeypatch, failure):
    transport, checker, _ = setup
    runtime = Runtime(ssh={"config_file": str(tmp_path / "ssh-config")})
    monkeypatch.setattr(runtime, "_backend", lambda name: transport)
    checker.runtime = runtime
    remote = tmp_path / "远程 inputs"
    remote.mkdir()
    (remote / "binary").write_bytes(b"\x00\xff")
    (remote / "empty").mkdir()
    local = tmp_path / "answer.json"
    local.write_text('{"answer": 42}')
    calls, staged = [], []
    upload = transport.upload

    def record_upload(instance, source, target):
        if Path(source).parent.name.startswith("envweaver-files-"):
            staged.append(Path(source))
        return upload(instance, source, target)

    monkeypatch.setattr(transport, "upload", record_upload)

    def ssh(host, command, payload, *, ssh_options=None):
        calls.append((host, ssh_options))
        if failure == "timeout":
            raise TimeoutError("SSH timed out")
        try:
            return json.dumps(_operate(payload)).encode()
        except OSError as error:
            from envweaver.errors import RuntimeError

            raise RuntimeError(str(error)) from error

    monkeypatch.setattr(_ssh_transport, "ssh", ssh)
    checker.entry.write_text("""import json
from pathlib import Path
assert json.loads(Path('answer.json').read_text()) == {'answer': 42}
assert Path('inputs/assets/binary').read_bytes() == bytes([0, 255])
assert Path('inputs/assets/empty').is_dir()
assert Path('inputs/copy.bin').read_bytes() == bytes([0, 255])
print(json.dumps({'score': 1}))
""")
    address = "ssh://user@files:2222" + quote(str(remote), safe="/")
    files = [
        {"source": local, "target": "answer.json"},
        {"source": address, "target": "inputs/assets"},
        {
            "source": address + ("/missing" if failure == "missing" else "/binary"),
            "target": "inputs/copy.bin",
        },
    ]
    if failure:
        from envweaver.errors import RuntimeError

        with pytest.raises(TimeoutError if failure == "timeout" else RuntimeError):
            run_verifier(checker, RuntimeRef("test", "prepared"), files=files)
    else:
        assert run_verifier(checker, RuntimeRef("test", "prepared"), files=files) == {"score": 1}
        assert len(staged) == 2
    assert calls and all(call == ("ssh://user@files:2222", None) for call in calls)
    assert all(not path.exists() for path in staged)
    assert transport.closed == [transport.runner]
    assert not list(transport.root.iterdir())
    assert not (checker.entry.parent / "inputs").exists()
    assert (remote / "binary").read_bytes() == b"\x00\xff"


@pytest.mark.parametrize("source", ["s3://bucket/file", "ssh://server", "ssh://server/a?b"])
def test_invalid_source_address_fails_before_sandbox_operations(setup, source):
    runtime, checker, _ = setup
    with pytest.raises(ValueError):
        checker.run(
            verifier_instance=runtime.runner, files=[{"source": source, "target": "answer"}]
        )
    assert not runtime.started


@pytest.mark.parametrize("env", [{"A=B": "x"}, {"A": None}, {"A": "x\0y"}, {"": "x"}])
def test_invalid_environment(setup, env):
    runtime, checker, _ = setup
    with pytest.raises(ValueError):
        run_verifier(checker, RuntimeRef("test", "prepared"), env=env)
    assert not list(runtime.root.iterdir())


def test_forwarding_failure_stops_verifier_promptly(setup, monkeypatch, tmp_path):
    import time

    runtime, checker, target = setup
    checker.requires_task_instance = True
    ready = tmp_path / "ready"
    checker.entry.write_text(
        f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(60)"
    )

    def fail(*args):
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise OSError("connection lost")

    monkeypatch.setattr(_Evaluation, "_forward", fail)
    started = time.monotonic()
    with pytest.raises(OSError, match="connection lost"):
        run_verifier(checker, RuntimeRef("test", "prepared"), task_instance=target)
    assert time.monotonic() - started < 5
    assert not list(runtime.root.iterdir())


def test_python_failure_destroys_new_sandbox(setup, monkeypatch):
    runtime, checker, _ = setup
    runtime.runner = Instance("vm", "qemu", data={"ssh": {"host": "guest"}})

    def fail(*args, **kwargs):
        raise OSError("guest unavailable")

    monkeypatch.setattr(runtime, "exec", fail)
    with pytest.raises(OSError, match="guest unavailable"):
        run_verifier(
            checker,
            RuntimeRef("test", "prepared"),
        )
    assert runtime.closed == [runtime.runner]
    assert not list(runtime.root.iterdir())


@pytest.mark.parametrize(
    "source",
    ["print('{\"score\": 1}')", "raise RuntimeError('failed')", "import time; time.sleep(30)"],
)
def test_caller_owns_sandbox_after_success_error_or_timeout(setup, monkeypatch, source):
    runtime, checker, target = setup
    checker.requires_task_instance = True
    verifier_instance = runtime.start(ref=RuntimeRef("test", "prepared"))
    checker.timeout = 0.5
    checker.entry.write_text(source)
    close = runtime.close
    monkeypatch.setattr(
        runtime, "start", lambda **kw: pytest.fail("Verifier must not start a sandbox")
    )
    monkeypatch.setattr(
        runtime, "close", lambda **kw: pytest.fail("Verifier must not close a sandbox")
    )
    try:
        if source.startswith("print"):
            assert checker.run(verifier_instance=verifier_instance, task_instance=target) == {
                "score": 1
            }
        else:
            with pytest.raises(VerifierError):
                checker.run(verifier_instance=verifier_instance, task_instance=target)
        assert (runtime.root / verifier_instance.id).is_dir()
        assert not runtime.closed
    finally:
        close(instance=verifier_instance)
    assert runtime.closed == [verifier_instance]


def test_verifier_instance_is_required(setup):
    _, checker, _ = setup
    with pytest.raises(TypeError, match="verifier_instance"):
        checker.run()
