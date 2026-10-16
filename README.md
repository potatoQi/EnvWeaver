# EnvWeaver

**Agent-Authored Pipelines for Executable Task Generation**

提供少量可组合的基建接口，让 Agent 根据真实数据源编写可复用的 Data Pipeline，持续生产可执行、可验证、可供训练的任务。

第一版基建代码已实现，可从源码或 wheel 安装，尚未发布到 PyPI。使用方式见 [使用说明](docs/usage.md)，已确认的设计见 [设计文档](docs/design.md)。

完整手册从 [文档首页](docs/index.md) 开始，API 参考由 Python 签名、类型和 docstring 自动生成。本地预览：

```bash
uv run --group docs mkdocs serve
# 严格构建，输出到 site/：
uv run --group docs mkdocs build --strict
```

Markdown 指南、代码说明与生成页面之间的关系见 [文档维护](docs/contributing.md)。

```bash
uv sync
uv run pytest
uv build
```

```python
from envweaver import Runtime, RuntimeRef, TaskPool, agent, llm, verifier

runtime = Runtime("./storage/runtime")
task = TaskPool("./storage/tasks", runtime=runtime)
instance = runtime.start(RuntimeRef("docker", "python:3.11-slim-bookworm", {
    "command": ["sleep", "infinity"],
}))
try:
    print(runtime.exec(instance, "python3 --version"))
finally:
    runtime.close(instance)
```

## 使用方式

```text
使用者安装 EnvWeaver，提供数据源、生产要求和接口文档
    ↓
Coding Agent 编写调用 EnvWeaver 的 Python Pipeline
    ↓
使用者在自己选择的环境中运行 Pipeline
    ↓
Task Pool → 下游训练与评测
```

EnvWeaver 计划作为可通过 pip 安装的 Python 包发布。Pipeline 的运行位置、依赖环境、权限和隔离方式由使用者决定；uv、conda、Docker 等均不改变包的职责。项目自身使用 uv 管理 Python 依赖。

## 核心设计

```text
Task = Goal + Runtime + Verifier + Context
```

- **Goal**：有序的文字、图片内容列表。
- **Runtime**：可恢复的执行环境及必要启动配置；运行中的对象称为 `instance`。
- **Verifier**：包含 `run.py` 的验证包，输出包含 `score` 的 JSON，分数范围为 0～1。
- **Context**：任务依赖和输入材料。软件等内 Context 随 Runtime 持久化；外 Context 单独保存，启动时复制到实例。
- **metadata**：可选的 JSON 元数据，字段由 Pipeline 自定。

Task Pool 是存储抽象，Runtime 资产独立存储并由 Task 引用。自包含意味着所需资产可以完整取得和恢复，不要求每个 Task 物理复制一份镜像。

## 第一版范围

| 部分 | 已确认选择 |
| --- | --- |
| 语言与依赖 | Python，uv |
| Runtime backend | Docker、QEMU/KVM；目标运行平台为 Linux 服务器 |
| 任务系统 | 先跑通 Linux 和 Windows；macOS、Android 后续扩展 |
| 起始环境 | 使用预先具备所需连接设施的基础镜像 |
| 环境准备 | `start → exec / Agent 操作 → save`；暂不提供 `build` 接口 |
| Agent | 可替换，优先接 pi；桌面工具参考 Mini-CUA 的 IPython tool |
| LLM | OpenAI 兼容协议，支持配置服务地址、模型和 API Key |
| 存储 | 本地目录；Task 名称在同一个 Pool 内唯一，重名报错 |
| 检索 | 关键词搜索、metadata 字段精确筛选 |

## 职责边界

EnvWeaver 提供环境操作、Agent/LLM 调用、验证执行和任务存储能力。普通函数和模块就是组件，允许保留 Coding、CUA 等领域的高层封装。

Pipeline 的编写者决定数据获取、任务构造、Agent 选择、会话使用、质量检查、重试和调度。包不规定统一的生产流程，也不引入专门的编排语言。

模型优先用于选择、提取、实现和判断；Goal 尽量保留真实需求，减少自由创作带来的分布偏差。具体生成策略仍由 Pipeline 决定。

已验证真实 Docker 的任务保存/恢复闭环、Linux X11 桌面的截图与鼠标操作，以及真实 pi + IPython 与模拟模型服务的工具、图片交互。QEMU/KVM 后端已经实现，但当前测试服务器没有 KVM，因此 Linux/Windows 虚拟机尚未完成实测。详见 [验证范围](docs/usage.md#7-示例和验证范围)。

接口能够承载生产流程，不等于已经验证 Agent 可以稳定写出高质量 Pipeline。[Coding 示例](examples/coding/pipeline.py) 展示调用方式；领域流程不进入基础设施代码。
