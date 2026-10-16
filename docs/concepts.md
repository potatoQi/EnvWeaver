# 核心概念

```text
Task = Goal + Runtime + Verifier + Context
```

| 对象 | 含义 |
| --- | --- |
| RuntimeRef | 可启动环境的引用：backend、镜像或磁盘、启动配置。 |
| Runtime | 操作环境、管理环境资产的 Python 对象。 |
| Instance | 已启动的容器或 VM 的连接描述。它不是磁盘快照。 |
| Goal | 按顺序排列的文字、图片，描述用户需求。 |
| Verifier | 一个以 `run.py` 为入口的验证包，输出包含 score 的 JSON。 |
| Context | 题目所需的软件、依赖和输入材料。 |
| Task Pool | 保存、读取、检索任务的本地目录存储。 |

## Context 与保存

内 Context 是安装进环境的软件、依赖等，随 Runtime 的持久化状态保存。
外 Context 使用 `source → target` 文件映射，材料存进任务包，每次启动复制到实例。
第一版不支持通过挂载注入外 Context。

`runtime.save` 保存磁盘文件和启动配置，不保存进程内存、未保存的编辑内容或窗口状态。
`task.save` 调用环境保存能力，并收集 Goal 图片、验证包、外 Context 和 metadata。
Task 引用独立的 Runtime 资产，不要求所有东西塞在一个文件里。

## 两种 Agent

**编写 Pipeline 的 Agent** 是用户使用的 Codex、Claude Code 等编程工具。
它阅读接口文档，生成用户自己的 Python 程序。

**Pipeline 运行时调用的 Agent** 通过 `agent.run` 启动，例如调用 pi 操作某个实例。
何时调用、使用哪个后端、是否继续会话，由 Pipeline 决定。

## 程序在哪里运行

| 内容 | 位置 |
| --- | --- |
| Pipeline、pi 进程、LLM 客户端、验证入口 run.py | 用户选定的 Pipeline 执行环境，可以是虚拟环境或容器。 |
| Runtime.exec 的命令、IPython cell、桌面软件 | 被操作的目标 Instance 内。 |
| 模型推理服务 | base_url 指向的位置，可以在本地，也可以是远程服务。 |

Verifier 通过框架传入的 Instance 描述连接本次目标。同一份验证包可以验证不同实例。
LLM 本身不会连接桌面：Pipeline 或 Verifier 先取得文件/截图，再把需要的内容发给模型。

## 分数与失败

`score=0` 表示验证器正常执行，任务得零分。验证器崩溃、超时或输出格式错误是异常，
不转换成零分。Agent 自称完成也不等于验证通过。
