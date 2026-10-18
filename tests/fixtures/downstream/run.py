"""Task-owned verifier, executed outside the task container."""

import argparse
import json
import os
import shlex
from pathlib import Path

from envweaver import Runtime

if os.environ.get("CHECKER_TOKEN") != "test-value":
    raise RuntimeError("Missing checker configuration")

parser = argparse.ArgumentParser()
parser.add_argument("--instance", required=True)
runtime = Runtime()
instance = runtime.connect(path=parser.parse_args().instance)
# Hidden tests are delivered only when verification begins.
runtime.upload(instance=instance, source="cases.json", target="/tmp/csv task/hidden-cases.json")
runtime.download(
    instance=instance, source="/tmp/csv task/hidden-cases.json", target="received.json"
)
assert Path("received.json").read_bytes() == Path("cases.json").read_bytes()
cases = json.loads(Path("cases.json").read_text(encoding="utf-8"))
passed = 0
for case in cases:
    path = case["input"]
    code, _, error = runtime.exec(instance=instance, command="test -f " + shlex.quote(path))
    if code:
        raise RuntimeError("Input material is missing: " + path + ": " + error)
    code, output, error = runtime.exec(
        instance=instance,
        command="python3 parser.py " + shlex.quote(path),
        cwd="/tmp/csv task",
        timeout=10,
    )
    if code:
        continue  # The submitted program failed on this test case.
    try:
        passed += json.loads(output) == case["expected"]
    except ValueError:
        pass
print(json.dumps({"score": passed / len(cases), "passed": passed, "total": len(cases)}))
