#!/usr/bin/env python3
"""构建、校验并按需发布 EnvWeaver。

需要 Python 3.11+ 和 uv。在仓库根目录运行：

    python3 scripts/publish.py

默认只构建 wheel 和源码包，并用 twine check --strict 检查包元数据与 README。
产物保存在 dist/ 下本次构建的独立目录；不会上传，也不需要密钥。
首次运行会下载构建和校验工具。

版本从 pyproject.toml 的 project.version 读取，修改后运行 uv lock 同步锁文件。
Git 发布标签为 v<版本号>；Task 格式版本仍为 1。
向 PyPI 发布前，先按 AGENTS.md 发布对应版本的文档并确认可访问。

上传到 TestPyPI（默认目标）：

    python3 scripts/publish.py --repository testpypi --publish

上传到 PyPI：

    python3 scripts/publish.py --repository pypi --publish

密钥分别从环境变量 TESTPYPI_TOKEN 和 PYPI_TOKEN 读取。
已配置在 ~/.bashrc 时，在当前 Bash 终端执行 source ~/.bashrc 加载。
上传使用 uv publish，密钥通过子进程环境传递，不写入命令参数或构建产物。

只有显式添加 --publish 才会上传。每次重新构建和校验，只上传本次生成的两个文件；
不创建 Git 标签，也不推送代码。构建、校验或上传失败时返回非零退出码。
两个文件依次上传，失败前已上传的文件不会自动撤回。
"""

import argparse
import os
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORIES = {
    "pypi": ("https://upload.pypi.org/legacy/", "PYPI_TOKEN"),
    "testpypi": ("https://test.pypi.org/legacy/", "TESTPYPI_TOKEN"),
}


def main() -> None:
    """Build fresh archives, validate metadata, and optionally publish them."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repository", choices=REPOSITORIES, default="testpypi")
    parser.add_argument("--publish", action="store_true", help="upload after successful checks")
    args = parser.parse_args()
    if not shutil.which("uv"):
        parser.error("uv is required")

    url, token_name = REPOSITORIES[args.repository]
    token = os.environ.get(token_name, "").strip()
    if args.publish and not token:
        parser.error(f"export {token_name} before publishing")

    # Build tools do not need publishing credentials. Ignore inherited upload overrides.
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYPI_TOKEN", "TESTPYPI_TOKEN"}
        and not key.startswith("UV_PUBLISH_")
    }
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    name = project["name"].replace("-", "_")
    version = project["version"]
    (ROOT / "dist").mkdir(exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix=f"{name}-{version}-", dir=ROOT / "dist"))
    artifacts = [
        output / f"{name}-{version}.tar.gz",
        output / f"{name}-{version}-py3-none-any.whl",
    ]
    print(f"Building {name} {version} (tag v{version}) into {output}", flush=True)
    # uv builds the wheel from the sdist, checking that the source archive is usable.
    subprocess.run(
        ["uv", "build", "--no-sources", "--out-dir", str(output)],
        cwd=ROOT, env=env, check=True,
    )
    if not all(path.is_file() for path in artifacts):
        raise SystemExit("Build did not produce the expected wheel and source archive")
    subprocess.run(
        ["uv", "tool", "run", "--from", "twine>=6,<7", "twine", "check", "--strict",
         *map(str, artifacts)],
        cwd=ROOT, env=env, check=True,
    )
    if not args.publish:
        print(f"Checks passed. No upload performed. Artifacts: {output}")
        return

    print(f"Publishing {name} {version} to {args.repository}", flush=True)
    subprocess.run(
        ["uv", "publish", "--no-config", "--trusted-publishing", "never",
         "--publish-url", url, *map(str, artifacts)],
        cwd=ROOT, env={**env, "UV_PUBLISH_TOKEN": token}, check=True,
    )


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from None
