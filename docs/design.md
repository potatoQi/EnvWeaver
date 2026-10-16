# EnvWeaver 第一版设计

本文整理截至 2026-10-16 已确认的设计。首版基建实现已加入仓库，尚未发布到 PyPI。实际调用方式、配置、测试结果及尚未验收的范围见 [使用说明](usage.md)。本页保留设计职责与接口契约。

## 1. 定位与职责

EnvWeaver 是可通过 pip 安装的 Python 基建包，项目使用 uv 管理依赖。使用者向 Coding Agent 提供包的文档、数据源和要求，由 Agent 编写普通 Python Data Pipeline。Pipeline 可以反复处理新输入，也可以调用 Agent 和 LLM。

| 参与方 | 职责 |
| --- | --- |
| EnvWeaver | Runtime 后端、Agent/LLM 调用、Verifier 执行契约、Task 与资产存储接口。 |
| Pipeline 编写者 | 获取数据、准备环境、构造任务、选择 Agent、组织会话、使用验证结果，以及调度、重试和质量策略。 |
| 使用者 | 安装和运行环境、依赖准备、模型服务、凭证、权限与隔离部署。 |

包不强制 Pipeline 运行在宿主机、Docker 或某一种 Python 环境里。安全隔离和受限 Runtime 管理服务属于部署方案，不是所有用户安装使用本包的前提。Python 包以及 uv/conda 环境本身不承担任意 Pipeline 代码的系统权限隔离。

Pipeline 中运行的 Agent 使用常规工具即可。选择 Runtime、生成任务等业务不必各自注册专用工具。领域组件可以使用并组合底层接口，不要求将所有领域逻辑拆成统一的微小操作。

## 2. Runtime

### 范围

- 目标运行平台为 Linux 服务器，第一版实现 Docker 和 QEMU/KVM 两个 backend。
- 第一版验收覆盖 Linux 和 Windows；Docker 路线先覆盖 Linux，QEMU/KVM 路线覆盖 Linux、Windows 虚拟机。
- 长期目标包括 Linux、Windows、macOS、Android；不把第一版后端实现等同于已支持所有系统。
- 起点使用预先准备好命令、文件或桌面连接设施的基础镜像。任务软件、仓库和依赖由 Pipeline 自己准备。
- 已保存的 Runtime 也可以作为后续任务的起点，不限于固定的基础镜像集合。

### 对象与接口

`runtime_ref` 引用已保存的环境，关联 backend、镜像或磁盘资产及必要启动配置。`instance` 表示运行中的目标实例，携带身份和操作所需连接信息。Docker、QEMU 的差异由各自 backend 处理。

```text
runtime.start(ref, context=None) -> instance
runtime.exec(instance, command, cwd, timeout) -> exit_code, stdout, stderr
runtime.upload(instance, local_path, remote_path) -> None
runtime.download(instance, remote_path, local_path) -> None
runtime.save(instance, name) -> runtime_ref
runtime.close(instance) -> success, error
```

- `start` 从引用创建独立实例，准备所需连接并注入传入的外 Context，返回可操作的实例。
- `exec` 在指定目标实例中执行命令。Pipeline 自身的代码在哪里运行，由使用者决定。
- `upload/download` 支持文件和目录并保留目录结构；图片按普通文件传递。
- `save` 保存持久化文件或磁盘状态及必要启动配置，恢复时重新启动环境；不承诺恢复进程内存、未保存的编辑内容或原来的窗口状态。保存后原实例仍可继续使用，后端负责必要的状态一致性处理。
- `close` 释放运行实例；持久化资产与实例生命周期分开管理，清理时保护仍被引用的资产。

第一版不提供 `build`，环境准备使用：

```text
已有基础镜像 → start → exec / Agent 操作 → save → 新 runtime_ref
```

需要 Dockerfile 构建等额外能力时，由使用者或 Pipeline 调用外部工具完成。

### Runtime 与 Tools

Runtime 准备、启动环境并提供连接信息；Tools 利用连接执行命令、读写文件或操作桌面；Agent 决定如何调用工具。

不强制所有实例都具备桌面。截图、点击等是面向目标环境的工具能力，不需要给纯命令环境安装桌面，也不预先增加复杂的能力注册体系。

## 3. Agent 与 LLM

### Agent

```text
agent.run(backend, instruction, workspace,
          tools=None, instance=None, config=None) -> result
```

- `backend` 选择 Agent 实现，第一版优先接 pi，结构上允许接入 Codex、Claude Code 等其他实现。
- `config` 传所选实现的配置，例如模型、服务地址和凭证。不同 Agent 可以有自己的配置。
- `instruction` 是本次任务，`workspace` 是使用者为本次运行指定的工作目录。
- `tools` 指定提供的工具；`instance` 指定工具要操作的目标环境，可以为空。
- `result` 引用运行结果、产物和轨迹；轨迹与图片等运行材料保存到工作目录。

