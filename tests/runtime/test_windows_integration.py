"""Real Windows guest operations and verification across Windows/Linux sandboxes."""

import io
import json
import os
import time
from pathlib import Path

import pytest
from PIL import Image

from envweaver import Runtime, RuntimeRef, Verifier
from tests.agents.helpers import CooperativeAgent

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("ENVWEAVER_TEST_WINDOWS_REF") and os.environ.get("ENVWEAVER_TEST_QEMU_REF")),
        reason="requires prepared Windows and Linux QEMU references",
    ),
]


def test_windows_commands_files_timeout_and_cross_os_verifier(tmp_path):
    runtime = Runtime(root=tmp_path / "records")
    ref = RuntimeRef.from_dict(json.loads(Path(os.environ["ENVWEAVER_TEST_WINDOWS_REF"]).read_text()))
    linux_ref = RuntimeRef.from_dict(json.loads(Path(os.environ["ENVWEAVER_TEST_QEMU_REF"]).read_text()))
    connection = json.loads(os.environ["ENVWEAVER_TEST_WINDOWS_CONNECTION"])
    python = connection["ssh"]["python"]
    source = tmp_path / "input.txt"
    source.write_text("中文 input", encoding="utf-8")
    target = "C:/EnvWeaver 测试/input.txt"
    windows = linux = None
    try:
        windows = runtime.start(
            ref=ref, connection=connection,
            context=[{"source": str(source), "target": target}],
            setup={"command": [python, "-c", f"from pathlib import Path; assert Path({target!r}).read_text(encoding='utf-8') == '中文 input'"], "timeout": 120},
        )
        arguments = ["a b", 'a"b', "a&b", "中文", "trailing\\"]
        code, stdout, stderr = runtime.exec(
            instance=windows,
            command=[python, "-c", "import sys,json; print(json.dumps(sys.argv[1:]))", *arguments],
            cwd="C:/EnvWeaver 测试",
        )
        assert code == 0, stderr
        assert json.loads(stdout) == arguments
        assert runtime.exec(instance=windows, command="Write-Output 'PowerShell works'")[1].strip() == "PowerShell works"
        assert runtime.exec(instance=windows, command=[python, "-c", "raise SystemExit(7)"])[0] == 7
        local = tmp_path / "download"
        runtime.download(instance=windows, source="C:/EnvWeaver 测试", target=local)
        assert (local / "input.txt").read_text(encoding="utf-8") == "中文 input"
        record = tmp_path / "instance.json"
        runtime.describe(instance=windows, path=record)
        connected = Runtime().connect(path=record)
        assert connected.id == windows.id
        with Image.open(io.BytesIO(runtime.screenshot(instance=connected))) as picture:
            assert picture.format == "PNG" and picture.width > 0

        marker = "C:/EnvWeaver 测试/late.txt"
        child = f"import time; from pathlib import Path; time.sleep(5); Path({marker!r}).write_text('leaked')"
        parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(30)"
        with pytest.raises(TimeoutError):
            runtime.exec(instance=windows, command=[python, "-c", parent], timeout=2)
        stopped = time.monotonic()

        linux = runtime.start(ref=linux_ref)
        entry = tmp_path / "checker/run.py"
        entry.parent.mkdir()
        entry.write_text(
            "import argparse,json,tempfile\nfrom pathlib import Path\nfrom envweaver import Runtime\n"
            "p=argparse.ArgumentParser();p.add_argument('--instance');r=Runtime();i=r.connect(path=p.parse_args().instance)\n"
            "with tempfile.TemporaryDirectory() as d:\n"
            f" f=Path(d)/'input';r.download(instance=i,source={target!r},target=f)\n"
            " assert f.read_text(encoding='utf-8')=='中文 input'\n"
            " assert r.screenshot(instance=i).startswith(b'\\x89PNG')\n"
            " print(json.dumps({'score':1,'details':{'os_pair':'linux-checks-windows'}}))\n",
            encoding="utf-8",
        )
        verifier = Verifier(entry=entry, runtime=runtime, timeout=180)
        assert verifier.run(verifier_instance=linux, task_instance=windows)["score"] == 1

        runtime.upload(instance=linux, source=source, target="/tmp/ew-windows-input.txt")
        entry.write_text(
            "import argparse,json,os,tempfile\nfrom pathlib import Path\nfrom envweaver import Runtime\n"
            "assert os.name=='nt' and os.environ['EW_EXPECTED']=='yes'\n"
            "assert Path('provided.txt').read_text(encoding='utf-8')=='中文 input'\n"
            "p=argparse.ArgumentParser();p.add_argument('--instance');r=Runtime();i=r.connect(path=p.parse_args().instance)\n"
            "with tempfile.TemporaryDirectory() as d:\n"
            " f=Path(d)/'input';r.download(instance=i,source='/tmp/ew-windows-input.txt',target=f)\n"
            " assert f.read_text(encoding='utf-8')=='中文 input'\n"
            " print(json.dumps({'score':1,'details':{'os_pair':'windows-checks-linux'}}))\n",
            encoding="utf-8",
        )
        verifier = Verifier(entry=entry, runtime=runtime, timeout=180, required_env=["EW_EXPECTED"])
        assert verifier.run(
            verifier_instance=windows, task_instance=linux,
            files=[{"source": source, "target": "provided.txt"}], env={"EW_EXPECTED": "yes"},
        )["score"] == 1
        time.sleep(max(0, 6 - (time.monotonic() - stopped)))
        assert runtime.exec(
            instance=windows, command=[python, "-c", f"from pathlib import Path; assert not Path({marker!r}).exists()"],
        )[0] == 0
    finally:
        try:
            if linux is not None:
                runtime.close(instance=linux)
        finally:
            if windows is not None:
                runtime.close(instance=windows)
    assert not list((tmp_path / "records/instances").glob("*/instance.json"))


