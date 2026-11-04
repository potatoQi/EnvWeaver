# Task

Task 将各种任务场景的任务组织成统一的任务包，通过 TaskPool 保存、读取和检索，供 Data Pipeline 交付任务、Training 和 Evaluation 使用。

## Task 的组成 {#package}

Task 是一个材料包，`task.json` 保存 Task 的元信息。

```text
<TaskPool.root>/<name>/
├── task.json                 # 必需：Task 元信息
├── goal/                     # 按需：任务说明中的图片
├── context/                  # 按需：任务输入文件或目录
└── verifier/                 # 必需：验证程序和材料
    ├── run.py                # 必需：验证入口
    └── <其他文件或目录>       # 按需：测试、标准答案、辅助文件等
```

## task.json {#manifest}

<div id="task-fields" class="table-responsive" markdown="1">

| 字段 | 含义 | 必填 / 默认值 |
| --- | --- | --- |
| `version` | Task 格式版本 | EnvWeaver 自动填写，当前为 `1` |
| `name` | 同一 Task Pool 内唯一的任务名称 | 是 |
| `goal[]` | 按顺序排列的文字和图片 | 是 |
| &emsp;↳ `type` | `text` 或 `image` | 是 |
| &emsp;↳ `text` | 任务文字 | `type=text` 时必填 |
| &emsp;↳ `path` | 图片路径，相对于 `task.json` | `type=image` 时必填 |
| `metadata` | 来源、标签等自定义信息 | 否，`{}` |
| &emsp;↳ `tags[]` | 标签字符串列表；每项去除首尾空白后为 1–100 个字符，与管理页面共用 | 否，`[]` |
| &emsp;↳ `<自定义字段>` | 其他可保存为 JSON 的值，字段名由 Pipeline 决定 | 按需 |
| `task_runtime_ref` | 任务环境的镜像引用 | 是 |
| &emsp;↳ `name` | 环境名称 | 否，使用 `image` |
| &emsp;↳ `backend` | 后端名称，如 `docker`、`qemu` | 是 |
| &emsp;↳ `image` | Docker 镜像名或 ID；QEMU 服务器上的磁盘路径 | 是 |
| &emsp;↳ `metadata` | 环境自定义 JSON 字典；`tags` 为环境标签字符串列表 | 否，`{}` |
| &emsp;↳ `config` | 后端启动配置 | 否，`{}` |
| &emsp;&emsp;↳ `python` | Docker：容器内的 Python 程序 | 否，`python3` |
| &emsp;&emsp;↳ `python_install_command` | Docker：缺少默认 Python 时执行的 shell 安装命令 | 否，内置安装流程，上限 300 秒 |
| &emsp;&emsp;↳ `command[]` | Docker：容器启动命令 | 否，使用镜像默认命令 |
| &emsp;&emsp;↳ `env` | Docker：传入容器的环境变量 | 否，`{}` |
| &emsp;&emsp;&emsp;↳ `<变量名>` | 环境变量的值，不填写真实密钥 | 按需 |
| &emsp;&emsp;↳ `user` | Docker：运行用户，如 `1000:1000` | 否，使用镜像默认用户 |
| &emsp;&emsp;↳ `memory` | 内存；Docker 如 `4g`，QEMU 单位为 MiB | 否；Docker 不额外限制，QEMU 为 `4096` |
| &emsp;&emsp;↳ `cpus` | CPU 数量，Docker 可填小数 | 否；Docker 不额外限制，QEMU 为 `2` |
| &emsp;&emsp;↳ `network` | Docker：网络模式或网络名称 | 否，使用 Docker 默认网络 |
| &emsp;&emsp;↳ `shm_size` | Docker：共享内存大小，如 `1g` | 否，使用 Docker 默认值 |
| &emsp;&emsp;↳ `gpus` | Docker：可用 GPU，如 `all` | 否，不请求 GPU |
| &emsp;&emsp;↳ `disk_interface` | QEMU：磁盘接口 | 否，`virtio` |
| &emsp;&emsp;↳ `network_device` | QEMU：虚拟网卡型号 | 否，`virtio-net-pci` |
| &emsp;&emsp;↳ `format` | QEMU：基础磁盘的格式 | 否，`qcow2` |
| &emsp;&emsp;↳ `machine` | QEMU：机器类型 | 否，`q35` |
| &emsp;&emsp;↳ `accel` | QEMU：加速方式 | 否，`kvm` |
| &emsp;&emsp;↳ `firmware` | QEMU：服务器上的只读 UEFI 固件路径 | 否，不额外配置 |
| &emsp;&emsp;↳ `nvram` | QEMU：服务器上的 UEFI 设置文件路径 | 否，不额外配置 |
| &emsp;&emsp;↳ `args[]` | 额外的 Docker 或 QEMU 启动参数 | 否，`[]` |
| &emsp;↳ `connection` | SSH 用户名、Python 路径或 VNC 密码环境变量名；自定义后端字段由后端约定 | 否，`{}` |
| &emsp;&emsp;↳ `ssh.user` | QEMU：虚拟机中的 SSH 用户名 | 使用 QEMU 时填写 |
| &emsp;&emsp;↳ `ssh.python` | QEMU：虚拟机内的 Python 程序 | 否，`python3` |
| &emsp;&emsp;↳ `vnc.password_env` | Docker：保存 VNC 密码的环境变量名称 | 否 |
| `context[]` | 做题需要的输入文件或目录 | 否，`[]` |
| &emsp;↳ `source` | 材料路径，相对于 `task.json` | 是 |
| &emsp;↳ `target` | 材料在做题环境内的目标路径 | 是 |
| `cwd` | 启动 Agent 时所在的目录，填写沙箱内的绝对路径，如 `/workspace/project`。目录可由 context 上传或 setup 创建，但启动 Agent 前必须存在 | 是 |
| `setup` | 复制 context 后执行的准备操作 | 否，省略时不执行 |
| &emsp;↳ `command` | 沙箱内执行的命令字符串或参数列表；准备完成后以退出码 0 结束 | 是 |
| &emsp;↳ `timeout` | 准备命令最长运行秒数，大于 0 | 是 |
| `verifier_runtime_ref` | 验证环境的镜像引用，字段同 `task_runtime_ref` | 是 |
| `verifier` | 验证程序和执行要求 | 是 |
| &emsp;↳ `entry` | 验证程序的路径，相对于 `task.json` | 是 |
| &emsp;↳ `timeout` | 验证程序最长运行秒数，大于 0 | 是 |
| &emsp;↳ `required_env[]` | 下游需提供的环境变量名称 | 否，`[]` |
| &emsp;↳ `requires_task_instance` | 验证程序是否需要任务沙箱 | 否，`true` |

