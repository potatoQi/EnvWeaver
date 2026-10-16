# Tools 与异常

`agent.run(tools=["shell", "ipython"], instance=instance, ...)` 自动创建并清理内置工具。
自己创建 Tool 对象时，资源生命周期由调用者管理。

::: envweaver.tools.Tool

::: envweaver.tools.shell

::: envweaver.tools.ipython

## 基建异常

以下异常不代表任务分数。标准文件异常和 HTTP 客户端异常也可能直接传出，
不要假定所有失败都会被统一包装。

::: envweaver.errors
    options:
      members: [EnvWeaverError, RuntimeError, VerifierError, AgentError, TaskExistsError]
