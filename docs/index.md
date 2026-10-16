# EnvWeaver：接口怎么用

安装到运行 Pipeline 的 Python 环境中（目前尚未发布到 PyPI）：

```bash
pip install -e /path/to/EnvWeaver
```

```python
from envweaver import Runtime, RuntimeRef, TaskPool, agent, llm, verifier

runtime = Runtime("./storage/runtimes")
pool = TaskPool("./storage/tasks", runtime=runtime)
```

以下各段独立展示调用方式；文件、模型配置和运行中的 `instance` 换成你自己的。

## Runtime：操作环境

先准备可用的 Docker 和镜像。`ref` 是环境模板，`instance` 是启动后的环境。

```python
ref = RuntimeRef("docker", "python:3.11-slim-bookworm", {
    "command": ["sleep", "infinity"],
})
instance = runtime.start(ref)

try:
    code, stdout, stderr = runtime.exec(instance, "python3 --version")
    runtime.upload(instance, "./input.txt", "/work/input.txt")
    runtime.download(instance, "/work/input.txt", "./downloaded.txt")
    saved_ref = runtime.save(instance, "prepared-environment")
finally:
    success, error = runtime.close(instance)
```

`save` 保存文件和启动配置，不保存内存或未保存的编辑内容。`exec` 返回非零 code 时，由 Pipeline 决定怎么处理。

<details markdown="1">
<summary>查看参数和返回值</summary>

::: envweaver.Runtime
    options:
      members: [start, exec, upload, download, save, close, connect]

::: envweaver.models.RuntimeRef
    options:
      members: []

::: envweaver.models.Instance
    options:
      members: []

</details>

## Task Pool：保存、查找、读取任务

环境准备好后，把任务文字、验证器和输入文件一起保存。

```python
goal = [{"type": "text", "text": "修改表格，完成销售额汇总。"}]
context = [{"source": "./sales.xlsx", "target": "/work/sales.xlsx"}]

task_ref = pool.save(
    instance, "sales-summary", goal, "./verify/run.py",
    context=context, metadata={"software": "LibreOffice"},
)
refs = pool.search(query="销售额", filters={"software": "LibreOffice"})
task = pool.load(task_ref)

# 启动这道题；外 Context 文件会自动复制进去。
candidate = runtime.start(task.runtime, task.context)
# 使用结束后调用 runtime.close(candidate)。
```

这里的 `instance` 必须仍在运行，并处于题目初始状态。任务名不能重复；验证器目录会整体保存，输入文件在每次启动时复制进去。

<details markdown="1">
<summary>查看参数和返回值</summary>

::: envweaver.TaskPool
    options:
      members: [save, search, load]

::: envweaver.models.Task
    options:
      members: []

</details>

## Agent：让 Agent 执行任务

先安装 pi。下面让 pi 操作指定的 `instance`，运行记录写入 `workspace`。

```python
result = agent.run(
    backend="pi",
    instruction="打开 /work/sales.xlsx，完成销售额汇总并保存。",
    workspace="./runs/sales-summary",
    instance=instance,
    tools=["shell", "ipython"],
    config={
        "base_url": "https://your-provider/v1",
        "model_name": "your-model",
        "api_key": api_key,
    },
)
print(result.text)
```

pi 在 Pipeline 侧运行；shell 和 IPython 在目标环境内执行。桌面操作需要目标镜像预装连接服务、PyAutoGUI 和可用桌面，模型需要支持看图。

<details markdown="1">
<summary>查看参数和返回值</summary>

::: envweaver.agent.run

::: envweaver.models.AgentResult
    options:
      members: []

</details>

## LLM：直接调用模型

```python
response = llm.call(
    base_url="https://your-provider/v1",
    model_name="your-model",
    api_key=api_key,
    messages=[{"role": "user", "content": "从这些候选环境里选一个最合适的。"}],
)
print(response["choices"][0]["message"]["content"])
```

返回完整的 OpenAI 兼容响应。`base_url` 填 API 前缀，通常以 `/v1` 结尾。

<details markdown="1">
<summary>查看参数和返回值</summary>

::: envweaver.llm.call

</details>

## Verifier：检查任务是否完成

```python
result = verifier.run("./verify/run.py", instance)
print(result["score"])  # 0～1，允许部分得分
```

`run.py` 由 Pipeline 编写。它在 Pipeline 侧运行，框架自动传入要验证的实例。例如，在目标环境内运行测试：

```python
import argparse
import json
from envweaver import runtime

parser = argparse.ArgumentParser()
parser.add_argument("--instance", required=True)
instance = runtime.connect(parser.parse_args().instance)

code, stdout, stderr = runtime.exec(instance, "python3 -m pytest -q", cwd="/work/repo")
if code not in (0, 1):
    raise RuntimeError(stderr)
print(json.dumps({"score": float(code == 0)}))
```

标准输出只放 JSON，日志写到标准错误。验证器执行失败会抛异常，不会被当成 0 分。

<details markdown="1">
<summary>查看参数和返回值</summary>

::: envweaver.verifier.run

</details>
