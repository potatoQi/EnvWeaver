# 设计说明

## 职责

EnvWeaver 提供基础接口，Agent 编写 Python Pipeline，将它们组合成任务生产流程。任务来源、生成策略、重试和训练反馈由 Pipeline 组织。

- Runtime 是沙箱管理器，一个 Runtime 可以创建和操作多个沙箱；Instance 代表一个沙箱。
- Agent 由 Pipeline 或下游自行调用，可通过 Runtime.exec 在沙箱内运行。
- Verifier 在调用者提供的验证沙箱中执行 Verify，返回得分。
- 模型调用由 Pipeline 或验证程序自行选择 SDK。
- Task Pool 保存任务描述和材料，镜像与磁盘由 Runtime 存储。

Pipeline 可以安装在 uv、conda 或 Docker 中。组件允许继续封装为 Coding、CUA 等领域工具。

## 代码组织

- `runtime/manager.py` 实现 Runtime；`runtime/__init__.py` 只导出 Runtime 和 RuntimeBackend。
- `_paths.py` 解析本地路径和 SSH 文件地址；`_storage.py` 负责文件传输及环境记录的读写。
- `_ssh_transport.py` 提供通用 SSH 调用；`runtime/_remote.py` 负责 QEMU 远程操作，`runtime/_ssh.py` 负责虚拟机内的命令和文件操作。

公开导入方式保持不变：`from envweaver import Runtime` 或 `from envweaver.runtime import Runtime`。

## 任务与保存

| 内容 | 保存什么 |
| --- | --- |
| Goal | 有序的文字和图片 |
| task_runtime_ref | 任务环境的镜像引用 |
| verifier_runtime_ref | 验证环境的镜像引用 |
| Verifier | `run.py` 所在目录、超时、所需环境变量名称，以及是否需要任务沙箱 |
| 内 Context | 已安装的软件和依赖，随环境保存 |
| 外 Context | 任务输入文件，按 `source → target` 在启动时复制 |
| Setup | 复制 Context 后执行的命令和超时 |
| metadata | Pipeline 自定义的 JSON 字典 |

外 Context 复制进实例后，会随 `runtime.save` 一起保存，不做额外排除。Task Pool 仍单独保存传入的外 Context 材料，允许它们同时存在于环境快照和任务包中。

`Runtime.start(ref, context, setup=...)` 依次启动沙箱、复制全部 Context、执行 Setup，成功后返回 Instance。Setup 只包含 command 和正数 timeout；命令在沙箱内执行，等待就绪的逻辑由脚本负责，退出码 0 表示准备完成。命令失败或超时会报错并清理本次实例。TaskPool 保存和读取 setup，旧 Task 省略时为 None；准备脚本通过 Context 提供或预装在镜像中。Setup 不写入 RuntimeRef，不改变后端接口。

保存前将实例准备到题目初始状态。命名和检索由 Task Pool 管理；search 仅按 filters 精确匹配 metadata 的第一层字段，多个条件同时满足才返回；省略 filters 或传空字典返回全部任务。同一 Task Pool 内名称唯一，重名报错；保存完成后才发布任务。

Task Pool 的 root 支持本地目录和 SSH 地址，与沙箱所在服务器独立。远端保存使用名称锁和同盘重命名，完整写入后才发布；检索只读取 task.json。读取远端任务时将材料下载到临时目录，Task 及其 Verifier 释放后清理本地副本。SSH 连接使用 Runtime.ssh 或已有 OpenSSH 配置。Runtime 的 `root` 可省略，省略时不写环境记录；填写时支持本地目录或 SSH 地址，与 `host` 独立，只保存环境记录：镜像 ID 或磁盘路径、启动配置和服务器地址。Docker 镜像由目标 Docker 服务存储；QEMU 的运行文件、差分磁盘和固件存入必填的 `config.disk_dir`，该目录位于启动虚拟机的服务器上。

Task 格式版本保持 1，只有用户明确要求时才修改。包版本保持 0.1.0。

TaskPool.save 写入 task.json 时，对做题环境和验证环境均清理内置后端的 EnvWeaver 操作配置：Docker 去掉 startup_timeout，保留镜像内的 python 路径；QEMU 去掉 save_mode、disk_dir、remote_python、binary、image_binary、startup_timeout、shutdown_timeout。Runtime.save 返回的完整配置和环境记录不变；自定义后端配置原样保留。下游使用 EnvWeaver 启动 QEMU 时自行补充 disk_dir，必要时指定工具路径。

