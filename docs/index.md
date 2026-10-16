## 接口怎么用

需要 Python 3.11+，安装到运行 Pipeline 的环境中（目前尚未发布到 PyPI）：

```bash
pip install -e /path/to/EnvWeaver
```

```python
from envweaver import (
    Runtime,
    RuntimeRef,
    TaskPool,
    agent,
    llm,
    verifier,
)

runtime = Runtime("./storage/runtimes")
pool = TaskPool("./storage/tasks", runtime=runtime)
```

以下各段只展示接口调用，不是完整生产 Pipeline；文件、模型配置和运行中的 `instance` 换成你自己的。编辑安装后修改源码，重启 Pipeline 即可生效；依赖变更需重新安装。

### Runtime：操作环境

先准备可用的 Docker 和镜像。`ref` 是环境模板，`instance` 是启动后的环境。

```python
ref = RuntimeRef(
    "docker",
    "python:3.11-slim-bookworm",
    {
        "command": ["sleep", "infinity"],
    },
)
instance = runtime.start(ref)

try:
    code, stdout, stderr = runtime.exec(
        instance, "python3 --version"
    )
    runtime.upload(
        instance, "./input.txt", "/work/input.txt"
    )
    runtime.download(
        instance, "/work/input.txt", "./downloaded.txt"
    )
    saved_ref = runtime.save(
        instance, "prepared-environment"
    )
finally:
    success, error = runtime.close(instance)
```

`save` 保存文件和启动配置，不保存内存或未保存的编辑内容。`exec` 返回非零 code 时，由 Pipeline 决定怎么处理；超时抛异常。文件复制的目标路径指目标本身；目录合并、同名文件覆盖，不删除其他文件。

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


<details markdown="1">
<summary>Docker、QEMU 与桌面连接配置</summary>

Docker 需要可用的 CLI/daemon，镜像内需有 Python 3、`/bin/sh`、`sync`、`chown`。常用 config 为 `command`、`env`、`user`、`memory`、`cpus`、`network`、`gpus`、`shm_size`、`python`（默认 python3）、`startup_timeout` 和额外 `args`。

Docker 保存时同步文件、暂停提交并导出镜像归档，然后继续运行。第一版拒绝保存带 bind mount/volume 的实例。桌面端口发现面向本地 Linux daemon；跨网络部署的连接与路由由调用方配置。

QEMU 需要 Linux 上的 `qemu-system-x86_64`、`qemu-img`、`/dev/kvm`，以及预装驱动和目标端服务的镜像：

```python
ref = RuntimeRef(
    "qemu", "/images/windows.qcow2",
    {
        "memory": 8192,  # MiB
        "cpus": 4,
        "guest_port": 8765,
        "token_env": "ENVWEAVER_GUEST_TOKEN",
        "startup_timeout": 180,
        "shutdown_timeout": 120,
    },
)
```

QEMU 默认 virtio 磁盘/网络，可用 `disk_interface`、`network_device` 调整；其他配置有 `binary`、`image_binary`、`machine`、`accel`、`format`、UEFI `firmware` 和可写 `nvram` 模板。每个实例使用独立磁盘 overlay；保存时正常关机、导出独立磁盘后重启，关机超时则失败。第一版不保存额外数据盘或 TPM 状态，Linux/Windows 虚拟机尚未完成实测。

QEMU 和 Docker 桌面镜像需预装目标端服务。在目标环境中安装本地源码或 wheel 的 `guest,desktop` extras，并在所需桌面会话中启动：

```bash
pip install '/path/to/EnvWeaver[guest,desktop]'
# 启动前设置 ENVWEAVER_GUEST_TOKEN。
envweaver-guest --host 0.0.0.0 --port 8765
```

Pipeline 侧需有相同令牌。Docker 设置 `guest_port` 后自动分配端口并注入令牌；QEMU 镜像启动时自行提供。服务只向可信调用方开放。自动注入的 Docker 令牌从提交配置中清除，但包不会清理使用者自行写入文件或镜像的秘密。

Linux 桌面需可用 X11 会话、正确的 `DISPLAY`/`XAUTHORITY`/`XDG_SESSION_TYPE=x11` 和 `scrot` 等依赖；Windows 服务的 Session 0 无法代替登录桌面。命令分别使用 `/bin/sh` 或 PowerShell，框架不会翻译命令。

</details>

<details markdown="1">
<summary>添加 Runtime 后端</summary>

实现以下方法后注册普通对象；无需继承：

```python
runtime.register("my_backend", my_backend)
```

::: envweaver.runtime.base.RuntimeBackend
    options:
      show_if_no_docstring: true

