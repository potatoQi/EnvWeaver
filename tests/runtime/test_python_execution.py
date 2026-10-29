"""Python source keeps its text, exit status and timeout across built-in transports."""

import subprocess
import sys

import pytest

from envweaver import Instance, Runtime
from envweaver._util import run_python
from envweaver.runtime.docker import DockerBackend


@pytest.fixture(params=["docker", "qemu"])
def python_target(request, ssh_instance, monkeypatch):
    if request.param == "qemu":
        return ssh_instance

    def docker(self, *args, **kwargs):
        assert args[:2] == ("exec", "container")
        # Execute the same stdlib payload sent by docker exec, without a Docker daemon.
        result = subprocess.run(
            [args[2], *args[3:]], capture_output=True, text=True, timeout=kwargs["timeout"]
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    monkeypatch.setattr(DockerBackend, "_docker", docker)
    return Instance("container", "docker", data={"python": sys.executable, "host_ssh": {}})


def test_python_source_and_nonzero_status(python_target):
    literal = "中文 ' \" $HOME $(echo not-a-shell) `echo literal`\nnext line"
    code, out, err = run_python(
        Runtime(), instance=python_target,
        source=f"import sys; print({literal!r}); print('error', file=sys.stderr); sys.exit(7)",
    )
    assert (code, out, err) == (7, literal + "\n", "error\n")


def test_python_timeout(python_target):
    with pytest.raises(TimeoutError):
        run_python(
            Runtime(), instance=python_target, source="import time; time.sleep(30)", timeout=0.1
        )


def test_configured_python_with_spaces(python_target, tmp_path):
    executable = tmp_path / "Python with spaces"
    executable.symlink_to(sys.executable)
    python_target.data["python"] = str(executable)
    code, out, err = run_python(Runtime(), python_target, "import sys; print(sys.executable)")
    assert (code, out.strip(), err) == (0, str(executable), "")
