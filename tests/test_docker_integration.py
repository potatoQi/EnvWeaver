import os
from pathlib import Path

import pytest

from envweaver import Runtime, RuntimeRef, TaskPool, verifier

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ENVWEAVER_TEST_DOCKER"),
        reason="set ENVWEAVER_TEST_DOCKER to a prepared image",
    ),
]


def test_real_docker_task_lifecycle(tmp_path):
    runtime = Runtime(tmp_path / "assets")
    pool = TaskPool(tmp_path / "pool", runtime=runtime)
    ref = RuntimeRef(
        "docker", os.environ["ENVWEAVER_TEST_DOCKER"], {"command": ["sleep", "infinity"]}
    )
    original = runtime.start(ref)
    instances = [original]
    try:
        source = tmp_path / "state.txt"
        source.write_text("broken")
        runtime.upload(original, source, "/work/state.txt")
        assert runtime.exec(original, "cat state.txt", "/work") == (0, "broken", "")
        with pytest.raises(TimeoutError):
            runtime.exec(original, "sleep 30", timeout=0.1)
        package = tmp_path / "checker"
        package.mkdir()
        entry = package / "run.py"
        entry.write_text("""import argparse, json
from envweaver import runtime
p = argparse.ArgumentParser()
p.add_argument('--instance')
i = runtime.connect(p.parse_args().instance)
code, out, err = runtime.exec(i, 'cat /work/state.txt')
if code: raise RuntimeError(err)
print(json.dumps({'score': float(out.strip() == 'fixed')}))
""")
        assert verifier.run(entry, original)["score"] == 0
        context = tmp_path / "inputs"
        context.mkdir()
        (context / "data.csv").write_text("original input")
        task_ref = pool.save(
            original,
            "bug",
            [{"type": "text", "text": "fix state.txt"}],
            entry,
            [{"source": str(context), "target": "/inputs"}],
        )
        assert runtime.exec(original, "cat /work/state.txt")[1] == "broken"
        assert runtime.close(original) == (True, None)
        instances.remove(original)
        source.unlink()
        entry.unlink()
        task = pool.load(task_ref)
        first = runtime.start(task.runtime, task.context)
        instances.append(first)
        assert verifier.run(task.verifier, first)["score"] == 0
        assert runtime.exec(first, "printf fixed > /work/state.txt")[0] == 0
        assert verifier.run(task.verifier, first)["score"] == 1
        runtime.download(first, "/inputs", tmp_path / "download")
        assert (tmp_path / "download/data.csv").read_text() == "original input"
        # Re-copy onto the same target must not add an extra nested directory.
        runtime.upload(first, context, "/inputs")
        assert runtime.exec(first, "cat /inputs/data.csv")[1] == "original input"
        second = runtime.start(task.runtime, task.context)
        instances.append(second)
        assert verifier.run(task.verifier, second)["score"] == 0
        assert Path(task.runtime.config["archive"]).is_file()
    finally:
        for instance in instances:
            assert runtime.close(instance)[0]


def test_uploaded_files_are_writable_by_runtime_user(tmp_path):
    runtime = Runtime(tmp_path / "assets")
    instance = runtime.start(
        RuntimeRef(
            "docker",
            os.environ["ENVWEAVER_TEST_DOCKER"],
            {
                "command": ["sleep", "infinity"],
                "user": "1000:1000",
            },
        )
    )
    try:
        source = tmp_path / "input.txt"
        source.write_text("input")
        runtime.upload(instance, source, "/tmp/input.txt")
        assert runtime.exec(instance, "printf edited > /tmp/input.txt")[0] == 0
        assert runtime.exec(instance, "cat /tmp/input.txt")[1] == "edited"
    finally:
        assert runtime.close(instance)[0]
