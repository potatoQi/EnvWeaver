"""Shared scripts uploaded into Agent sandboxes."""

from pathlib import Path

SANDBOX_SCRIPTS = Path(__file__).parent


def upload_model_capture(runtime, instance, directory):
    """Install the LiteLLM recorder and its redaction helper beside the runner."""
    for source, name in (
        (SANDBOX_SCRIPTS / "_model_capture.py", "model_capture.py"),
        (SANDBOX_SCRIPTS.parents[1] / "_redaction.py", "_redaction.py"),
    ):
        runtime.upload(instance=instance, source=source, target=f"{directory}/{name}")
