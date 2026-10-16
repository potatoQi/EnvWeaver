# 设计说明

## 职责边界

EnvWeaver 是 Python 基建包。使用者提供真实数据源和要求，由 Agent 编写普通 Python Pipeline；生产过程可以再次调用 Agent 或 LLM。框架不规定生产阶段、任务分解、调度、重试或质量筛选策略，也不限制 Pipeline 的复杂度。

组件使用普通函数和模块组合，允许保留 Coding、CUA 等领域封装。Goal 尽量来自真实需求，模型可用于提取、选择、实现和判断；具体策略由 Pipeline 决定。

部署环境、依赖、凭证和隔离由使用者负责。pip 包本身不提供安全沙盒，也不要求 Pipeline 在裸宿主机或 Docker 中运行。

## 运行位置与协作

- **Runtime** 管理目标容器或虚拟机，提供启动、命令、文件、保存和关闭能力。`RuntimeRef` 是环境模板引用，`Instance` 是已启动实例的连接信息。
- **Agent** 在 Pipeline 侧运行；传入 Instance 后，目标 shell/IPython 工具在该实例执行。不传 Instance 的 pi 默认操作 Pipeline 工作目录。
- **LLM** 请求由 Pipeline 侧发出，模型服务可以在本地或远端；Agent 也可以用自己的模型客户端。
- **Verifier** 在 Pipeline 的 Python 环境中运行。框架传入临时实例描述文件，验证包连接指定实例执行检查，也可调用 LLM 或 Agent。
- **Task Pool** 保存任务描述和材料，引用独立存储的 Runtime 资产。

Runtime 不必有桌面。桌面工具通过目标端连接服务执行 IPython/PyAutoGUI，文字和截图内容返回 Agent；IPython 会话内存不随 Runtime 保存。

## Task 与保存边界

```text
Task = Goal + Runtime + Verifier + Context
```

- **Goal**：有序的文字、图片列表；其他待处理文件放入 Context。
- **Runtime**：交给解题 Agent 的初始环境。只保存持久化文件和启动配置，恢复时重新启动，不恢复内存、未保存的编辑或原窗口状态。
- **Verifier**：包含 `run.py` 与辅助材料的验证包；私有答案不自动注入解题环境。
- **内 Context**：软件、依赖和配置，随 Runtime 保存。
- **外 Context**：任务输入文件或目录，以 `source → target` 指定位置，每次启动复制独立副本；第一版不支持挂载。
- **metadata**：可选 JSON 字典，字段由 Pipeline 决定。

保存任务时，框架归档 Goal 图片、整个验证包和外 Context，并调用 Runtime 保存环境。调用方必须先保存题目初态，再在独立实例上解题；框架不会自动推断或回滚初态。复用已有 Task 的 Runtime 不会自动继承其他字段，也不会清除快照内已经存在的文件。

同一 Pool 中名称唯一，重名报错。资产就绪后才发布任务描述，失败不会出现正常可读取的半成品任务。Runtime 资产和 Task 分开存储，自包含意味着所需资产可完整取得，不要求每个 Task 物理包含一份镜像。

第一版为本地目录存储，引用使用绝对路径。搬迁需同时保留 Task 与 Runtime 资产并处理路径；暂不提供迁移或自动垃圾回收。失败保存可能残留 Runtime 资产；进程异常终止后的 `.lock` 只能在确认生产者已退出后清理。

## 验证约定

`verifier.run` 用当前 Python 解释器、以验证包为工作目录执行 `run.py --instance <临时描述文件>`，结束后清理描述文件。验证逻辑决定怎么检查，调用方传入的 Instance 决定检查谁。

stdout 只能包含一个 JSON 对象，`score` 为 0～1 的有限数值；日志写 stderr。合法的零分与执行失败严格分开：异常退出、超时、格式错误或越界分数均抛异常，由 Pipeline 决定后续处理。

分别在正确状态和未完成状态检查验证器，是 Pipeline 可采用的质量检查，不是框架自动步骤，也不能证明验证器完全正确。模型服务配置和凭证由调用方提供，不固化进任务资产。

## 扩展边界

Runtime 与 Agent 各用一个 Protocol 描述后端接口，无需继承公共基类；注册方式见接口手册对应折叠项。目标端连接设施集中在 `_guest/`，Docker/QEMU 的保存逻辑留在各自后端。

第一版内置 Docker、QEMU/KVM 和 pi，LLM 使用 OpenAI Chat Completions 协议。Codex、Claude Code 等运行时适配器尚未实现。目标平台为 Linux 服务器，虚拟机优先 Linux/Windows，macOS/Android 后续扩展。

## 验证范围

截至 2026-10-16，已有真实 Docker 任务保存/恢复、规则验证器、非 root 文件编辑及 Linux X11 截图/鼠标操作验证。pi 工具调用、图片传递和会话继续使用真实 pi 进程与模拟模型服务测试；另有真实模型的小型 Coding 修复闭环和 LLM 文本调用记录，不能代表复杂生产 Pipeline 已验收。

尚缺真实模型驱动的完整 CUA 流程、模型型 Verifier、QEMU/KVM Linux/Windows 实测，以及 Agent 编写复杂可复用生产 Pipeline 的系统验收。

测试从仓库根目录运行；外部集成测试按需配置：

```bash
uv run pytest
ENVWEAVER_TEST_PI=/path/to/pi uv run pytest tests/test_llm_agent.py
ENVWEAVER_TEST_DOCKER=python:3.11-slim-bookworm uv run pytest tests/test_docker_integration.py
ENVWEAVER_TEST_DESKTOP=your-x11-image uv run pytest tests/test_desktop_integration.py
```

X11 测试镜像需有 IPython 依赖，也可通过 `ENVWEAVER_TEST_KERNEL_WHEELS` 提供离线 wheels。协议或模拟测试通过不代表对应真实系统已经验收。
