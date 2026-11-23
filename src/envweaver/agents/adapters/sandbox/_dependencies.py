"""Dependency installer executed by the Agent's supervised sandbox Python process."""

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

NODE_MAJOR = 22


def download(url, target, checksum=None):
    if checksum is not None and os.environ.get("ENVWEAVER_DOWNLOAD_REQUESTS"):
        from _download_client import download as cached_download

        if cached_download(url, checksum, target):
            return
    print(f"Downloading {url}", flush=True)
    with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as stream:
        shutil.copyfileobj(response, stream)


def node_ready():
    try:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=30)
        version = tuple(int(part) for part in result.stdout.strip().lstrip("v").split("."))
        npm = subprocess.run(["npm", "--version"], capture_output=True, timeout=30)
        supported = (
            version >= (22, 19, 0)
            if NODE_MAJOR == 22
            else (24, 16, 0) <= version < (25,) or version >= (26, 1, 0)
        )
        return result.returncode == npm.returncode == 0 and supported
    except (OSError, ValueError):
        return False


def extract_node(bundle, root):
    """Extract members in order, checking links against the current filesystem."""
    destination = root.resolve()
    for member in bundle:
        target = (destination / member.name).resolve()
        if (
            Path(member.name).is_absolute()
            or not target.is_relative_to(destination)
            or not (member.isfile() or member.isdir() or member.issym() or member.islnk())
        ):
            raise RuntimeError("Invalid Node archive path")
        if member.issym() or member.islnk():
            base = target.parent if member.issym() else destination
            link = Path(member.linkname)
            if link.is_absolute() or not (base / link).resolve().is_relative_to(destination):
                raise RuntimeError("Invalid Node archive link")
        member.mode &= 0o755
        # Some sandbox Python builds resolve relative symlinks from the archive
        # root in data_filter. Use the checks above consistently on all versions.
        options = {"filter": "fully_trusted"} if hasattr(tarfile, "data_filter") else {}
        bundle.extract(member, path=root, **options)


def install_node(root):
    if node_ready():
        return
    if list(Path("/lib").glob("ld-musl-*.so*")):
        if not shutil.which("apk"):
            raise RuntimeError("Automatic Node installation on musl requires apk")
        privilege = [] if os.geteuid() == 0 else ["sudo", "-n"]
        subprocess.run(privilege + ["apk", "add", "--no-cache", "nodejs", "npm"], check=True)
    else:
        architecture = {"x86_64": "x64", "aarch64": "arm64"}.get(platform.machine())
        if architecture is None:
            raise RuntimeError(f"Automatic Node installation does not support {platform.machine()}")
        base = f"https://nodejs.org/dist/latest-v{NODE_MAJOR}.x/"
        checksums = root / "SHASUMS256.txt"
        download(base + checksums.name, checksums)
        suffix = f"-linux-{architecture}.tar.gz"
        matches = [
            line.split() for line in checksums.read_text().splitlines() if line.endswith(suffix)
        ]
        if len(matches) != 1:
            raise RuntimeError(f"Cannot identify a Node {NODE_MAJOR} Linux distribution")
        checksum, filename = matches[0]
        if Path(filename).name != filename:
            raise RuntimeError("Invalid Node distribution filename")
        archive = root / filename
        download(base + filename, archive, "sha256:" + checksum)
        with archive.open("rb") as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != checksum:
            raise RuntimeError("Node archive checksum mismatch")
        with tarfile.open(archive) as bundle:
            extract_node(bundle, root)
        (root / filename.removesuffix(".tar.gz")).rename(root / "node")
        archive.unlink()
    if not node_ready():
        raise RuntimeError(
            f"Automatic installation did not provide a supported Node.js {NODE_MAJOR} and npm"
        )


def install_pip(root):
    result = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True)
    if result.returncode == 0:
        return
    installer = root / "get-pip.py"
    download("https://bootstrap.pypa.io/get-pip.py", installer)
    subprocess.run(
        [
            sys.executable,
            str(installer),
            "--target",
            str(root / "pip"),
            "--no-warn-script-location",
            "--disable-pip-version-check",
        ],
        check=True,
    )
    subprocess.run([sys.executable, "-m", "pip", "--version"], check=True)


if __name__ == "__main__":
    if len(sys.argv) > 3:
        NODE_MAJOR = int(sys.argv[3])
    root = Path(sys.argv[2])
    root.mkdir(parents=True, exist_ok=True)
    {"node": install_node, "pip": install_pip}[sys.argv[1]](root)
