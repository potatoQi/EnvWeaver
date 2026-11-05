"""Release controls tested without contacting a package registry."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "publish", Path(__file__).resolve().parents[1] / "scripts" / "publish.py"
)
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)


@pytest.fixture
def release(monkeypatch, tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="envweaver"\nversion="0.0.1"\n')
    monkeypatch.setattr(publish, "ROOT", tmp_path)
    monkeypatch.setattr(publish.shutil, "which", lambda _: "/usr/bin/uv")
    monkeypatch.setenv("PYPI_TOKEN", "production-test-token")
    monkeypatch.setenv("TESTPYPI_TOKEN", "sandbox-test-token")
    monkeypatch.setenv("UV_PUBLISH_URL", "https://wrong.invalid/")
    monkeypatch.setenv("UV_PUBLISH_TOKEN", "wrong-test-token")
    monkeypatch.setenv("UV_PUBLISH_INDEX", "wrong-index")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[1] == "build":
            out = Path(command[command.index("--out-dir") + 1])
            (out / "envweaver-0.0.1.tar.gz").touch()
            (out / "envweaver-0.0.1-py3-none-any.whl").touch()

    monkeypatch.setattr(publish.subprocess, "run", run)
    return calls


def test_default_only_builds_and_checks(monkeypatch, release):
    monkeypatch.setattr(sys, "argv", ["publish.py"])
    publish.main()
    assert [cmd[1] for cmd, _ in release] == ["build", "tool"]
    for _, kwargs in release:
        assert not any("TOKEN" in key for key in kwargs["env"] if "PYPI" in key)
        assert not any(key.startswith("UV_PUBLISH_") for key in kwargs["env"])


@pytest.mark.parametrize(
    ("repository", "token"), [("pypi", "production-test-token"), ("testpypi", "sandbox-test-token")]
)
def test_publish_selects_credentials_and_only_fresh_artifacts(monkeypatch, release, repository, token):
    stale = publish.ROOT / "dist" / "old.whl"
    stale.parent.mkdir()
    stale.touch()
    monkeypatch.setattr(sys, "argv", ["publish.py", "--repository", repository, "--publish"])
    publish.main()
    command, kwargs = release[-1]
    assert command[1] == "publish"
    assert command[command.index("--publish-url") + 1] == publish.REPOSITORIES[repository][0]
    assert kwargs["env"]["UV_PUBLISH_TOKEN"] == token
    assert not {"PYPI_TOKEN", "TESTPYPI_TOKEN", "UV_PUBLISH_INDEX", "UV_PUBLISH_URL"} & kwargs["env"].keys()
    assert token not in " ".join(command)
    assert str(stale) not in command
    assert command[-2:] == release[1][0][-2:]


def test_missing_selected_token_stops_before_build(monkeypatch, release):
    monkeypatch.delenv("TESTPYPI_TOKEN")
    monkeypatch.setattr(sys, "argv", ["publish.py", "--publish"])
    with pytest.raises(SystemExit):
        publish.main()
    assert release == []


@pytest.mark.parametrize("failed_step", ["build", "tool"])
def test_failed_check_never_uploads(monkeypatch, release, failed_step):
    original = publish.subprocess.run

    def run(command, **kwargs):
        if command[1] == failed_step:
            raise subprocess.CalledProcessError(1, command)
        return original(command, **kwargs)

    monkeypatch.setattr(publish.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["publish.py", "--publish"])
    with pytest.raises(subprocess.CalledProcessError):
        publish.main()
    assert all(command[1] != "publish" for command, _ in release)
