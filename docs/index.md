# EnvWeaver 使用手册

EnvWeaver 是一个 Python 包，为 Agent 编写可复用的任务生产 Pipeline 提供基础接口。

用户准备数据源、模型配置和可启动的环境，编程 Agent 编写普通 Python Pipeline。
Pipeline 调用 EnvWeaver 准备环境、调用模型或 Agent、执行验证、保存 Task。
生产流程、任务策略和运行部署均由用户决定。

| 你想做什么 | 从这里开始 |
| --- | --- |
| 安装包，完成第一次环境调用 | [快速开始](quickstart.md) |
| 理解 RuntimeRef、Instance、Task 和 Context | [核心概念](concepts.md) |
| 配置 Docker、QEMU、pi 和验证器 | [使用指南](usage.md) |
| 查参数、返回值、异常 | [Runtime API](api/runtime.md)、[Task Pool API](api/task.md)、[Agent / LLM API](api/agent.md)、[Verifier API](api/verifier.md) |
| 接入自己的 Runtime 或 Agent | [扩展指南](extending.md) |

## 当前范围

第一版可从源码或 wheel 安装，尚未发布到 PyPI。
内置 Docker、QEMU/KVM 后端和 pi 适配器；LLM 使用 OpenAI 兼容 Chat Completions。
Task Pool 使用本地目录。Linux Docker 与桌面连接已实测；QEMU/KVM 和 Windows VM 尚未完成实机验收。

编程 Agent 可以直接阅读仓库内的 Markdown 指南和 Python docstring。
API 网页从源码生成，不需要额外维护一套参数表。