RuntimeRef.connection 保存 SSH 用户名、Python 路径或 VNC 密码环境变量名。QEMU 支持 ssh.user、ssh.python；Docker 支持 vnc.password_env（变量名）。create 校验这些字段；start 按字段合并本次配置，同名字段以本次传入的为准，不修改原引用。save 保留实际使用的 ssh.user、ssh.python 或 vnc.password_env，不保存服务器地址、端口、密码或私钥路径。自定义后端在 create/save 中约定哪些连接设置可以保存。Task 只保存两个 RuntimeRef，不再单独保存连接配置。

文件地址统一使用普通路径或 `ssh://服务器/绝对路径`，用于 Runtime 和 TaskPool 的 `root`、`Runtime.describe/connect` 的 `path`、上传来源、下载目的地、Goal 图片来源、Context 来源、`Verifier.entry` 和 `Verifier.run(files=...)` 的源路径。SSH 使用已有配置和密钥，文件服务器需有 `python3`。公共文件模块负责地址解析和 SSH 传输；Runtime 后端只接收本地文件路径和实例内路径。SSH 文件经当前机器的临时目录中转，操作结束后删除临时副本。Task 保存时将 Goal 图片和 Context 复制进任务包，使用 TaskPool 的 Runtime.ssh 配置读取 SSH 来源。Verifier.entry 支持本地和 SSH；保存 Task、执行 Verify 时均复制 run.py 所在目录，SSH 使用该 Verifier 的 Runtime.ssh 配置。Task 内只记录归档后的相对入口路径，不保留材料服务器地址。当前支持本地和 SSH，其他协议会报错；云存储后续接入公共文件模块。

- Docker 使用原生镜像层共享，保存时不导出 tar。
- QEMU 每个实例使用独立差分盘；保存时关机，生成依赖基础盘的差分文件，再重启。基础盘须保留且不修改。
- QEMU 的 `config.save_mode` 默认为 `"incremental"`。重复保存同一个 QEMU 实例不加深依赖链；从已保存环境创建新环境再保存，会增加一层。
- QEMU 的 `config.save_mode="consolidated"` 将累计变化保存为直接依赖差分链最底层基础盘的新快照，不修改基础盘或中间快照。保存可能更慢；保留多个版本时，累计变化可能重复占用空间。Docker 使用原生镜像层，不配置 save_mode。
- Runtime.save 和 TaskPool.save 不接收保存模式；QEMU 按实例的 config.save_mode 保存，并在返回的 RuntimeRef 中保留此配置。Task 交付时去掉 save_mode。

`create` 检查目标服务器上的现有镜像或磁盘，返回 RuntimeRef，不启动环境或构建镜像。Docker 引用固定为镜像 ID；QEMU 引用使用绝对磁盘路径。`start` 接收 RuntimeRef 并启动实例，`save` 保存实例并返回 RuntimeRef。

启动位置只由 Runtime.host 决定；RuntimeRef 不保存服务器地址。重新使用已保存的环境时，需指定存有镜像或磁盘的服务器。Docker 连接不读取 `DOCKER_HOST` / `DOCKER_CONTEXT`，未指定服务器时使用本地 `default` context。

## 清理与迁移

`close` 删除运行实例及其连接记录，保留已保存的环境。删除 Task 也不会删除其引用的镜像或磁盘。

确认环境不再被任务或实例使用后：

- Docker：在 `metadata.json` 的 `host` 指定的服务器上，用 `tag` 执行 `docker image rm <tag>`，再删除 `root` 中的记录目录。不要用全局 prune 代替，Docker 不识别 Task Pool 的引用。
- QEMU：在目标服务器上用 `qemu-img info --backing-chain <disk.qcow2>` 检查依赖，先删不再使用的子盘，再删无人依赖的基础盘，最后删除 `root` 中的记录目录。

清理时暂停相关 Pipeline。迁移时需同时搬运 Docker 镜像或 QEMU 完整磁盘链，并更新地址与路径；只复制 `root` 不够。

TaskPool.save 默认 format="directory"，保存到 `root/tasks/<name>/` 并返回 task.json 地址；format="zip" 只保存 `root/archives/<name>.zip` 并返回 ZIP 地址。两种形式包含相同的元信息、Goal 图片、Context 和验证材料，不包含镜像或虚拟机磁盘，格式版本保持 1。search 读取两种形式的元信息并返回对应 task_ref，不解压 ZIP 材料。load 支持本地和 SSH；ZIP 解压材料在 Task 或其 Verifier 存活期间保留。两种形式共用任务名称和保存锁，重名均报错。save 先检查名称和 format，完成后发布一种形式，失败则删除临时材料；保存失败可能留下 Runtime 资产。异常退出留下的 `.lock` 文件，确认保存进程已退出后再删除。

## 实例连接

`start` 创建实例；`connect` 连接已有实例，两者返回 Instance，可执行命令、传文件、截图和关闭。只有 `start` 返回的实例可以保存；`connect` 返回的实例调用 `save` 报错。

