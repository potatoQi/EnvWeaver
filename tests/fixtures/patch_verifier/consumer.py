"""Downstream consumer using only stdlib and native Docker CLI."""

import json
import subprocess
import sys
import uuid
from pathlib import Path

package = Path(sys.argv[1]).resolve()
manifest = json.loads((package / "task.json").read_text())
host = sys.argv[2]
connection = ["--host", host] if host else ["--context", "default"]
containers = []


def docker(*args):
    result = subprocess.run(
        ["docker", *connection, *args],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def start(ref):
    config = ref["config"]
    args = ["run", "-d", "--pull=never", "--network", "none"]
    args += config.get("args", [])
    args += [ref["image"], *config["command"]]
    cid = docker(*args)
    containers.append(cid)
    return cid


try:
    candidate = start(manifest["task_runtime_ref"])
    for expression, score in [("a - b + 0", 0), ("a + b", 1)]:
        checker = start(manifest["verifier_runtime_ref"])
        # A deterministic solution step; no model service is needed for this transport test.
        docker(
            "exec",
            candidate,
            "python3",
            "-c",
            "from pathlib import Path; Path('/tmp/repo/app.py').write_text("
            + repr(f"def add(a, b):\n    return {expression}\n")
            + ")",
        )
        patch = docker("exec", "-w", "/tmp/repo", candidate, "git", "diff") + "\n"
        patch_file = Path.cwd() / "submission.patch"
        patch_file.write_text(patch)
        directory = "/tmp/evaluation-" + uuid.uuid4().hex
        entry = package / manifest["verifier"]["entry"]
        docker("cp", str(entry.parent), checker + ":" + directory)
        docker("cp", str(patch_file), checker + ":" + directory + "/submission.patch")
        supervisor = """import subprocess,sys
result=subprocess.run([sys.executable,'run.py'],capture_output=True,text=True,timeout=float(sys.argv[1]))
print(result.stdout,end=''); print(result.stderr,end='',file=sys.stderr); sys.exit(result.returncode)
"""
        output = docker(
            "exec",
            "-w",
            directory,
            "-e",
            "CHECKER_TOKEN=per-run-value",
            checker,
            "python3",
            "-c",
            supervisor,
            str(manifest["verifier"]["timeout"]),
        )
        assert json.loads(output) == {"score": score}
        docker("rm", "--force", checker)
        containers.remove(checker)
    print("Native patch consumer: setup, solution, git diff, git apply, scores 0/1; no EnvWeaver")
finally:
    for cid in containers:
        docker("rm", "--force", cid)
