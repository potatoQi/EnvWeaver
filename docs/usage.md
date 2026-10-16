# 第一版使用说明

这是可安装、可运行的首版基建实现，尚未发布到 PyPI。Python 3.11+，项目开发使用 uv；Pipeline 的部署方式由使用者决定。

```bash
uv sync
uv run pytest
uv build
# 在其他 Python 环境中安装构建产物：
pip install dist/envweaver-0.1.0-py3-none-any.whl
```

## 1. 对象与存储位置

```python
from envweaver import Runtime, RuntimeRef, TaskPool, agent, llm, verifier

runtime = Runtime("./storage/runtime")
task = TaskPool("./storage/tasks", runtime=runtime)
```

`runtime` 和 `task` 是普通 Python 对象，目录由调用者选择。也可使用 `from envweaver import runtime` 的模块函数，默认资产目录为 `$ENVWEAVER_HOME` 或 `~/.envweaver`。`TaskPool` 必须显式选择目录。

`RuntimeRef(backend, image, config)` 描述基础或保存的环境。`Instance` 是运行中的环境连接信息。二者都有 `to_dict()` / `from_dict()`；调用者无需处理 Docker 容器 ID 或 QEMU 连接细节。

## 2. Docker

需要可用的 Docker CLI 和 daemon，基础 Linux 镜像需要 Python 3、`/bin/sh`、`sync` 和 `chown`。镜像先由使用者取得；没有内置镜像构建流程。上传文件归属实例的默认用户，避免桌面用户无法编辑 root 所有的输入文件。

```python
ref = RuntimeRef("docker", "python:3.11-slim-bookworm", {
    "command": ["sleep", "infinity"],
    "cpus": 2,
    "memory": "2g",
})
instance = runtime.start(ref)
try:
    code, stdout, stderr = runtime.exec(instance, "python3 --version", timeout=30)
    runtime.upload(instance, "./inputs", "/work/inputs")
    runtime.download(instance, "/work/inputs", "./downloaded-inputs")
    saved = runtime.save(instance, "prepared-python")
finally:
    success, error = runtime.close(instance)
```

文件复制的 `remote_path` 是目标文件或目录本身，不是自动追加源名称的父目录。重复复制时覆盖同名文件，目录合并，不删除目标内额外文件。两种后端都采用这一语义。`exec` 返回非零退出码，不自动判定失败；超时抛异常。

Docker 配置：`command`、`env`、`user`、`memory`、`cpus`、`network`、`gpus`、`shm_size`、`python`（默认 `python3`）、`startup_timeout`，以及调用者负责的额外 `args` 列表。

`save` 执行文件系统同步、暂停提交，再导出 `image.tar`。引用包含镜像归档，不依赖 Docker 缓存永久存在。实例保存后继续运行。第一版拒绝带 bind mount / volume 的实例，避免把未进入快照的数据误当成已保存；外 Context 用复制注入。

Docker CLI 与 daemon、资产导出路径需位于使用者能正确访问的位置。第一版内置桌面端口发现面向本地 Linux daemon；远端 daemon / Pipeline 容器跨网络部署需要使用者配置连接与路由，本包不自动建立权限边界。

## 3. QEMU/KVM 与目标端连接

QEMU 后端面向 Linux，使用 `qemu-system-x86_64`、`qemu-img` 和 `/dev/kvm`。使用者准备 Linux 或 Windows 镜像，安装需要的驱动和自动启动的目标端服务：

```bash
# 在目标环境中安装；桌面运行还需图形会话和 PyAutoGUI 所需系统依赖。
pip install 'envweaver[guest,desktop]'
envweaver-guest --host 0.0.0.0 --port 8765
```

上述包名命令适用于发布后；当前可安装本仓库构建的 wheel，加上 `[guest,desktop]` extras。服务从 `ENVWEAVER_GUEST_TOKEN` 读取必须提供的凭证。Pipeline 环境也需有相同凭证；它不写入 Task 描述文件。令牌由使用者通过自己的镜像启动/配置方式提供。服务在目标系统中执行任意命令，应只向可信调用方开放；这只是连接设施，包不会替代 VM/容器隔离。

服务应在登录的桌面会话中运行，才能截图和操作该桌面。Linux X11 需正确设置 `DISPLAY`、必要的 `XAUTHORITY`、`XDG_SESSION_TYPE=x11`，并安装 PyAutoGUI 截图所需的 `scrot` 等系统工具。Windows 系统服务的 Session 0 不是交互式桌面。命令执行在 Linux 使用 `/bin/sh`，Windows 使用 PowerShell，命令文本不自动跨系统翻译。

