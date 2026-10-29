"""Fresh uv resolution, with only its selected wheel downloads shared by the controller."""

import os
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path


def install_packages():
    uv = [sys.executable, "-m", "uv"]
    with tempfile.TemporaryDirectory(prefix="envweaver-uv-") as folder:
        folder = Path(folder)
        requirements = folder / "requirements.in"
        requirements.write_text("harbor==0.25.0\n")
        lock = folder / "pylock.toml"
        subprocess.run(
            [
                *uv,
                "pip",
                "compile",
                str(requirements),
                "--python",
                sys.executable,
                "--format",
                "pylock.toml",
                "--output-file",
                str(lock),
                "--no-header",
                "--quiet",
            ],
            check=True,
        )
        if os.environ.get("ENVWEAVER_DOWNLOAD_REQUESTS"):
            from _uv_install import cache_wheels

            cache_wheels(lock)
        subprocess.run([*uv, "pip", "sync", str(lock), "--python", sys.executable], check=True)


def main():
    root = Path(sys.argv[1])
    if len(sys.argv) > 2:
        install_packages()
        return
    pip = [sys.executable]
    if os.environ.get("ENVWEAVER_DOWNLOAD_REQUESTS"):
        pip += [str(root / "_pip_install.py")]
    else:
        pip += ["-m", "pip"]
    subprocess.run(
        [*pip, "install", "--no-compile", "--target", str(root / "packages"), "uv"], check=True,
    )
    uv = [sys.executable, "-m", "uv"]
    downloads = nullcontext({})
    if os.environ.get("ENVWEAVER_DOWNLOAD_REQUESTS"):
        from _uv_install import python_downloads

        downloads = python_downloads(uv, "3.12")
    with downloads as environment:
        subprocess.run([*uv, "venv", "--python", "3.12", str(root / "venv")],
                       env={**os.environ, **environment}, check=True)
    subprocess.run(
        [
            str(root / "venv/bin/python"),
            __file__,
            str(root),
            "packages",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
