# EnvWeaver

**Agent-Authored Pipelines for Executable Task Generation**

提供少量通用基建，让 Agent 根据真实数据源编写可复用的 Data Pipeline，持续生产可执行、可验证、可供训练的任务。

当前处于设计阶段；本文中的接口是设计草案，尚未实现。

## 核心思路

```text
人类提供任务方向、数据源和约束
    ↓
Agent 编写可复用的 Data Pipeline
    ↓
数据工程师运行 Pipeline，持续处理新的源数据
    ↓
Task Pool
    ↓
下游 SFT / RL / 评测
```

交付物是可重复运行、可维护的生产程序。Pipeline 可以调用 Agent 和 LLM；基础设施不规定统一的阶段顺序，也不引入专门的编排语言。

- 普通函数和模块就是组件。通用能力尽量复用，Coding、GUI、CLI、CUA 等领域可以保留各自的高层封装。
- Goal 尽量来自真实需求、仓库、论坛和任务材料，减少模型自由创作带来的分布偏差。
- Pipeline 内的模型优先承担选择、提取、实现和判断，例如选择合适的 Runtime、准备环境或编写验证器。
- 下面的接口主要面向编写 Pipeline 的 Agent。运行中的 Agent 使用常规命令、文件和桌面工具，无须为每类业务定制专用工具。

## Task

```text
Task = Goal + Runtime + Verifier + Context
```

| 部分 | 含义 |
| --- | --- |
| Goal | 交给解题 Agent 的任务要求，可包含文字和图片。 |
| Runtime | 可恢复的执行环境及必要运行配置；可以由容器或虚拟机提供。运行中的对象称为 instance。 |
| Verifier | 具有统一执行入口的验证对象，可以包含程序、模型调用、Agent 调用和辅助材料。 |
| Context | 完成任务所需的依赖、配置和输入材料。 |

Context 分为两部分：内 Context 是随 Runtime 持久化的软件、依赖和配置；外 Context 是按任务注入的文件或材料，例如通过上传或挂载提供的输入文件。

Task Pool 是逻辑管理层。Runtime 和其他资产可以独立存储并被多个 Task 引用。自包含意味着任务所需内容可以完整取得和恢复，不要求每个 Task 物理复制一份镜像。

从已有 Task 选择 Runtime 时，不默认继承该 Task 的 Goal、Verifier 和外 Context。

## 最小接口草案

```text
runtime.start(ref, context=None) -> instance
runtime.exec(instance, command, cwd, timeout) -> exit_code, stdout, stderr
runtime.upload(instance, local_path, remote_path) -> None
runtime.download(instance, remote_path, local_path) -> None
runtime.save(instance, name) -> runtime_ref
runtime.close(instance) -> success, error

verifier.run(entry_path, instance) -> result_json

agent.run(base_url, model_name, api_key,
          instruction, workspace, tools, instance=None) -> result

llm.call(base_url, model_name, api_key,
         messages, output_schema=None) -> response

task.load(task_ref) -> task
task.search(query) -> task_refs
task.save(instance, name, goal, verifier, context) -> task_ref
```

### 接口约定草案

- `runtime.start` 创建独立实例，准备 Context 和运行配置，返回时相关控制服务已经可用。`instance` 携带实际支持的命令、文件或桌面连接信息。
- `runtime.exec` 在目标实例内执行命令。宿主机上的普通程序逻辑由 Pipeline 自己执行。
- `runtime.upload/download` 支持文件和目录并保留目录结构，图片作为普通文件传递。
- `runtime.save` 保存持久化状态及恢复所需配置，保存后原实例仍可继续使用。默认不保存应用内存、未落盘内容或外部挂载数据。
- `runtime.close` 释放实例资源。废弃持久化产物的回收由底层存储管理，须保护仍被引用的资产。
- `agent.run` 将指定工具绑定到目标实例；桌面工具包括截图、点击等能力。轨迹和图片保存在 `workspace`，由 `result` 引用。
- `llm.call` 在宿主机侧调用模型，`messages` 支持文字与图片。
- `verifier.run` 的入口在宿主机执行，获得目标实例后可复用 Runtime、Agent 和 LLM 接口。模型服务配置由宿主机提供，不固化密钥到任务包。
- `task.save` 内部调用 Runtime 保存能力，保存任务初始状态及完整验证包、Context 或稳定引用，全部就绪后再发布。验证私有材料不自动暴露给解题 Agent。
- 原始输入、Agent 修改后的工作文件和验证器私有材料应分别管理。任务必须能在离开生产临时目录后重新执行。

### 待确定的构建能力

如果支持从构建材料生产新的基础环境，可增加：

```text
runtime.build(build_path) -> runtime_ref
```

`build_path` 指向构建目录，例如 Dockerfile 及其相关文件；后端由构建配置确定。如果初期只使用已有基础环境，可以暂不提供此接口。该接口目前仍是提案。

## Pipeline 的职责

Pipeline 自己决定如何获取源数据、选择 Runtime、准备初始状态、整理 Goal、编写 Verifier、检查质量和保存 Task。领域算法、并发、重试、断点续跑等可以作为普通模块复用，无须全部成为平台接口。

正确参考状态能够通过、初始未完成状态不能通过，是验证器的基本检查；两项检查应使用同一份验证器和独立实例。这不构成验证正确性的完整证明。

保存训练任务时应保存初始状态 A。完成状态 B 可以作为参考或后续任务的环境基础，但不能混作当前任务的起点。A/B 的关系和生产记录由 Pipeline 管理。

## 设计参照

- **CodeWorld**：仓库发现、环境构建、代码与测试分析、任务生成和质量检查，可以作为 Coding Pipeline 的可复用模块。
- **EnvWorld**：真实软件需求、桌面操作、A/B 状态验证、验证包和环境状态积累，用于检查 CUA 流程的覆盖程度。

现阶段的判断来自代码审阅。接口能够表达生产流程，不等于已经证明 Agent 可以稳定编写达到现有项目质量的 Pipeline；后续需要通过实际运行验证。