后端须保证实例独立、复制语义一致、保存引用可恢复。后端 close 失败时抛异常，由 Runtime.close 转换成返回值。注册只对当前 Runtime 对象有效；自定义后端用于 Verifier 时，run.py 中也要创建 Runtime 并注册。

</details>

### Task Pool：保存、查找、读取任务

环境准备好后，把任务文字、验证器和输入文件一起保存。

```python
goal = [
    {"type": "text", "text": "修改表格，完成销售额汇总。"}
]
context = [
    {"source": "./sales.xlsx", "target": "/work/sales.xlsx"}
]

task_ref = pool.save(
    instance,
    "sales-summary",
    goal,
    "./verify/run.py",
    context=context,
    metadata={"software": "LibreOffice"},
)
refs = pool.search(
    query="销售额", filters={"software": "LibreOffice"}
)
task = pool.load(task_ref)

# 启动这道题；外 Context 文件会自动复制进去。
candidate = runtime.start(task.runtime, task.context)
# 使用结束后调用 runtime.close(candidate)。
```

这里的 `instance` 必须仍在运行，并处于题目初始状态。任务名不能重复；验证器目录会整体保存，输入文件在每次启动时复制进去。Goal 可交错加入 `{"type": "image", "path": "./reference.png"}`。关键词匹配名称、Goal 文字和 metadata 文本（忽略大小写），filters 对 metadata 顶层字段做 AND 精确筛选。

<details markdown="1">
<summary>查看参数和返回值</summary>

::: envweaver.TaskPool
    options:
      members: [save, search, load]

::: envweaver.models.Task
    options:
      members: []

</details>

### Agent：让 Agent 执行任务

pi 是外部 Node.js 程序，需单独安装；当前测试版本：

```bash
npm install -g @earendil-works/pi-coding-agent@1.3.0
```

下面让 pi 操作指定的 `instance`，运行记录写入 `workspace`。`api_key` 从自己的配置或环境变量读取，不写进任务资产。

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


<details markdown="1">
<summary>pi 配置、桌面工具和扩展</summary>

传入 instance 时默认工具为目标 `shell`，桌面操作显式添加 `ipython`；不传时默认 pi 的 `read/bash/edit/write` 操作 Pipeline 工作目录。IPython 可跨调用保留变量，例如：

```python
import pyautogui as gui
gui.click(500, 300)
display(gui.screenshot())
```

图片内容直接返回模型。每个 cell 默认超时 300 秒，超时重启 kernel、丢失变量；工具允许任意 Python，并不限制为鼠标键盘。

pi config 还支持 `command`、`provider`、`system_prompt`、`thinking`、`session`、`timeout`、`env`、CLI `args`，以及模型的 `input`、`reasoning`、`context_window`、`max_tokens`。用 `session=result.session` 续接会话；默认不限制整次运行时间。使用已有 pi 配置时传 `agent_dir`，不要与 `base_url`/`api_key` 混用；单独传 api_key 时需指定 provider。

运行记录位于 workspace 的 `.envweaver/<run-id>/`；失败保留诊断文件并抛 AgentError。超时先停止 pi，再等待已开始的工具调用结束。

自定义工具用 Tool，调用方负责其资源清理；适配器创建的内置工具由适配器清理：

::: envweaver.tools.Tool
    options:
      members: []

添加 Agent 后端：实现 run 方法、返回 AgentResult，并注册对象。config 由该适配器解释，工具接入也需自己实现。

```python
agent.register("my_agent", my_adapter)
```

::: envweaver.agent.AgentBackend
    options:
      show_if_no_docstring: true

</details>

### LLM：直接调用模型

```python
response = llm.call(
    base_url="https://your-provider/v1",
    model_name="your-model",
    api_key=api_key,
    messages=[
        {
            "role": "user",
            "content": "从这些候选环境里选一个最合适的。",
        }
    ],
)
print(response["choices"][0]["message"]["content"])
```

使用 OpenAI Chat Completions 协议，返回完整响应。`base_url` 填 API 前缀，通常以 `/v1` 结尾。图片和 output_schema 需服务支持；HTTP/超时错误直接抛出，不自动重试。pi 自己调用模型，不必经过 llm.call。

<details markdown="1">
<summary>查看参数和返回值</summary>

::: envweaver.llm.call

</details>

### Verifier：检查任务是否完成

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

code, stdout, stderr = runtime.exec(
    instance, "python3 -m pytest -q", cwd="/work/repo"
)
if code not in (0, 1):
    raise RuntimeError(stderr)
print(json.dumps({"score": float(code == 0)}))
```

标准输出只放 JSON，日志写到标准错误。验证器执行失败会抛异常，不会被当成 0 分。

<details markdown="1">
<summary>查看参数和返回值</summary>

::: envweaver.verifier.run

</details>