```python
ref = RuntimeRef("qemu", "/images/windows.qcow2", {
    "memory": 8192,   # MiB
    "cpus": 4,
    "guest_port": 8765,
    "token_env": "ENVWEAVER_GUEST_TOKEN",
    "startup_timeout": 180,
    "shutdown_timeout": 120,
})
instance = runtime.start(ref)
runtime.exec(instance, 'Get-ChildItem C:\\Tasks')
```

每个实例使用独立 qcow2 overlay、QMP socket 和本地转发端口。默认 virtio 磁盘/网络，镜像需预装驱动；可用 `disk_interface`、`network_device` 调整。其他配置有 `binary`、`image_binary`、`machine`、`accel`、基础磁盘 `format`、只读 UEFI `firmware` 和可写 `nvram` 模板。

`save` 通过 ACPI 正常关机，导出独立 qcow2、固件和 NVRAM，然后重启原实例。实例对象继续有效，但进程、IPython 会话和桌面窗口不会保留。关机超时则保存失败，不强制断电后声称快照成功。基础镜像必须支持正常关机。第一版只管理一个系统磁盘，以及可选 firmware/NVRAM；额外数据盘和 TPM 状态不在本版保存范围。

## 4. 桌面工具与 pi

pi 是外部 Node.js 程序，Python 包不自动安装它。目前适配并测试 `@earendil-works/pi-coding-agent@1.3.0`：

```bash
npm install -g @earendil-works/pi-coding-agent@1.3.0
```

```python
result = agent.run(
    backend="pi",
    instruction="打开输入表格，按任务要求修改并保存。",
    workspace="./runs/task-1",
    tools=["shell", "ipython"],
    instance=instance,
    config={
        "base_url": "http://127.0.0.1:8000/v1",
        "model_name": "your-vision-model",
        "api_key": "provided-at-run-time",
    },
)
print(result.text, result.trajectory, result.session)
```

运行位置：pi、工具连接层和模型请求在 Pipeline 环境；`shell` 命令与 IPython cell 在目标实例。IPython 保留变量，可执行：

```python
import pyautogui as gui
gui.click(500, 300)
display(gui.screenshot())
```

文字及 PNG/JPEG 以实际内容返回模型，不只返回图片路径。工具会话内存不随 Runtime 保存。内置 IPython 工具默认每个 cell 最多 300 秒；超时重启 kernel 并报告变量丢失。它是参考 Mini-CUA 的最小实现，不包含 Mini-CUA 完整的异步 yield、输入释放恢复和轨迹导出体系。

对 Docker 桌面，基础镜像也需启动该服务。`RuntimeRef.config` 设置 `guest_port=8765` 后，后端自动分配独立端口并在启动时注入调用方的 `ENVWEAVER_GUEST_TOKEN`（可用 `token_env` 指定调用方变量名）。该注入值在 Docker 提交配置中清除，恢复时重新注入。已由使用者写入镜像磁盘、其他环境变量或文件的凭证无法自动识别；不要把模型凭证放进任务资产。

不传 `instance` 时默认使用 pi 的 `read/bash/edit/write`，操作 Pipeline 的 workspace；传 `instance` 时默认只提供目标 `shell`，可显式添加 `ipython`。不会把目标端 shell 名称悄悄映射成宿主机 bash。

`config` 还支持 `command`（pi 可执行文件或 argv 前缀）、`provider`、`model_name`、`system_prompt`、`thinking`、`session`、`timeout`、`env` 和原生 CLI `args`。`session=result.session` 可继续之前的 pi 会话，是否继续由 Pipeline 决定。默认使用临时 pi 配置目录；使用自己已配置的登录和模型目录时设置 `agent_dir`，不要同时传 `base_url`。服务地址配置还支持 `input`、`reasoning`、`context_window`、`max_tokens`。

不传 `base_url` 而直接提供 `api_key` 时，必须指定 `provider`，凭证只写入本次运行的私有临时配置，结束后删除。已有 `agent_dir` 使用自身凭证或 `config.env`，不会被适配层改写。

pi 的事件、图片内容与会话文件保存在 workspace 的 `.envweaver/<run-id>/`。超时或退出错误保留诊断文件并抛 `AgentError`。超时先终止 pi，再等待已开始的工具调用结束，避免返回后工具仍在改动环境；工具自身应有合理的终止条件。

自定义工具使用 `envweaver.tools.Tool(name, description, parameters, execute)`，回调返回含 `content` 的文字/图片结果。自定义工具的生命周期由调用者管理；由 Agent 适配层创建的 IPython 会话由适配层关闭。增加 Agent backend 只需实现 `run(...)` 并 `agent.register(name, adapter)`；首版未假装已经接通 Codex 或 Claude Code。

