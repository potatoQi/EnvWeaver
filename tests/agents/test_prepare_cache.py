import base64
import hashlib
import io
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from envweaver import Instance
from envweaver.agents.adapters._cli import CliAgent
from envweaver.agents.execution import AgentExecution
from tests.agents.test_cli_agents import LocalTransport


def test_real_pip_resolves_again_but_reuses_only_selected_artifact(tmp_path, monkeypatch):
    pytest_cache = tmp_path / "controller-cache"
    monkeypatch.setenv("ENVWEAVER_CACHE_DIR", str(pytest_cache))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.02)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as wheel:
        wheel.writestr("ew_cache_fixture.py", 'value = "original"\n')
        wheel.writestr(
            "ew_cache_fixture-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: ew-cache-fixture\nVersion: 1.0\n",
        )
        wheel.writestr(
            "ew_cache_fixture-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        wheel.writestr("ew_cache_fixture-1.0.dist-info/RECORD", "")
    content = buffer.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    filename = "ew_cache_fixture-1.0-py3-none-any.whl"

    class Handler(BaseHTTPRequestHandler):
        wheels = 0
        indexes = 0

        def do_GET(self):
            if self.path.endswith(".whl"):
                type(self).wheels += 1
                body, kind = content, "application/octet-stream"
            else:
                type(self).indexes += 1
                body = f'<a href="/{filename}#sha256={digest}">{filename}</a>'.encode()
                kind = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    class Probe(CliAgent):
        dependency = "pip"
        executable = "ew-cache-fixture"

        def _version_command(self):
            return [
                sys.executable,
                "-c",
                "from importlib.metadata import version; print(version('ew-cache-fixture'))",
            ]

        def _install_command(self, version):
            assert not Path(self._remote, "_npm_download.cjs").exists()
            pip = [self._remote + "/_pip_install.py"] if self._downloads else ["-m", "pip"]
            return [
                sys.executable,
                *pip,
                "install",
                "--target",
                self._remote + "/packages",
                "--index-url",
                f"http://127.0.0.1:{server.server_port}/simple",
                "--disable-pip-version-check",
                "--no-deps",
                "ew-cache-fixture==1.0",
            ]

    snapshots = []
    try:
        for number, enabled in enumerate((True, True, False)):
            agent = Probe(cache=enabled, env={"PIP_NO_CACHE_DIR": "1", "NO_PROXY": "127.0.0.1"})
            execution = AgentExecution(agent)
            try:
                execution.prepare(
                    runtime=LocalTransport(),
                    instance=Instance("local", "fake", None, {"python": sys.executable}),
                    output_dir=tmp_path / f"run-{number}",
                    cwd=str(tmp_path),
                )
                root = Path(agent._remote)
                assert not (root / "downloads").exists()
                assert not (root / "_pip_install.py").exists()
                files = {
                    str(p.relative_to(root / "packages")): p.read_bytes()
                    for p in (root / "packages").rglob("*")
                    if p.is_file() and p.suffix != ".pyc" and p.name != "RECORD"
                }
                snapshots.append(files)
                (root / "packages/ew_cache_fixture.py").write_text('value = "previous task patch"')
                if number < 2:
                    assert Handler.wheels == 1
            finally:
                execution.stop()
                if agent._remote:
                    shutil.rmtree(agent._remote, ignore_errors=True)
        assert snapshots[0] == snapshots[1] == snapshots[2]
        assert Handler.wheels == 2
        assert Handler.indexes >= 3
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_real_npm_keeps_cache_private_and_reuses_tarball(tmp_path, monkeypatch):
    import pytest

    if not shutil.which("npm"):
        pytest.skip("npm is not installed")
    if (
        int(
            subprocess.check_output(["node", "--version"], text=True)
            .strip()
            .lstrip("v")
            .split(".")[0]
        )
        < 22
    ):
        pytest.skip("requires Node 22+")
    monkeypatch.setenv("ENVWEAVER_CACHE_DIR", str(tmp_path / "controller-cache"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr("envweaver.agents.adapters._cli._POLL_INTERVAL", 0.02)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in {
            "package/package.json": b'{"name":"ew-cache-npm","version":"1.0.0","main":"index.js"}',
            "package/index.js": b'module.exports = "original";\n',
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    content = buffer.getvalue()
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(content).digest()).decode()

    class Handler(BaseHTTPRequestHandler):
        tarballs = 0

        def do_GET(self):
            import json

            if self.path.endswith(".tgz"):
                type(self).tarballs += 1
                body = content
                kind = "application/octet-stream"
            else:
                package = {
                    "name": "ew-cache-npm",
                    "version": "1.0.0",
                    "dist": {
                        "tarball": f"http://127.0.0.1:{server.server_port}/package.tgz",
                        "integrity": integrity,
                    },
                }
                body = json.dumps(
                    {
                        "name": "ew-cache-npm",
                        "dist-tags": {"latest": "1.0.0"},
                        "versions": {"1.0.0": package},
                    }
                ).encode()
                kind = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    class Probe(CliAgent):
        dependency = "node"
        executable = "ew-cache-npm"
        package = "ew-cache-npm"

        def _version_command(self):
            return [
                "node",
                "-e",
                "console.log(require("
                + repr(self._remote + "/npm/node_modules/ew-cache-npm/package.json")
                + ").version)",
            ]

        def _ensure_dependencies(self):
            assert not Path(self._remote, "_pip_install.py").exists()
            self._dependency_env["npm_config_cache"] = self._remote + "/npm-cache"

    try:
        for number in range(2):
            agent = Probe(
                env={
                    "npm_config_registry": f"http://127.0.0.1:{server.server_port}",
                    "npm_config_audit": "false",
                    "npm_config_fund": "false",
                }
            )
            execution = AgentExecution(agent)
            try:
                execution.prepare(
                    runtime=LocalTransport(),
                    instance=Instance("local", "fake", None, {"python": sys.executable}),
                    output_dir=tmp_path / str(number),
                    cwd=str(tmp_path),
                )
                root = Path(agent._remote)
                assert (
                    root / "npm/node_modules/ew-cache-npm/index.js"
                ).read_bytes() == b'module.exports = "original";\n'
                assert not (root / "downloads").exists()
                assert not (root / "_npm_download.cjs").exists()
                assert Handler.tarballs == 1
            finally:
                execution.stop()
                if agent._remote:
                    shutil.rmtree(agent._remote, ignore_errors=True)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
