"""Bootstrap Python before Runtime context and setup, using a bare Docker image."""

import os
import subprocess
import uuid

import pytest

from envweaver import Runtime
from envweaver.errors import RuntimeError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ENVWEAVER_TEST_PYTHON_BOOTSTRAP_IMAGE"),
        reason="requires a local Docker image without Python",
    ),
]


def test_install_python_before_context_and_setup(tmp_path):
    runtime = Runtime()
    proxy = os.environ.get("ENVWEAVER_TEST_PROXY")
    env = {"http_proxy": proxy, "https_proxy": proxy} if proxy else {}
    ref = runtime.create(
        backend="docker",
        image=os.environ["ENVWEAVER_TEST_PYTHON_BOOTSTRAP_IMAGE"],
        config={"command": ["sleep", "infinity"], "env": env},
    )
    source = tmp_path / "input.txt"
    source.write_text("input")
    instance = runtime.start(
        ref=ref,
        context=[{"source": source, "target": "/inputs/input.txt"}],
        setup={
            "command": [
                "python3", "-c",
                "from pathlib import Path; "
                "assert Path('/inputs/input.txt').read_text() == 'input'; "
                "Path('/inputs/ready.txt').write_text('ready')",
            ],
            "timeout": 15,
        },
    )
    try:
        assert runtime.exec(instance=instance, command="cat /inputs/ready.txt") == (0, "ready", "")
        code, _, error = runtime.exec(
            instance=instance,
            command=["python3", "-c", "import ssl; assert ssl.create_default_context().get_ca_certs()"],
        )
        assert code == 0, error
        runtime.download(instance=instance, source="/inputs/ready.txt", target=tmp_path / "ready.txt")
        assert (tmp_path / "ready.txt").read_text() == "ready"
    finally:
        runtime.close(instance=instance)


@pytest.mark.parametrize("overrides", [{"user": "65534:65534"}, {"python": "/missing/python"}])
def test_bootstrap_failure_removes_container(overrides):
    runtime = Runtime()
    label = "envweaver.bootstrap-test=" + uuid.uuid4().hex
    ref = runtime.create(
        backend="docker",
        image=os.environ["ENVWEAVER_TEST_PYTHON_BOOTSTRAP_IMAGE"],
        config={"command": ["sleep", "infinity"], "args": ["--label", label], **overrides},
    )
    with pytest.raises(RuntimeError, match="Python|python"):
        runtime.start(ref=ref)
    assert not subprocess.check_output([
        "docker", "--context", "default", "ps", "-aq", "--filter", "label=" + label,
    ]).strip()