## 5. LLM 与 Verifier

```python
response = llm.call(
    base_url="http://127.0.0.1:8000/v1",
    model_name="your-model", api_key="provided-at-run-time",
    messages=[{"role": "user", "content": "选择最适合的候选项。"}],
)
```

`llm.call` 采用 OpenAI Chat Completions 协议，返回完整响应字典；支持文字/图片 messages 和可选 `output_schema`。不同提供方对图片、JSON Schema 的支持由提供方决定。HTTP/超时错误直接抛出，不自动重试。pi 自己调用模型，不需要经过此函数。

验证包放在独立目录，入口固定 `run.py`。示例见 [可执行验证器](../examples/coding/verifier/run.py)。

```python
score = verifier.run("./checker/run.py", instance)
```

框架使用当前 Python 解释器，以验证包为工作目录启动 `run.py --instance <临时文件>`。脚本通过 `runtime.connect(path)` 取得实例，并调用 `runtime.exec/upload/download`、Agent 或 LLM。它不会在目标 VM 里自动启动另一个验证主程序。

stdout 必须只有一个 JSON 对象，包含有限数值 `score`（0～1）；日志用 stderr。非零程序退出、超时（默认 300 秒，可用关键字 `timeout` 调整）、无效结果都抛 `VerifierError`。合法 `score=0` 才表示零分。

## 6. Task Pool

```python
task_ref = task.save(
    instance, "repair-spreadsheet",
    goal=[{"type": "text", "text": "修复表格"},
          {"type": "image", "path": "./reference.png"}],
    verifier="./checker/run.py",
    context=[{"source": "./inputs/sales.xlsx", "target": "C:\\Tasks\\sales.xlsx"}],
    metadata={"software": "Office", "source": "forum-post-url"},
)
refs = task.search(query="表格", filters={"software": "Office"})
saved = task.load(refs[0])
instance = runtime.start(saved.runtime, saved.context)
```

`name` 在 Pool 内唯一，重名或同名保存进行中抛 `TaskExistsError`。Goal 图片、整个验证包、原始 Context 都复制进任务目录，Runtime 单独归档并引用。外 Context 每次复制进实例，副本之间不共享修改。任务最终通过目录 rename 发布，保存失败不会出现可正常读取的半成品任务。

`query` 对名称、Goal 文字和 metadata 的文本做大小写不敏感子串搜索；`filters` 对 metadata 顶层字段做 AND 精确匹配。尚未加入向量检索、工作流调度或领域专属字段。

目录中的引用目前使用绝对路径。备份和搬迁需同时保留 Task Pool 与 Runtime 资产，并维持路径或显式调整引用；第一版未提供跨机器导入导出或自动垃圾回收。失败保存可能留下未被 Task 引用的 Runtime 资产；并发保存使用独占锁，进程被强杀后仅在确认该生产者已结束时手工清理其残留 `.lock`。

## 7. 示例和验证范围

```bash
uv run python examples/coding/pipeline.py
# 配置 MODEL_BASE_URL、MODEL_NAME、MODEL_API_KEY，且安装 pi 后：
uv run python examples/coding/pipeline.py --agent
```

示例安装 pytest、放入有 bug 的仓库、验证零分、保存初态、启动独立实例修复，再验证一分。默认用已知修复；`--agent` 才让 pi 执行修复。

```bash
uv run pytest
ENVWEAVER_TEST_PI=/path/to/pi uv run pytest tests/test_llm_agent.py
ENVWEAVER_TEST_DOCKER=python:3.11-slim-bookworm uv run pytest tests/test_docker_integration.py
# 准备好的 X11 镜像已有 IPython 依赖，或用 ENVWEAVER_TEST_KERNEL_WHEELS 提供离线 wheels：
ENVWEAVER_TEST_DESKTOP=your-desktop-image uv run pytest tests/test_desktop_integration.py
```

当前验证结果：本地 33 项测试通过（含真实 pi 进程、会话继续和模拟模型服务）；casia23 上另外 3 项真实 Docker 集成测试通过，覆盖任务保存/恢复、非 root 用户修改上传文件、X11 桌面的 IPython 截图及鼠标操作。wheel 构建及独立安装检查通过。没有调用真实收费模型来衡量解题能力。

QEMU 的 QMP 和快照顺序有协议测试，Windows 命令路由有测试；尚无可用 KVM/Windows 镜像完成虚拟机实测，因此不能把后端代码就绪等同于四种操作系统已经验收。
