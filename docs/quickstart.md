# 快速开始

## 1. 安装

Python 3.11+。在你运行 Pipeline 的环境里执行：

```bash
python -m pip install -e /path/to/EnvWeaver
```

这是开发期间的编辑安装：Python 源码改动后，重新启动 Pipeline 即可加载最新代码。
修改依赖或包配置后需重新安装。普通使用也可以安装构建后的 wheel。

EnvWeaver 不规定使用 uv、conda 还是 Docker 部署。下面以可访问本机 Docker 的 Linux 环境为例。

## 2. 准备环境并运行

安装包不会自动安装 Docker、QEMU、pi 或桌面软件。先确认 Docker CLI 和 daemon 可用，取得镜像：

```bash
docker pull python:3.11-slim-bookworm
```

在自己的项目中创建 `pipeline.py`：

```python
from envweaver import Runtime, RuntimeRef

runtime = Runtime("./storage/runtimes")
instance = runtime.start(RuntimeRef(
    "docker", "python:3.11-slim-bookworm",
    {"command": ["sleep", "infinity"]},
))
try:
    code, stdout, stderr = runtime.exec(instance, "python3 --version")
    if code:
        raise RuntimeError(stderr)
    print(stdout)
finally:
    success, error = runtime.close(instance)
    if not success:
        raise RuntimeError(error)
```

```bash
python pipeline.py
```

此时 Python 在 Pipeline 侧调用 EnvWeaver；`python3 --version` 在目标容器内执行。

## 3. 编写生产 Pipeline

向编程 Agent 提供真实数据源、生产要求和这份手册，让它在用户项目中编写流程。
EnvWeaver 没有固定的生产阶段或特殊 Pipeline 语言。

- 用 [Runtime](api/runtime.md) 准备题目初始状态。
- 用 [Agent / LLM](api/agent.md) 完成必要的操作或判断。
- 编写 `run.py`，通过 [Verifier](api/verifier.md) 检查指定实例。
- 用 [Task Pool](api/task.md) 保存 Goal、环境、验证包和外 Context。

保存的是题目的初始状态；试解时另起实例。重试、筛选、批量处理由 Pipeline 自己决定。
