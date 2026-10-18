"""Evaluate a submission entirely inside the verification sandbox."""

import json
import os
import subprocess
import sys
from pathlib import Path

assert len(sys.argv) == 1  # No instance descriptor or EnvWeaver dependency.
assert os.environ["CHECKER_TOKEN"] == "per-run-value"
marker = Path("/tmp/envweaver-evaluation-marker")
assert not marker.exists()
marker.write_text("this sandbox has been evaluated")
patch = Path("submission.patch").resolve()
subprocess.run(["git", "apply", str(patch)], cwd="repo", check=True, capture_output=True)
result = subprocess.run(
    [sys.executable, "-m", "unittest", "discover", "-s", "."],
    cwd="repo",
    capture_output=True,
    text=True,
)
if result.returncode not in (0, 1):
    raise RuntimeError(result.stderr)
print(json.dumps({"score": float(result.returncode == 0)}))
