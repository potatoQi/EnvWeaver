import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

from ._util import run_process
from .errors import VerifierError
from .runtime import Runtime


def run(entry_path, instance, *, timeout=300):
    entry = Path(entry_path).resolve(strict=True)
    if entry.name != "run.py":
        raise ValueError("Verifier entry must be named run.py")
    with tempfile.TemporaryDirectory(prefix="envweaver-verifier-") as directory:
        descriptor = Path(directory) / "instance.json"
        Runtime.describe(instance, descriptor)
        try:
            result = run_process(
                [sys.executable, str(entry), "--instance", str(descriptor)],
                cwd=entry.parent,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise VerifierError(f"Verifier timed out after {timeout}s") from error
    diagnostics = result.stderr.decode("utf-8", errors="replace")
    if result.returncode:
        raise VerifierError(f"Verifier exited with {result.returncode}: {diagnostics}")
    try:
        output = json.loads(result.stdout)
        score = output["score"]
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not 0 <= score <= 1
        ):
            raise ValueError("score must be a finite number in [0, 1]")
        # Validate auxiliary fields too: NaN/Infinity are not valid JSON.
        json.dumps(output, allow_nan=False)
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        raise VerifierError(f"Invalid verifier result: {error}; stderr: {diagnostics}") from error
    return output