适配层负责启动与连接、配置转换、结果与轨迹收集、结束与清理。Agent 选择、是否继续已有会话、重试和多 Agent 协作由 Pipeline 决定；适配层保留实现这些策略所需的底层能力，不规定统一策略。

Agent 不必经过 EnvWeaver 的 `llm.call` 调用模型，可以使用自身的模型接入机制。

### 桌面工具

优先参考 Mini-CUA 的 `ipython(code)`，而非重新实现一套 Agent 循环：

```text
pi 调用 ipython(code)
    ↓
代码在目标实例的 Python/IPython 会话中执行
    ↓
操作桌面、文件或程序
    ↓
文字和 display(...) 产生的图片返回 pi
```

同一工具会话内变量和导入可跨调用保留，代码通过 PyAutoGUI 等执行桌面操作。IPython 会话内存不属于 Runtime 的持久化快照。

代码审阅时 Mini-CUA 明确验证过的桌面链路是 Linux X11；Windows 的完整链路仍需实际验证，不能据此声明已支持。

### LLM

```text
llm.call(base_url, model_name, api_key,
         messages, output_schema=None) -> response
```

第一版支持 OpenAI 兼容协议，其他协议后续扩展。可连接兼容该协议的本地或远端服务；文字和图片通过 `messages` 传入，`output_schema` 用于指定结构化输出要求。具体模型仍需支持请求使用的能力。

LLM 调用代码位于调用者的执行环境中，不要求在裸宿主机运行，也不要求模型服务部署在本机。模型配置和凭证由使用者或 Pipeline 提供，不作为固化的任务资产保存。

## 4. Task 数据契约

```text
Task = Goal + Runtime + Verifier + Context
```

此外，Task 有名称、引用和可选的 `metadata`。不规定 Coding、CUA 等领域专属字段。

### Goal

Goal 是有序内容列表，第一版包含 `text` 和 `image` 两种元素。保留文字和图片交错出现的顺序：

```json
[
  {"type": "text", "text": "把表格调整成下面这样的布局："},
  {"type": "image", "path": "layout.png"},
  {"type": "text", "text": "再按下面的参考图调整颜色："},
  {"type": "image", "path": "colors.png"}
]
```

纯文字任务只有 text 元素。保存 Task 时保存或稳定引用 Goal 的图片，恢复后解析到实际资产。

### Context

- **内 Context**：安装的软件、依赖、系统或应用配置，随 Runtime 持久化。
- **外 Context**：按任务提供的原始输入文件和材料，单独保存，启动实例时注入。

外 Context 第一版采用文件映射列表，每项指定 `source` 和 `target`，支持文件或目录：

```json
[
  {"source": "inputs/sales.xlsx", "target": "C:\\Tasks\\sales.xlsx"}
]
```

`source` 指源材料，`target` 指目标实例中的位置，由 Pipeline 决定。保存任务时归档源材料，恢复时从已保存资产解析源位置。第一版采用复制，每个实例使用独立副本；挂载后续扩展。

Goal 是交代任务的消息，Context 是目标环境中准备的材料。例如参考图可以放入 Goal，待修改的 Excel 文件放入 Context。相同文件在有需要时可以同时被消息引用和作为任务文件提供。

### metadata

可选字典，内容须可序列化为 JSON，字段由 Pipeline 自定，例如来源 URL、标签、软件名称。框架提供存储和字段精确筛选，不规定领域特有结构。

### 任务初态与资产边界

Task 的 Runtime 表示交给下游解题 Agent 的初始状态 A。完成状态 B 可以保留为参考或后续任务的环境基础，不能误当成当前任务的起点。

原始 Context、Agent 修改后的工作文件、Verifier 私有材料分开管理。验证器的答案和私有参考材料不自动注入解题环境。

选择某个已有 Task 的 Runtime 时，仅选择其环境引用，不默认继承 Goal、Verifier 或重新注入外 Context。这不意味着自动从环境快照中删除已经持久化的文件；环境内容取决于保存时的状态，复用准备由 Pipeline 决定。

## 5. Verifier

### 验证包与入口

验证包是包含 `run.py` 及所需辅助材料的目录。入口是独立程序，不要求实现固定名称的 Python 函数。它可以调用其他脚本、LLM 或 Agent。

```text
verifier.run(entry_path, instance) -> result_json
```

`entry_path` 指向验证包的 `run.py`。调用方传入目标 `instance`，框架自动：

1. 为目标实例生成临时描述文件，包含连接所需的信息。
2. 使用 EnvWeaver 当前 Python 环境的解释器启动入口，并传入参数。
3. 读取、校验结果，结束后清理临时描述文件。