`describe` 写出 version、backend、host 和后端连接字段，不写 root，也不复制整个 Instance。Docker 使用容器 ID，通过 inspect 读取配置；QEMU 使用 QMP 地址、SSH 连接信息和启动配置，CPU、内存、磁盘从 QMP 读取。QMP 不能完整还原启动命令，固件、设备型号及额外参数由启动程序记录。

连接地址、端口、SSH 私钥路径和 VNC 密码不放进 Task。连接记录可以保存 SSH 配置文件和私钥的路径，不包含私钥内容或 VNC 密码。Runtime.host 和 Runtime.ssh 不进入保存后的 RuntimeRef。

`Runtime.ssh` 配置服务器及 SSH 文件地址的连接；`connection.ssh` 配置虚拟机连接。两者支持 `config_file`、`known_hosts_file`、`identity_file` 和 `host_key_alias`，不填时使用 OpenSSH 配置。文件路径按调用时的当前目录解析，不修改用户的 SSH 配置，也不自动信任未知主机。实验密钥、SSH Agent 和共享连接策略写在专用配置文件中。

Instance 和连接记录保存服务器的 `host_ssh` 及虚拟机的 `ssh` 配置，只记录文件路径，不复制私钥内容；重新连接时可由 `Runtime.ssh` 覆盖服务器配置。QEMU 跳板使用服务器配置，登录虚拟机使用虚拟机配置。Docker 的 SSH 选项通过单次命令的临时包装程序传递，不修改进程全局 PATH，结束后删除包装程序。

Docker start 不允许挂载；connect 允许连接带挂载的容器，但不能保存。

Docker 通过原生命令操作；QEMU 通过 SSH 执行临时的 Python 标准库代码，通过 QMP 管理虚拟机和截图。虚拟机内需要 OpenSSH 和 Python，不需要 EnvWeaver。远端 QEMU 服务器仍需安装 EnvWeaver，虚拟机 SSH 认证从当前机器经跳板连接。

QEMU 连接目前限单个 qcow2 系统盘、可选 pflash、用户网络和 localhost SSH 端口转发。关闭外部虚拟机保留原磁盘，只删除 EnvWeaver 创建的实例目录。Docker close 删除目标容器。

## 验证与扩展

`Verifier` 接收 entry、runtime、timeout 和可选的 required_env、requires_task_instance。`run(verifier_instance=..., task_instance=...)` 在调用者提供的验证沙箱执行；requires_task_instance 默认为 False；为 True 时必须传 task_instance，为 False 时不能传。调用不匹配时，在传输材料和执行 Verify 前报错。Verifier 不启动或销毁沙箱，调用者使用 Runtime.start 和 Runtime.close 管理生命周期。

TaskPool.save 分别接收 verifier 和 verifier_runtime_ref。load 返回 task.verifier 和 task.verifier_runtime_ref；前者使用 TaskPool 的 Runtime 执行 Verify，后者用于启动验证沙箱。Task 和 task.json 统一用 task_runtime_ref、verifier_runtime_ref 表示任务和验证环境的镜像引用；验证程序要求放在 verifier 中，格式版本保持 1。Task 不保存 Runtime 对象或本次连接凭据。

每次 run 在验证沙箱创建独立验证目录，复制材料并执行 run.py。是否复用沙箱由调用者决定；Verifier 不恢复沙箱状态。files 使用 source、target 文件映射列表，target 为相对路径时以 run.py 所在目录为基准，为绝对路径时指向验证沙箱内的位置。files 和 env 是本次输入，不写入 Task；required_env 中的变量必须通过 run(env=...) 提供，缺少时报错，不读取调用进程的环境变量。

传入 task_instance 时，验证程序通过临时连接文件调用 exec、upload、download、screenshot。操作由调用者的 Runtime 转发，不向验证沙箱复制 SSH 凭据或挂载 Docker socket；该连接不能保存或关闭任务实例。

转发失败时向调用者抛错，沙箱和残留验证进程由调用者处理。timeout 从执行 run.py 开始计算，不包含启动和材料复制；执行失败、超时或结果格式错误均不计为零分。

Task 不依赖 Verifier 的内部转发方式。下游也可以自行启动验证环境、传入文件与变量、执行 `run.py` 并读取结果。

Runtime 后端实现 create、start、connect、describe、exec、upload、download、screenshot、save、close；`register` 接收类并传入 `root`、`host`，配置了 `ssh` 时一并传入。无需继承基类。

内置 Docker、QEMU 支持 SSH 远程运行；QEMU 通过远端已安装的 EnvWeaver 执行操作。自定义后端自行解释 host 并连接对应平台。

