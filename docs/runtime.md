# Runtime

Runtime 用统一接口启动、操作、保存和关闭沙箱，支持 Docker、QEMU 及自定义后端，可用于 Data Pipeline、Training、Evaluation。

一个 Runtime 可以创建和操作多个沙箱。

## Runtime：沙箱管理器 {#runtime_1}

::: envweaver.Runtime
    options:
      show_root_heading: false
      members: []

```python
from envweaver import Runtime

runtime = Runtime(root="./storage/runtimes")
```

<details markdown="1" id="ssh-config">
<summary>SSH 配置</summary>

`Runtime(ssh=...)` 配置服务器连接。

| 字段 | 用法与默认值 |
| --- | --- |
| `config_file` | 当前机器上的 SSH 配置文件路径；不填则使用 OpenSSH 配置 |
| `known_hosts_file` | 当前机器上的主机身份记录文件路径；不填则使用 OpenSSH 配置 |
| `identity_file` | 当前机器上的 SSH 私钥路径；不填则使用 OpenSSH 配置 |
| `host_key_alias` | 在身份记录文件中查找虚拟机或服务器时使用的名称；默认不指定 |

```python
runtime = Runtime(
    root="./storage/runtimes",
    host="ssh://worker@server",
    ssh={
        "config_file": "./experiment/ssh/config",
        "known_hosts_file": "./experiment/ssh/known_hosts",
        "identity_file": "./experiment/ssh/id_ed25519",
    },
)
```

</details>

## RuntimeRef：环境引用 {#runtimeref}

::: envweaver.models.RuntimeRef
    options:
      show_root_heading: false
      members: []

## Instance：运行实例 {#instance}

::: envweaver.models.Instance
    options:
      show_root_heading: false
      members: []

## Runtime.create：创建环境引用 {#create}

::: envweaver.Runtime.create
    options:
      show_root_heading: false

```python
ref = runtime.create(
    backend="docker",
    image="python:3.11-slim-bookworm",
    config={"command": ["sleep", "infinity"]},
)
```

## Runtime.start：启动环境 {#start}

::: envweaver.Runtime.start
    options:
      show_root_heading: false

```python
instance = runtime.start(
    ref=ref,
    context=[{"source": "./prepare", "target": "/work/prepare"}],
    setup={"command": ["python3", "/work/prepare/setup.py"], "timeout": 120},
)
```

<details markdown="1" id="connection-config">
<summary>连接配置</summary>

`create(connection=...)` 可填写 QEMU 的 SSH 用户名、Python 路径，或 Docker 的 VNC 密码环境变量名。其他连接设置传给 `start(connection=...)`。

`start` 中重新填写的设置会覆盖引用中的值，没填的保留原值。

`save` 保留实际使用的 SSH 用户名、Python 路径和 VNC 密码环境变量名；不保存服务器地址、端口、密码或私钥路径。

```python
ref = runtime.create(
    backend="qemu",
    image="/images/windows.qcow2",
    config={"disk_dir": "/data/vms"},
    connection={"ssh": {"user": "worker", "python": "python"}},
)
instance = runtime.start(
    ref=ref,
    connection={"ssh": {
        "config_file": "./experiment/ssh/config",
        "known_hosts_file": "./experiment/ssh/known_hosts",
        "identity_file": "./experiment/ssh/vm_key",
    }},
)
```