```text
<当前 Python 解释器> run.py --instance <临时实例描述文件路径>
```

`run.py` 通过 EnvWeaver 读取描述文件并连接目标实例。描述文件是连接信息，不是被验证的输入文件，也不是持久化任务资产；调用方无须手工生成它。

实现中的读取接口为 `runtime.connect(instance_path) -> instance`，只连接已有实例，不创建新实例。验证逻辑负责“如何检查”，调用方传入的 `instance` 决定“检查谁”。

入口运行在调用者选择的执行环境中，可以是在容器内，不能将其描述为必须在裸宿主机或目标 Runtime 内运行。依赖安装、是否隔离以及如何组织验证逻辑，由使用者和 Pipeline 编写者负责。

### 输出与错误

标准输出承载一个 JSON 结果，最低要求为：

```json
{"score": 0.8}
```

`score` 是 0～1 范围内的有限数值，允许部分得分。日志写入标准错误，避免混入结果输出。

- 程序正常结束且输出合法：返回 `result_json`；`score: 0` 表示正常验证后得到零分。
- 程序异常退出、执行超时、输出不是合法结果或分数越界：`verifier.run` 抛出异常，不返回零分。
- 重试、跳过、人工检查等后续处理由 Pipeline 决定。包不规定统一的重试或质量筛选流程。

使用同一份验证包，在独立的正确状态和未完成状态上检查，是 Pipeline 可采用的基本质量检查，不构成验证正确性的完整证明，也不是每次 `verifier.run` 自动执行的步骤。

## 6. Task Pool

Task Pool 是存储抽象，第一版使用使用者指定的本地目录。Runtime 的镜像或磁盘资产独立存储，多个 Task 可以引用同一资产。任务可完整恢复不等于每个任务都包含独立镜像副本。

```text
task.save(instance, name, goal, verifier,
          context=None, metadata=None) -> task_ref
task.load(task_ref) -> task
task.search(query=None, filters=None) -> task_refs
```

### 保存与读取

- `name` 在同一个 Task Pool 内唯一。重名直接报错，保留原任务，不覆盖、不自动添加后缀；Pipeline 自行换名重试。
- `task.save` 内部使用 Runtime 保存能力，保存给定实例的持久化状态。调用者负责传入正确的任务初始状态，框架不推断或回滚到某个历史时刻。
- 收集 Goal 图片、Verifier 包和外 Context 材料，保存完整内容或稳定引用，不能只留下生产机器临时目录中的路径。
- 所需资产全部就绪后发布任务描述。保存失败不能让不完整 Task 成为正常可读取的任务。
- `task.load` 读取描述并解析资产引用，不代替调用方决定何时启动实例或运行验证。

### 检索

`query` 提供关键词搜索，`filters` 提供 metadata 字段精确筛选；返回匹配的 `task_refs`。哪个 Task 或 Runtime 适合当前工作，由 Pipeline 或其调用的 Agent 决定。

## 7. 组件交互与实现边界

```text
使用者 / Coding Agent 编写的 Pipeline
    ├─ Runtime：准备、操作和保存目标环境
    ├─ Agent：调用指定实现，将工具连接到目标实例
    ├─ LLM：直接调用兼容的模型服务
    ├─ Verifier：执行验证包并返回分数或异常
    └─ Task Pool：保存和检索任务及资产引用
```

公共操作与后端实现分开，Docker、QEMU 和不同 Agent 的差异放在对应适配层。存储先实现本地目录，其他存储实现可以后续接入。仅在实际变化的边界保留抽象，不引入插件框架、工作流语言或固定生产阶段。

代码保持普通函数和模块的组合：主流程负责串联步骤，配置转换、资源清理、材料归档各自使用具名的小函数。Runtime/Agent 的 Protocol 只描述后端接口，不要求实现类继承公共基类。不要把领域策略放入基建，也不要为假设中的扩展预先建立层级。

目标端的 `guest.py` 负责 HTTP 连接与会话生命周期，`_kernel.py` 负责 IPython 执行；两者职责分开。Docker 和 QEMU 共用目标服务的连接等待逻辑，保存方式仍留在各自后端。

参考项目的作用：

- CodeWorld：检验仓库环境准备、代码与测试分析、任务生成和质量检查如何通过这些基建组合。
- EnvWorld：检验桌面环境、附件、A/B 验证和环境复用。
- Mini-CUA：参考 IPython 工具的目标端执行、会话状态及文字/图片返回。

上述参考不意味着照搬项目的流程或要求 Pipeline 遵守其策略。后续验收应关注 Coding 与 CUA Pipeline 能否复用同一套接口，Task 能否在生产实例关闭后重新启动与验证，以及生产程序能否持续处理新输入。
