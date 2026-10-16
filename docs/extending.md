# 扩展指南

扩展使用普通 Python 对象，不需要插件框架或特殊继承树。

## Runtime 后端

实现以下 Protocol 所列方法，然后注册实例：

```python
runtime = Runtime("./storage/runtimes")
runtime.register("my_backend", my_backend)
instance = runtime.start(RuntimeRef("my_backend", "my-image"))
```

::: envweaver.runtime.base.RuntimeBackend
    options:
      show_if_no_docstring: true

后端需要保证启动实例独立、文件复制语义一致、保存后的引用可重新启动。
失败应抛出异常；后端 close 不返回 success/error，由 Runtime.close 负责转换。

注册只对当前 Runtime 对象有效。Verifier 在独立进程运行，若使用自定义后端，
run.py 也要创建 Runtime 并注册该后端，再使用读取到的 Instance。

## Agent 后端

实现 run 方法，返回 AgentResult，然后在 Pipeline 进程注册：

```python
agent.register("my_agent", my_adapter)
result = agent.run("my_agent", instruction, workspace, config=config)
```

::: envweaver.agent.AgentBackend
    options:
      show_if_no_docstring: true

config 由适配器解释。是否支持某个工具、如何连接目标实例、如何清理资源，
需要适配器明确实现；注册名称不会自动获得 pi 的能力。

## 目录边界

- `runtime/`、`agent/` 各自管理公开入口与后端实现。
- `task.py`、`verifier.py`、`llm.py`、`tools.py` 各自保持单一职责。
- `_guest/` 放目标端服务、连接客户端和 IPython kernel，属于内部实现。
- `guest.py` 只兼容旧的模块启动命令，标准入口仍是 `envweaver-guest`。
- 领域流程放在用户的 Pipeline 中；普通高层函数可以组合这些基础接口。