| 字段 | 用法与默认值 |
| --- | --- |
| `ssh.user` | 虚拟机中的 SSH 用户名，必填 |
| `ssh.python` | 虚拟机内的 Python 程序，默认 `python3` |
| `ssh.config_file`、`ssh.known_hosts_file`、`ssh.identity_file`、`ssh.host_key_alias` | 与上面的 [SSH 配置](#ssh-config) 相同，用于连接虚拟机 |
| `vnc.host` | Docker 桌面的 VNC 地址，截图时必填；需能从当前机器访问 |
| `vnc.port` | VNC 端口，默认 `5900` |
| `vnc.password_env` | 当前机器上保存 VNC 密码的环境变量名；默认不传密码 |

</details>

## Runtime.exec：执行命令 {#exec}

::: envweaver.Runtime.exec
    options:
      show_root_heading: false

```python
code, stdout, stderr = runtime.exec(
    instance=instance,
    command="python3 --version",
    timeout=30,
)
```

## Runtime.upload：上传文件 {#upload}

::: envweaver.Runtime.upload
    options:
      show_root_heading: false

```python
runtime.upload(
    instance=instance,
    source="./input.txt",
    target="/work/input.txt",
)
```

## Runtime.download：下载文件 {#download}

::: envweaver.Runtime.download
    options:
      show_root_heading: false

```python
runtime.download(
    instance=instance,
    source="/work/input.txt",
    target="./downloaded.txt",
)
```

## Runtime.screenshot：桌面截图 {#screenshot}

::: envweaver.Runtime.screenshot
    options:
      show_root_heading: false

```python
from pathlib import Path

image = runtime.screenshot(instance=instance)
Path("screen.png").write_bytes(image)
```

## Runtime.save：保存环境 {#save}

::: envweaver.Runtime.save
    options:
      show_root_heading: false

```python
saved_ref = runtime.save(instance=instance)
```

## Runtime.close：释放实例 {#close}

::: envweaver.Runtime.close
    options:
      show_root_heading: false

```python
runtime.close(instance=instance)
```

## 后端配置 {#backend-config}

<details markdown="1">
<summary>Docker config</summary>

| 字段 | 用法与默认值 |
| --- | --- |
| `command` | 容器启动时执行的命令。 例如：`["sleep", "infinity"]`；默认：`[]`，沿用镜像默认命令 |
| `env` | 向容器传入环境变量。 例如：`{"LANG": "C.UTF-8"}`；默认：`{}` |
| `user` | 容器运行用户。 例如：`"1000:1000"`；默认：镜像默认用户 |
| `memory` | 内存上限，使用 Docker 格式。 例如：`"4g"`；默认：不额外指定 |
| `cpus` | 可使用的 CPU 核数，可填小数。 例如：`2`；默认：不额外指定 |
| `network` | 网络模式或网络名称。 例如：`"bridge"`；默认：Docker 默认网络 |
| `shm_size` | 共享内存大小。 例如：`"1g"`；默认：Docker 默认值 |
| `gpus` | 允许使用的 GPU。 例如：`"all"`；默认：不请求 GPU |
| `python` | 容器内执行命令所用的 Python。 例如：`"/usr/bin/python3"`；默认：`"python3"` |
| `startup_timeout` | `docker run` 的超时秒数。 例如：`120`；默认：`120` 秒 |
| `args` | 额外的 `docker run` 参数列表，放在镜像名前。 例如：`["--hostname", "desktop"]`；默认：`[]` |

</details>

<details markdown="1">
<summary>QEMU config</summary>

```python
ref = runtime.create(
    backend="qemu",
    image="/images/windows.qcow2",
    config={
        "disk_dir": "/data/envweaver/disks",
        "save_mode": "incremental",
        "memory": 8192,
        "cpus": 4,
    },
)
```

| 字段 | 用法与默认值 |
| --- | --- |
| `disk_dir` | **必填**。启动虚拟机的服务器上的绝对目录，保存运行中的磁盘、控制文件和已保存的差分磁盘。例如：`"/data/envweaver/disks"` |
| `save_mode` | 保存方式：`"incremental"` 保留差分链（默认）；`"consolidated"` 合并中间差分层。 |
| `remote_python` | 远端已安装 EnvWeaver 的 Python 解释器，本地运行时忽略。 例如：`"/opt/envweaver/.venv/bin/python"`；默认：`"python3"` |
| `memory` | 虚拟机内存（MiB）。 例如：`8192`；默认：`4096` MiB |
| `cpus` | 虚拟 CPU 数量。 例如：`4`；默认：`2` |
| `startup_timeout` | 等待 SSH 就绪的最长时间。 例如：`180`；默认：`180` 秒 |
| `shutdown_timeout` | 保存前等待虚拟机正常关机的最长时间。 例如：`120`；默认：`120` 秒 |
| `disk_interface` | 磁盘接口，镜像内需要相应驱动。 例如：`"virtio"`；默认：`"virtio"` |
| `network_device` | 虚拟网卡型号，镜像内需要相应驱动。 例如：`"virtio-net-pci"`；默认：`"virtio-net-pci"` |
| `format` | `image` 所指基础磁盘的格式。 例如：`"raw"`；默认：`"qcow2"` |
| `machine` | QEMU 机器类型。 例如：`"q35"`；默认：`"q35"` |
| `accel` | QEMU 加速方式。 例如：`"kvm"`；默认：`"kvm"` |
| `firmware` | 只读 UEFI 固件文件路径。 例如：`"/images/OVMF_CODE.fd"`；默认：不额外配置 |
| `nvram` | UEFI 设置文件的模板路径。 例如：`"/images/OVMF_VARS.fd"`；默认：不额外配置 |
| `binary` | QEMU 可执行程序。 例如：`"/usr/bin/qemu-system-x86_64"`；默认：`"qemu-system-x86_64"` |
| `image_binary` | 磁盘处理程序。 例如：`"/usr/bin/qemu-img"`；默认：`"qemu-img"` |
| `args` | 额外 QEMU 启动参数，不能覆盖磁盘、进程和 QMP 控制配置。 例如：`["-vga", "std"]`；默认：`[]` |

</details>

## 扩展与连接

<details markdown="1" id="register-backend">
<summary>Runtime.register：添加后端</summary>

::: envweaver.Runtime.register
    options:
      show_root_heading: false

```python
runtime.register(name="my_vm", backend=MyVMBackend)
```

`MyVMBackend` 是你编写的后端类，需要实现以下方法。将注册后的同一个 Runtime 传给 TaskPool 和 Verifier：

::: envweaver.runtime.base.RuntimeBackend
    options:
      show_root_heading: false
      show_if_no_docstring: true
      show_bases: false
      heading_level: 4
      separate_signature: false
      show_signature_annotations: true

</details>

<details markdown="1" id="describe">
<summary>Runtime.describe：记录实例连接</summary>

::: envweaver.Runtime.describe
    options:
      show_root_heading: false

```python
runtime.describe(instance=instance, path="ssh://server/data/instance.json")
```

</details>

<details markdown="1" id="connect">
<summary>Runtime.connect：连接已有实例</summary>

::: envweaver.Runtime.connect
    options:
      show_root_heading: false

```python
runtime = Runtime()
instance = runtime.connect(path="ssh://server/data/instance.json")
```

</details>