TaskPool 和 Verifier 共用调用者传入的 Runtime，保留其后端注册和连接配置。Verifier 通过内部辅助函数调用 exec 执行 Python 程序，解释器取 Instance.data.python，默认 python3；连接记录由后端 describe 生成并由 connect 读取。


## 验证范围

Verifier 已在 casia23 的真实 Docker 上完成制作与下游消费流程：保存 Task，制作端检查错误解和 Oracle，移动材料包并删除制作目录，再在下游启动新实例做题、评分。两端分别检查 0、0.5、1 分；隐藏测试注入和文件回传通过调用者转发。测试调用者为每次验证创建沙箱，并在结束后销毁。

不用 EnvWeaver 启动环境的原生下游也通过全部 13 组检查，包括依赖缺失、错误连接、缺少材料、非法评分、超时、实例隔离和挂载输入。测试通过写入固定答案检查评分，未调用真实模型。QEMU 验证环境的 Linux/Windows 路径和 Python 选择通过模拟测试，未实测虚拟机。

不传 instance 的 patch 流程也已在真实 Docker 上通过：用 git diff 导出提交，run.py 在验证环境中 git apply 后运行测试；使用独立容器得到 0、1、1、0 分，损坏或缺失 patch 抛异常。独立下游只用标准库和 Docker CLI 完成启动、做题和评分，run.py 不导入 EnvWeaver。两条端到端测试均通过，测试容器和保存的测试镜像已清理。

已有真实 Docker 任务保存/恢复、规则验证器、非 root 文件编辑验证。另有真实模型的小型 Coding 修复闭环记录。

2026-10-17 已验证 Docker 原生镜像层共享、任务保存/恢复及实例隔离；使用真实 qemu-img/qemu-io 验证了 raw、qcow2 基础盘的两种保存模式、快照不被后续写入改变、派生链与实例清理。consolidated 模式连续保存后仍只依赖基础盘，移除中间快照后数据保持完整。这些磁盘测试未启动虚拟机。

已通过 Runtime(host="ssh://casia23") 实测远端启动、文件传输、保存恢复、Verifier 跨进程连接和目标服务二进制传输；错误的 Docker 环境变量不会改变目标服务器。

QEMU 的真实 SSH 通路已验证命令、二进制文件传输、Verifier 和错误回传；目标服务运行在隔离测试容器中，未启动虚拟机。远端生命周期、本地记录与远端磁盘的分离另有自动化测试。casia23 当前无 QEMU 和 /dev/kvm。

尚缺真实模型驱动的完整 CUA 流程、模型型 Verifier、QEMU/KVM Linux/Windows 实测，以及 Agent 编写复杂可复用生产 Pipeline 的系统验收。

测试从仓库根目录运行；外部集成测试按需配置：

```bash
uv run pytest
ENVWEAVER_TEST_DOCKER=python:3.11-slim-bookworm uv run pytest tests/test_docker_integration.py
# 通过 API 指定远端，不使用 DOCKER_HOST。
ENVWEAVER_TEST_DOCKER_HOST=ssh://casia23 ENVWEAVER_TEST_DOCKER=python:3.11-slim-bookworm uv run pytest tests/test_docker_integration.py
# 独立下游交付与验证流程；PATCH_IMAGE 需预装 Python 3 和 Git。
ENVWEAVER_TEST_DOCKER_HOST=ssh://casia23 ENVWEAVER_TEST_DOCKER=python:3.11-slim-bookworm ENVWEAVER_TEST_PATCH_IMAGE=your-python-git-image uv run pytest tests/test_downstream_integration.py
# 已安装 qemu-img 与 qemu-io 时验证差分文件，无需启动虚拟机。
uv run pytest tests/test_qemu_disk_integration.py
# REF 为本地镜像配置 JSON；DISK_DIR 填启动虚拟机的服务器上的绝对目录，远程测试另设 ENVWEAVER_TEST_QEMU_HOST=ssh://server。
ENVWEAVER_TEST_QEMU_CONNECTION='{"ssh":{"user":"worker"}}' ENVWEAVER_TEST_QEMU_REF=/path/to/ref.json ENVWEAVER_TEST_QEMU_DISK_DIR=/srv/envweaver-test uv run pytest tests/test_qemu_integration.py
```

本次连接改造验证：casia23 上通过 4 项真实 Docker 集成测试，包含外部容器连接、保存恢复、资源限制保留，以及 Verifier 跨进程连接。原生 OpenSSH 的命令、二进制传输和超时已在服务器实测；VNC 客户端通过本地 RFB 测试服务验证截图。QEMU 连接、配置查询及拒绝保存外部实例通过模拟测试，尚未实测虚拟机启动。