def test_windows_eval_with_image_and_cwd(tmp_path):
    from envweaver import EvalConfig, Task, eval

    runtime = Runtime(root=tmp_path / "records")
    ref = RuntimeRef.from_dict(json.loads(Path(os.environ["ENVWEAVER_TEST_WINDOWS_REF"]).read_text()))
    connection = json.loads(os.environ["ENVWEAVER_TEST_WINDOWS_CONNECTION"])
    python = connection["ssh"]["python"]
    image = tmp_path / "goal.png"
    Image.new("RGB", (3, 2), "red").save(image)
    source = tmp_path / "input.txt"
    source.write_text("prepared")
    entry = tmp_path / "checker/run.py"
    entry.parent.mkdir()
    entry.write_text(
        "import argparse,json,os,tempfile\nfrom pathlib import Path\nfrom envweaver import Runtime\n"
        "assert os.name=='nt'\n"
        "p=argparse.ArgumentParser();p.add_argument('--instance');r=Runtime();i=r.connect(path=p.parse_args().instance)\n"
        "with tempfile.TemporaryDirectory() as d:\n"
        " f=Path(d)/'answer';r.download(instance=i,source='C:/EW Eval/answer.txt',target=f)\n"
        " print(json.dumps({'score':float(f.read_text()==os.environ['EXPECTED'])}))\n",
    )

    class WindowsAgent(CooperativeAgent):
        def run(self, goal, *, runtime, instance, output, cwd: str):
            assert [item["type"] for item in goal] == ["text", "image"]
            runtime.upload(instance=instance, source=goal[1]["path"], target="C:/EW Eval/goal.png")
            code, _, stderr = runtime.exec(
                instance=instance, cwd=cwd,
                command=[python, "-c", "from PIL import Image; from pathlib import Path; Path('answer.txt').write_text(str(Image.open('goal.png').width))"],
            )
            assert code == 0, stderr
            output.record("message", {"text": "answer saved"})
            return output.finish([{"type": "text", "text": "done"}])

    task = Task(
        name="windows-eval", task_ref="integration", metadata={},
        goal=[{"type": "text", "text": "Read image width"}, {"type": "image", "path": str(image)}],
        task_runtime_ref=ref, verifier_runtime_ref=ref,
        context=[{"source": str(source), "target": "C:/EW Eval/input.txt"}],
        cwd="C:/EW Eval",
        setup={"command": [python, "-c", "from pathlib import Path; assert Path('C:/EW Eval/input.txt').read_text()=='prepared'"], "timeout": 120},
        verifier=Verifier(entry=entry, runtime=runtime, timeout=180, required_env=["EXPECTED"]),
    )
    (result,) = eval(
        tasks=task, agent=WindowsAgent(), output_dir=tmp_path / "run",
        config=EvalConfig(task_connection=connection, verifier_connection=connection, verifier_env={"EXPECTED": "3"}),
    )
    assert result["errors"] == [], result
    assert result["verification_result"]["score"] == 1
    assert result["agent_result"]["artifacts"]["logs"] is not None
    assert json.loads((tmp_path / "run/results.json").read_text()) == [result]
    assert not list((tmp_path / "records/instances").glob("*/instance.json"))
