import hashlib
import io
import subprocess
import tarfile
from unittest.mock import Mock

import pytest

from envweaver.agents.adapters import _dependencies as deps


def test_existing_node_and_npm_require_no_install(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "node_ready", lambda: True)
    download = Mock(side_effect=AssertionError("must not download"))
    monkeypatch.setattr(deps, "download", download)
    deps.install_node(tmp_path)
    download.assert_not_called()


@pytest.mark.parametrize(
    "version,npm_code,expected",
    [
        ("v22.19.0", 0, True),
        ("v24.0.0", 0, True),
        ("v22.18.0", 0, False),
        ("v20.19.0", 0, False),
        ("v22.19.0", 1, False),
        ("invalid", 0, False),
    ],
)
def test_node_and_npm_readiness(monkeypatch, version, npm_code, expected):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, npm_code if command[0] == "npm" else 0, stdout=version
        )

    monkeypatch.setattr(deps.subprocess, "run", run)
    assert deps.node_ready() is expected


def node_archive(path, member_name="node-v22.19.0-linux-x64/bin/node"):
    with tarfile.open(path, "w:gz") as bundle:
        member = tarfile.TarInfo(member_name)
        member.size = 4
        member.mode = 0o755
        bundle.addfile(member, io.BytesIO(b"node"))


@pytest.mark.parametrize("bad_checksum,unsafe_path", [(False, False), (True, False), (False, True)])
def test_node_download_verifies_checksum_and_paths(
    tmp_path, monkeypatch, bad_checksum, unsafe_path
):
    archive = tmp_path / "fixture.tar.gz"
    node_archive(archive, "../escape" if unsafe_path else "node-v22.19.0-linux-x64/bin/node")
    content = archive.read_bytes()
    filename = "node-v22.19.0-linux-x64.tar.gz"
    checksum = "0" * 64 if bad_checksum else hashlib.sha256(content).hexdigest()

    def download(url, target, expected_checksum=None):
        if url.endswith("SHASUMS256.txt"):
            target.write_text(f"{checksum}  {filename}\n")
        else:
            assert url.endswith(filename)
            target.write_bytes(content)

    ready = iter([False, True])
    monkeypatch.setattr(deps, "node_ready", lambda: next(ready))
    monkeypatch.setattr(deps.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(deps, "download", download)
    root = tmp_path / "dependencies"
    root.mkdir()
    if bad_checksum or unsafe_path:
        with pytest.raises(RuntimeError, match="checksum|archive path"):
            deps.install_node(root)
        assert not (tmp_path / "escape").exists()
    else:
        deps.install_node(root)
        assert (root / "node/bin/node").read_bytes() == b"node"
        assert not (root / filename).exists()


def test_missing_pip_installs_into_private_directory(tmp_path, monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 1 if len(commands) == 1 else 0)

    monkeypatch.setattr(deps.subprocess, "run", run)
    monkeypatch.setattr(deps, "download", lambda url, target: target.write_text("installer"))
    deps.install_pip(tmp_path)
    assert "--target" in commands[1]
    assert commands[1][commands[1].index("--target") + 1] == str(tmp_path / "pip")
    assert commands[-1][-3:] == ["-m", "pip", "--version"]


def test_existing_pip_skips_download(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deps.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0)
    )
    download = Mock(side_effect=AssertionError("must not download"))
    monkeypatch.setattr(deps, "download", download)
    deps.install_pip(tmp_path)
    download.assert_not_called()


def test_node_relative_symlinks_and_hardlinks(tmp_path, monkeypatch):
    archive = tmp_path / "node.tar"
    with tarfile.open(archive, "w") as bundle:
        file = tarfile.TarInfo("node/lib/npm.js")
        file.size, file.mode = 3, 0o4755
        bundle.addfile(file, io.BytesIO(b"npm"))
        for name, kind, target in [
            ("node/bin/npm", tarfile.SYMTYPE, "../lib/npm.js"),
            ("node/bin/npx", tarfile.LNKTYPE, "node/lib/npm.js"),
        ]:
            link = tarfile.TarInfo(name)
            link.type, link.linkname, link.mode = kind, target, 0o755
            bundle.addfile(link)
    monkeypatch.setattr(
        tarfile, "data_filter", Mock(side_effect=AssertionError("must not use version-dependent checks")),
    )
    root = tmp_path / "extracted"
    with tarfile.open(archive) as bundle:
        deps.extract_node(bundle, root)
    assert (root / "node/bin/npm").read_bytes() == b"npm"
    assert (root / "node/bin/npx").read_bytes() == b"npm"
    assert (root / "node/lib/npm.js").stat().st_mode & 0o7777 == 0o755


@pytest.mark.parametrize("kind,target", [
    (tarfile.SYMTYPE, "../../../escape"),
    (tarfile.LNKTYPE, "../escape"),
    (tarfile.SYMTYPE, "/etc/passwd"),
    (tarfile.FIFOTYPE, ""),
])
def test_node_rejects_unsafe_links_and_special_files(tmp_path, kind, target):
    archive = tmp_path / "node.tar"
    with tarfile.open(archive, "w") as bundle:
        link = tarfile.TarInfo("node/bin/npm")
        link.type, link.linkname = kind, target
        bundle.addfile(link)
    with tarfile.open(archive) as bundle, pytest.raises(RuntimeError, match="archive"):
        deps.extract_node(bundle, tmp_path / "extracted")


def test_node_checks_paths_after_extracting_symlinks(tmp_path):
    archive = tmp_path / "node.tar"
    with tarfile.open(archive, "w") as bundle:
        link = tarfile.TarInfo("node/alias")
        link.type, link.linkname = tarfile.SYMTYPE, ".."
        bundle.addfile(link)
        file = tarfile.TarInfo("node/alias/../escape")
        file.size = 4
        bundle.addfile(file, io.BytesIO(b"oops"))
    with tarfile.open(archive) as bundle, pytest.raises(RuntimeError, match="archive path"):
        deps.extract_node(bundle, tmp_path / "extracted")
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("uid,privilege", [(0, []), (1000, ["sudo", "-n"])])
def test_musl_uses_apk_without_downloading_glibc_node(tmp_path, monkeypatch, uid, privilege):
    ready = iter([False, True])
    monkeypatch.setattr(deps, "node_ready", lambda: next(ready))
    monkeypatch.setattr(deps.Path, "glob", lambda self, pattern: iter([tmp_path / "ld-musl.so"]))
    monkeypatch.setattr(deps.shutil, "which", lambda executable: "/sbin/apk")
    monkeypatch.setattr(deps.os, "geteuid", lambda: uid)
    run = Mock()
    monkeypatch.setattr(deps.subprocess, "run", run)
    monkeypatch.setattr(deps, "download", Mock(side_effect=AssertionError("must not download")))
    deps.install_node(tmp_path)
    run.assert_called_once_with(
        privilege + ["apk", "add", "--no-cache", "nodejs", "npm"], check=True
    )


def test_unsupported_architecture_fails_before_download(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "node_ready", lambda: False)
    monkeypatch.setattr(deps.Path, "glob", lambda self, pattern: iter([]))
    monkeypatch.setattr(deps.platform, "machine", lambda: "unknown-architecture")
    monkeypatch.setattr(deps, "download", Mock(side_effect=AssertionError("must not download")))
    with pytest.raises(RuntimeError, match="does not support"):
        deps.install_node(tmp_path)