</div>

<details markdown="1">
<summary>Docker 完整示例</summary>

```json
{
  "version": 1,
  "name": "sales-summary",
  "goal": [
    {
      "type": "text",
      "text": "打开 /work/sales.xlsx，按下面的样式制作各地区销售额汇总表。"
    },
    {
      "type": "image",
      "path": "goal/1.png"
    },
    {
      "type": "text",
      "text": "保留原始数据，将结果保存为 /work/summary.xlsx。"
    }
  ],
  "metadata": {
    "source": "https://example.com/forum/posts/123",
    "software": "LibreOffice",
    "tags": [
      "spreadsheet",
      "sales"
    ]
  },
  "task_runtime_ref": {
    "backend": "docker",
    "image": "sha256:<保存后的镜像 ID>",
    "config": {
      "command": [
        "sleep",
        "infinity"
      ],
      "env": {
        "LANG": "C.UTF-8"
      },
      "user": "1000:1000",
      "memory": "4g",
      "cpus": 2,
      "network": "bridge",
      "shm_size": "1g",
      "gpus": "all",
      "args": [
        "--hostname",
        "desktop"
      ],
      "python": "python3"
    },
    "connection": {}
  },
  "context": [
    {
      "source": "context/0/sales.xlsx",
      "target": "/work/sales.xlsx"
    },
    {
      "source": "context/1/setup.py",
      "target": "/work/setup.py"
    }
  ],
  "cwd": "/work",
  "setup": {
    "command": [
      "python3",
      "/work/setup.py"
    ],
    "timeout": 120
  },
  "verifier_runtime_ref": {
    "backend": "docker",
    "image": "registry.example.com/verifiers/spreadsheet:1.0",
    "config": {
      "command": [
        "sleep",
        "infinity"
      ],
      "env": {
        "LANG": "C.UTF-8"
      },
      "memory": "1g",
      "cpus": 1,
      "python": "python3"
    },
    "connection": {}
  },
  "verifier": {
    "entry": "verifier/run.py",
    "timeout": 300,
    "required_env": [
      "LLM_BASE_URL",
      "LLM_API_KEY"
    ],
    "requires_task_instance": true
  }
}
```

</details>

<details markdown="1">
<summary>QEMU 完整示例</summary>

```json
{
  "version": 1,
  "name": "windows-sales-summary",
  "goal": [
    {
      "type": "text",
      "text": "打开 C:\\Tasks\\sales.xlsx，按下面的样式制作各地区销售额汇总表。"
    },
    {
      "type": "image",
      "path": "goal/1.png"
    },
    {
      "type": "text",
      "text": "保留原始数据，将结果保存为 C:\\Tasks\\summary.xlsx。"
    }
  ],
  "metadata": {
    "source": "https://example.com/forum/posts/123",
    "software": "LibreOffice",
    "tags": [
      "spreadsheet",
      "sales",
      "windows"
    ]
  },
  "task_runtime_ref": {
    "backend": "qemu",
    "image": "/data/envweaver/disks/runtimes/<环境 ID>/disk.qcow2",
    "config": {
      "memory": 8192,
      "cpus": 4,
      "disk_interface": "virtio",
      "network_device": "virtio-net-pci",
      "format": "qcow2",
      "machine": "q35",
      "accel": "kvm",
      "firmware": "/data/envweaver/disks/runtimes/<环境 ID>/firmware.fd",
      "nvram": "/data/envweaver/disks/runtimes/<环境 ID>/nvram.fd",
      "args": [
        "-vga",
        "std"
      ]
    },
    "connection": {
      "ssh": {
        "user": "worker",
        "python": "python"
      }
    }
  },
  "context": [
    {
      "source": "context/0/sales.xlsx",
      "target": "C:\\Tasks\\sales.xlsx"
    },
    {
      "source": "context/1/setup.py",
      "target": "C:\\Tasks\\setup.py"
    }
  ],
  "cwd": "C:\\Tasks",
  "setup": {
    "command": [
      "python",
      "C:\\Tasks\\setup.py"
    ],
    "timeout": 120
  },
  "verifier_runtime_ref": {
    "backend": "docker",
    "image": "registry.example.com/verifiers/spreadsheet:1.0",
    "config": {
      "command": [
        "sleep",
        "infinity"
      ],
      "env": {
        "LANG": "C.UTF-8"
      },
      "memory": "1g",
      "cpus": 1,
      "python": "python3"
    },
    "connection": {}
  },
  "verifier": {
    "entry": "verifier/run.py",
    "timeout": 300,
    "required_env": [
      "LLM_BASE_URL",
      "LLM_API_KEY"
    ],
    "requires_task_instance": true
  }
}
```

</details>

## TaskPool：任务池管理器 {#taskpool}

::: envweaver.TaskPool
    options:
      show_root_heading: false
      members: []

```python
from envweaver import TaskPool
pool = TaskPool(root="./tasks", runtime=runtime)
```

## Task：任务 {#task_1}

<div class="table-responsive" markdown="1">

::: envweaver.models.Task
    options:
      show_root_heading: false
      members: []

</div>

## TaskPool.save：保存任务 {#save}

已有环境可直接复用 RuntimeRef；需要保留实例中的修改时，先调用 [Runtime.save](runtime.md#save)，再传入返回的引用。

<div class="table-responsive" markdown="1">

::: envweaver.TaskPool.save
    options:
      show_root_heading: false

</div>

```python
task_ref = pool.save(
    name="sales-summary",
    goal=[
        {"type": "text", "text": "修改表格，按图中的样式完成销售额汇总。"},
        {"type": "image", "path": "./reference.png"},
    ],
    metadata={"software": "LibreOffice", "tags": ["office", "spreadsheet"]},
    task_runtime_ref=task_runtime_ref,
    context=[
        {"source": "./sales.xlsx", "target": "/work/sales.xlsx"},
        {"source": "./setup.py", "target": "/work/setup.py"},
    ],
    setup={"command": ["python3", "/work/setup.py"], "timeout": 120},
    cwd="/work",
    verifier_runtime_ref=verifier_runtime_ref,
    verifier=checker,
)
```

## TaskPool.load：读取任务 {#load}

::: envweaver.TaskPool.load
    options:
      show_root_heading: false

<details markdown="1">
<summary>使用读取的任务</summary>

```python
task = pool.load(task_ref=task_ref)
```

**Docker：启动任务沙箱**

```python
task_instance = runtime.start(
    ref=task.task_runtime_ref,
    context=task.context,
    setup=task.setup,
)
```

**QEMU：指定磁盘目录并启动任务沙箱**

```python
ref = runtime.create(
    backend="qemu",
    image=task.task_runtime_ref.image,
    config={**task.task_runtime_ref.config, "disk_dir": "/data/task-runs"},
    connection=task.task_runtime_ref.connection,
)
task_instance = runtime.start(
    ref=ref,
    context=task.context,
    setup=task.setup,
)
```

服务器地址填在 `Runtime(host=...)`；QEMU 磁盘目录填在 `config.disk_dir`；虚拟机 SSH 私钥和 Docker VNC 地址通过 `start(connection=...)` 传入。

**启动验证沙箱**

Docker 直接使用任务中的验证镜像引用：

```python
verifier_instance = runtime.start(
    ref=task.verifier_runtime_ref,
)
```

QEMU 需要先补充磁盘目录：

```python
verifier_ref = runtime.create(
    backend="qemu",
    image=task.verifier_runtime_ref.image,
    config={**task.verifier_runtime_ref.config, "disk_dir": "/data/verify-runs"},
    connection=task.verifier_runtime_ref.connection,
)
```

```python
verifier_instance = runtime.start(ref=verifier_ref)
```

**执行 Verify**

```python
result = task.verifier.run(
    verifier_instance=verifier_instance,
    task_instance=task_instance,
    env={"LLM_BASE_URL": base_url, "LLM_API_KEY": api_key},
)
```

</details>

## TaskPool.search：检索任务 {#search}

::: envweaver.TaskPool.search
    options:
      show_root_heading: false

```python
refs = pool.search(
    filters={"software": "LibreOffice"},
)
```
