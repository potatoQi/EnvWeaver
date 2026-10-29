# 设计说明

## 职责

EnvWeaver 提供基础接口，Agent 编写 Python Pipeline，将它们组合成任务生产流程。任务来源、生成策略、重试和训练反馈由 Pipeline 组织。

- Runtime 是沙箱管理器，一个 Runtime 可以创建和操作多个沙箱；Instance 代表一个沙箱。
- Agent 为 Eval 统一适配不同 Agent 框架；独立 Pipeline 可通过 Runtime.exec 安装和执行原生 Agent。
- Verifier 在调用者提供的验证沙箱中执行 Verify，返回得分。
- 模型调用由 Pipeline 或验证程序自行选择 SDK。
- Task Pool 保存任务描述和材料，镜像与磁盘由 Runtime 存储。

Pipeline 可以安装在 uv、conda 或 Docker 中。组件允许继续封装为 Coding、CUA 等领域工具。

Eval 组件通过统一的 `eval(tasks, agent, ...)` 组织单题和并发评测，执行 Setup、Agent Run、Verify 与清理。tasks 接收单个 Task 或可迭代对象；返回值始终为按输入顺序排列的结果列表。agent 统一接收已配置的 Agent 对象，Eval 调用其 new() 为每道题和每次重试创建独立执行对象，原对象可以重复用于多个批次。配置由 EvalConfig 提供，concurrency 控制并发数；任务选择由调用者完成。评测时始终启用 Rich 面板显示状态，不参与执行或评分。

Agent 基类只保存配置并声明公开的 preflight、prepare、run、stop 接口，用户直接实现这些方法。AgentExecution 通过组合调用适配对象，负责调用顺序、停止等待、输入图片处理和最终结果校验；AgentOutput 负责原子结果快照、ATIF 校验与保存、JSONL 原始事件序号和产物同步，作为 output 参数显式传给 prepare、run；执行器和 Eval 从同一个 output.directory 定位产物与失败快照。公共层不设置适配对象的沙箱或目录属性。Eval 通过 AgentExecution 的生命周期方法取消执行，不直接修改它的内部事件。Task 保存与 Agent 执行共用 goal 格式校验，非文字或图片对象统一抛出 ValueError；内置适配还检查图片支持、原生参数冲突及 mini 的版本要求。Eval 在启动沙箱前检查 setup、context、验证器必需变量及输入文件可读性；模型凭据是否有效仍由模型服务验证。内置 CodexAgent、PiAgent、MiniSweAgent 在 Linux 沙箱运行 CLI：Codex/Pi 读取 JSONL 事件，mini-swe-agent 2.x 增量读取原生轨迹快照。原始事件保留在 data 中，不要求不同厂商具有相同内部字段。依赖类型和 stdout 是否包含 JSONL 事件由各适配类声明；公共 CLI 不根据 Agent 名称选择执行路径。Pi 的图片事件解析与归档保留在 Pi 适配代码中，按原生 type、data、mimeType 字段读取，不推测其他框架的字段。

Terminus2Agent 同样通过 CliAgent 在任务沙箱运行，复用依赖安装、进程监督、日志读取和停止接口。适配器固定使用官方 Harbor 0.25.0 的 Terminus 2；Harbor 没有接管已有沙箱的独立 Terminus CLI，因此由 `agents/adapters/terminus/runner.py` 提供沙箱内启动入口。启动入口只桥接本地命令、技能目录检查和 tmux 会话，记录官方轨迹及类型化异常；推理循环、响应解析、模型请求重试和上下文压缩均使用 Harbor 原实现。未预装依赖时通过 uv 安装独立 Python 3.12 和 Harbor，不要求当前机器安装 Harbor。原生参数通过 options 传入，认证变量只进入沙箱进程。

Terminus 收到公共监督器的停止信号后取消原生异步执行，并中断其 tmux 前台命令，保留后台题目服务。无法停止前台命令时不报告正常退出，由公共停止时限记录清理失败并关闭沙箱。原生轨迹通过原子快照持续归档；步数耗尽转为 STEP_LIMIT，其他异常按原生异常类型及继承关系分类，不在公共层匹配错误文案。

内置 CLI 每轮事件和日志同步后固定等待 2 秒，停止请求可提前唤醒等待。

Agent 基类统一保存传入的全部构造配置，包括 name、model、version 及任意自定义参数。config 属性返回配置的深拷贝；配置值支持 JSON 类型及 Path，在构造时检查可序列化性，拒绝非有限数字和循环引用。客户端、进程等运行对象在 prepare() 中创建。内置 CLI 的运行参数使用配置副本，运行中的修改不改变创建新对象和存档所用的配置。new() 用保存的配置重新调用构造函数，不复制锁、进程、取消状态或结果。内置 CLI 与自定义适配共用基类的对象创建和配置记录逻辑，不需要分别重写 new()、_identity()。Eval 拒绝 new() 返回原对象、非 Agent、配置不一致或重复使用同一个执行对象。

内置 CLI 在构造时接收 model、env、extra_args 等配置；EvalConfig 只管理评测执行。env 覆盖沙箱进程的同名变量，不隐式转发当前机器的环境变量。extra_args 按参数列表传给原生 CLI，复杂配置交给各 Agent 的原生配置文件解析；配置文件必须在任务沙箱内，EnvWeaver 不自动上传这些路径。

prepare 和 run 共用产物目录，支持本地路径和 SSH 地址；prepare 初始化结果快照，run 持续更新。内置适配统一将回复写入结果快照的 output。快照使用同目录临时文件写入后原子替换；未知用量保留为未知，不用零代替。输入图片和原生事件中识别出的图片附件保存到 files；原生事件与附件通过 artifact 事件关联。内置 CLI 的 env 不写入快照，mini 原生轨迹的 config 不存档。结构化记录共用文本脱敏逻辑，图片 Data URL 和带图片 MIME 类型的 Base64 数据保留原字节，避免短凭据误匹配图片编码。任意工具输出仍可能包含敏感内容，适配代码不承诺对所有原始事件进行脱敏。

Agent 和 Eval 的 SSH 产物目录通过公共文件地址解析与 SSH 传输操作。Agent 在本地缓存写入，日志、轨迹、附件或结果更新后同步变化的文件；先发布附件、日志和子轨迹，再发布主 ATIF 文件，最后原子替换结果快照。失败和中断仍同步已有产物；同步失败时异常附带保留的本地缓存路径。正常缓存随持有对象释放。传给自定义 Agent 的 `output.directory` 始终是可直接操作的本地工作目录；直接写入的其他文件在下一次结果更新或准备结束时同步。执行器接受 goal 中的 SSH 图片地址，通过 local_source 读取并保存到 files 后，将本地副本路径交给 Agent.run，不修改调用者的 goal。

Eval 在分配沙箱前独占创建输出目录，每道题分别发布结果，最终发布 results.json。Agent 和 Eval 的 SSH 产物目录统一使用系统默认 OpenSSH 配置，包括 ~/.ssh/config、默认密钥和 SSH Agent，不读取任务的 Runtime.ssh。SSH 输出目录只决定产物存放位置，不改变 Runtime.host 或沙箱内部路径的含义。读取 SSH 图片同样使用系统默认配置。

run 正常返回后运行 Verifier。Agent 阶段外层时限到期，或适配代码抛出 AgentError，以 ErrorType 表示达到上下文、步数、费用等限制时，Eval 将问题追加到 errors，等待 stop 成功后仍运行 Verifier。SSH 操作超时、认证或环境故障、用户取消不属于执行限制，跳过验证；Setup 总超时不验证，也不自动重试；不与连接或传输超时混为一类。无法确认 Agent 停止时向 errors 追加 stage=cleanup 的记录并关闭沙箱，不与仍在执行的 Agent 并发验证。errors 按发生顺序保存本次尝试的所有评测问题，无问题时为 []；验证也失败时追加验证错误，保留此前的限制记录。重试检查全部记录，只有存在问题且每条记录均允许重试时才重试；任一记录表示取消时，保留未完成状态，所有错误记录仅包含 stage、type、message：type 是 ErrorType 的字符串值，message 保存具体说明，清理操作名称也写入 message。清理错误的 stage 为 cleanup，仍阻止自动整题重试，但不影响已取得分数的有效性。run 未返回时从快照恢复已有产物；读取失败不妨碍关闭沙箱，也不覆盖执行记录。每次尝试使用 000001 等独立子目录，其中 agent/result.json 保存 Agent 快照，result.json 保存该次评测结果；顶层 results.json 保存结果列表。执行异常与验证零分分别记录。

Setup 和 Agent Run 在线程中执行并受阶段时限控制；超时后显式请求停止，而不是把线程等待超时当成进程终止。内置适配在沙箱内使用独立监督进程启动 CLI，持续回传日志。命令退出时记录日志末尾位置，读取至该位置后结束，不等待后台服务停止输出。监督器读取启动请求或启动 CLI 失败时也写入终止状态，避免调用方持续等待，并删除包含环境变量的请求文件；原子写入失败时也删除未发布的临时文件。正常退出不发送停止请求；中断时监督器仅向启动的 CLI 进程发送 SIGTERM，由 CLI 处理自身退出，不扫描或杀掉整个进程树。已启动的后台命令与服务保留在 Instance 中，验收后由 Runtime.close 统一回收；不承诺 CLI 自身不会清理工具或服务。stop 的等待上限为 30 秒；无法停止时记录清理错误并继续关闭沙箱。自定义适配必须实现协作停止；Python 无法强制杀死任意线程。

Eval 在 Runtime 后端返回实例后、上传 context 和执行 setup 前登记实例，初始化过程中超时也会关闭它。启动超时后才返回的实例由资源管理器关闭，不会再进入 Agent 准备阶段。清理时最多等待 Setup 线程退出 30 秒，确保通常的延迟启动在 eval 返回前完成回收；超出等待上限时向 errors 追加 stage=cleanup 的记录。若底层 start 永不返回且未提供实例句柄，Eval 无法立即定位该资源；清理依赖后端自己的启动超时与失败回收。Verify 不另设总时限，verify_timeout 只覆盖 Verifier 副本中的程序运行时限，不限制材料复制。批次中断会停止提交任务，并请求取消正在运行的评测。

所有 Agent 退出原因集中定义为字符串枚举 AgentStopReason，包括正常结束、执行限制、用户取消和执行异常；should_verify 统一决定是否进入验证。Eval 结果的 agent_stop_reason 保存其字符串值，未进入 Agent Run 时为 null；后续验证或清理失败不覆盖 Agent 的退出原因。AgentError 统一携带 ErrorType；is_limit 标明执行限制，stop_reason 将其映射为 AgentStopReason，正常完成直接返回。新增公共原因时扩展枚举及适配映射，Eval 无需逐项增加判断。已确认达到限制但无法细分时使用 OTHER_LIMIT，并在 message 和原生轨迹中保留具体原因；mini 的 LimitsExceeded 不猜测为步数或费用之一。未知错误不能归入 OTHER_LIMIT。

各 Agent 适配代码负责解释自己的原生字段、错误码和错误文字，输出携带 ErrorType 的 AgentError，主动中断可抛出 InterruptedError。Codex 在 codex.py 解码 error/turn.failed，包括消息中的 JSON 错误码和 CLI 的 HTTP 状态格式；Pi 在 pi.py 读取 stopReason、errorMessage 及其中的提供方错误码，HTTP 400 缺少标准错误码时识别消息中的上下文超限，认证和临时故障状态优先；mini 在 mini_swe.py 优先映射 info.exit_status 的异常类型，仅对 LiteLLM 的 BadRequestError 中已确认的模糊情况读取 exit 事件的 extra.exception_str。结构化类型优先；无法识别的原生故障归为 UNKNOWN，不扫描工具输出或普通回复。自动恢复后以最终状态为准。公共 CLI 层只管理进程、日志与完成状态，evaluation/failure.py 仅依据统一异常和执行阶段决定处理策略，不解释原生错误文字。

问题类型集中定义在 errors.py 的 ErrorType 中，`evaluation/failure.py` 负责异常映射及处理策略。适配器抛出 AgentError 指定 type；模型连接中断、请求超时、限流及临时服务错误使用 PROVIDER_TRANSIENT，无法归因于模型服务的连接中断和传输超时使用 TRANSPORT。只有类型为 PROVIDER_TRANSIENT 或 TRANSPORT 且阶段不是 verify、cleanup 时才允许整题重试；批次检查全部错误，默认 max_retries 为 0。认证、配置、验证错误和未知错误不自动重试。外层阶段时限到期使用 StageTimeout，统一映射为 TIMEOUT；底层 TimeoutError 映射为 TRANSPORT，不视为 Agent 达到执行限制。明确类型优先于阶段，同一个连接异常在 setup、verify、cleanup 都使用 TRANSPORT，处理策略再根据阶段决定是否允许重试。执行限制仍评分且不重试。Eval 用 score_valid 标记统计有效性：验证成功、Agent 正常结束或达到限制时为 True；后续清理错误不使已取得的分数失效，无效结果不转换成零分。清理失败阻止自动重试，避免旧 Agent 与新尝试重叠。

请求级重试由原生 CLI 维护会话与工具执行状态，EnvWeaver 不通过重启 CLI 重放已执行的工具。[Codex](https://github.com/openai/codex/blob/main/codex-rs/model-provider-info/src/lib.rs) 使用 provider 的 request_max_retries 和 stream_max_retries；[Pi](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/settings.md) 使用 retry 设置；[mini](https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/models/utils/retry.py) 使用模型查询重试器。保留调用者的原生配置，包括显式关闭重试；不同 CLI 版本的具体默认值由原生实现决定。CLI 退出后必须有终止事件或明确退出原因，单纯退出码 0 或已有最终文本文件不能证明响应完整。

EvalConfig.max_retries 默认 0，不自动重跑整道题；显式设置为正数时才启用整题重试，不影响 Agent 内部的请求重试；retry_delay 默认 1 秒，采用有上限的指数退避和抖动。所有任务统一通过 Agent.new() 创建重试对象。每次重试独立运行 Setup、Agent Run 和 Verify；第一次目录为 000001，之后为 000001/attempt-000002 等，旧结果不覆盖。results.json 每道题只保留最终结果，result.output_dir 指向对应尝试。

manifest.json 原子记录输入指纹、各次尝试和完成状态，本地与 SSH 使用同一存储流程。resume=True 跳过已结束的任务，未结束的任务从新沙箱重新运行；不恢复原 Agent 会话。只校验任务定义、顺序、输入文件内容与权限；输入可读性检查复用计算指纹的结果。已完成任务不创建 Agent，也不改写已有尝试和结果；未完成任务使用本次 Agent 和 EvalConfig。每次尝试在创建 Agent 前记录 configuration，包括 values.agent、values.eval 和完整配置的 fingerprint；配置写入失败时不启动沙箱。环境变量值、原生 CLI 参数和敏感字段脱敏，保留名称及配置指纹。续跑要求每次尝试均有完整 configuration 配置快照；缺失或格式无效时拒绝读取，不转换旧记录。基类 `_identity()` 统一返回 class 和 config，config 包含完整构造配置，内置 CLI 的 env、extra_args 与自定义参数均自动纳入。沙箱内原生配置和可变镜像标签不属于此指纹，调用者须保持一致。

批次存储通过 `.eval.lock` 的独占 flock 阻止并行写入。锁由产物服务器上的 Python 子进程持有，stdin 关闭或进程退出时释放；当前机器和 SSH 文件服务器均需支持 POSIX 文件锁。锁丢失时停止写入清单，锁文件保留不代表仍被占用。强制杀死评测进程后遗留的沙箱需先清理，续跑不会接管旧实例。

SSH Task 重新加载时会换临时解包目录，输入指纹使用材料内容，不依赖临时绝对路径。

内置适配支持现有 CLI 或按需安装：Codex 使用 @openai/codex，Pi 使用 @earendil-works/pi-coding-agent，mini 使用 mini-swe-agent 2.x。模型、认证、工作目录和额外 CLI 参数由调用者配置；第一版 mini 适配只接收文字 goal。当前 mini/LiteLLM 组合运行时需要 mcp 的 CacheableResult 类型，准备阶段检查并按需安装 mcp>=2.3,<3。

Docker 后端在容器启动后、第一次 Python 操作前检查解释器；缺少默认 python3 时，通过容器内的 apt-get、apk、dnf 或 yum 安装 Python 和 CA 证书。安装需要软件源访问和容器内 root 或免密 sudo 权限，总时限由 config.python_install_timeout 指定，默认 300 秒；失败时关闭本次容器。已有解释器不升级，自定义路径缺失时报错。此步骤属于 Eval 的 Setup 阶段，先于 context 上传、task.setup 和 Agent.prepare；不构建或替换任务镜像。

prepare 使用 Runtime.exec 执行安装。未指定 version 且已有 CLI 可用时直接复用；缺少 CLI 时自动安装，指定 version 时安装对应版本。需要安装 Codex/Pi 时检查 Node.js 22.19+ 和 npm：glibc Linux x64/arm64 下载官方 Node 22 发行包并校验 SHA-256，放入本次 Agent 临时目录；musl Linux 使用 apk，需要 root 或免密 sudo。CLI 使用 npm --prefix 安装到独立目录。mini 缺少 pip 时通过官方 get-pip.py 安装到独立目录，CLI 及依赖也通过 pip --target 保存，不修改系统 Python 包。PATH/PYTHONPATH 只作用于本次 Agent 进程。依赖下载、安装与 CLI 共用进程监督、停止和 prepare.log 日志；权限、网络或平台不满足时准备失败。

Node 归档逐项检查并解压，只接受普通文件、目录和位于安装目录内的链接；符号链接相对于自身父目录解析，硬链接相对于归档根目录解析。检查基于已解压的文件系统，拒绝通过先前链接绕出目录，并移除特殊权限位；不依赖各版本 Python 对相对链接不同的过滤行为。

2026-10-21 通过本地模拟 API 验证真实 Codex CLI 0.154.0（npm 包）、Pi 1.3.0、mini-swe-agent 2.4.6：18 项测试覆盖正常完成、请求等待和工具执行中停止、重复停止、认证失败、上下文限制、503 重试恢复、控制进程退出及服务保留。另有 4 项 Codex 原生重试测试覆盖流中断、429、503 和 401。这些检查使用本地进程传输，不调用真实模型。另有 6 项真实 Docker Eval 测试，三个 CLI 分别在正常结束和外层超时后由独立 Verifier 检查 HTTP/TCP 服务，均得 1 分，并检查容器回收。mini 错误用例将原生重试次数设为 2；mini 2.4.6 / LiteLLM 1.106.1 对部分认证与上下文错误会先重试，EnvWeaver 在其最终退出后分类，未修改原生重试策略。 后续补充 6 项真实 CLI 测试：工具已执行后的 503 和 HTTP 200 响应中途断开，三个框架均恢复执行，且已完成的工具没有重放。

## 代码组织

- 顶层保留公开接口导出、数据对象、异常定义、TaskPool 和 Verifier。
- `agents/` 保存 Agent 配置与公开适配接口（base）、公共执行流程（execution）、产物写入（output）、CLI 适配和沙箱内的进程监督脚本。
- `runtime/` 保存 Runtime、后端接口和 Docker/QEMU 实现。
- `evaluation/runner.py` 执行评测；`store.py` 管理批次记录和续跑；`failure.py` 分类执行故障。
- `_io/` 保存各组件共用的路径、文件传输、产物同步和 SSH 操作；`runtime/_ssh.py` 负责虚拟机内的命令和文件操作。
- `tests/` 按 agents、runtime、evaluation、io、task、verifier 归类；跨组件下游测试放在 integration，共用测试材料放在 fixtures。

公开导入方式保持不变，例如 `from envweaver import Runtime, TaskPool, CodexAgent, EvalConfig, eval` 和 `from envweaver.runtime import Runtime`。

## 任务与保存

| 内容 | 保存什么 |
| --- | --- |
| Goal | 有序的文字和图片 |
| task_runtime_ref | 任务环境的镜像引用 |
| verifier_runtime_ref | 验证环境的镜像引用 |
| Verifier | `run.py` 所在目录、超时、所需环境变量名称，以及是否需要任务沙箱 |
| 内 Context | 已安装的软件和依赖，随环境保存 |
| 外 Context | 任务输入文件，按 `source → target` 在启动时复制 |
| cwd | Agent 的沙箱内绝对工作目录，必填 |
| Setup | 复制 Context 后执行的命令和超时 |
| metadata | Pipeline 自定义的 JSON 字典 |

外 Context 复制进实例后，会随 `runtime.save` 一起保存，不做额外排除。Task Pool 仍单独保存传入的外 Context 材料，允许它们同时存在于环境快照和任务包中。

`Runtime.start(ref, context, setup=...)` 依次启动沙箱、复制全部 Context、执行 Setup，成功后返回 Instance。Setup 只包含 command 和正数 timeout；命令在沙箱内执行，等待就绪的逻辑由脚本负责，退出码 0 表示准备完成。命令失败或超时会报错并清理本次实例。TaskPool 保存和读取 setup，省略时为 None；准备脚本通过 Context 提供或预装在镜像中。Setup 不写入 RuntimeRef，不改变后端接口。

`Task.cwd` 以原字符串保存和加载，不按当前机器的路径解析。Evaluation 在 context 上传和 setup 完成后，将它传给 `Agent.prepare(cwd=...)`，检查目录可进入后再准备 Agent。工作目录属于 Task，Agent 构造参数不包含 cwd；Task.cwd 不接受 None，保存和加载时必须提供；旧任务缺少该字段时直接报错，不自动补默认目录。内置 Agent 在该目录启动 CLI；Agent.prepare 和 Agent.run 的 `cwd` 均为必填字符串参数，自定义适配代码通过该参数使用工作目录。该字段不改变 setup 或 Verifier 的工作目录，Task 格式版本仍为 1。

保存前将实例准备到题目初始状态。命名和检索由 Task Pool 管理；search 仅按 filters 精确匹配 metadata 的第一层字段，多个条件同时满足才返回；省略 filters 或传空字典返回全部任务。同一 Task Pool 内名称唯一，重名报错；保存完成后才发布任务。

Task Pool 的 root 支持本地目录和 SSH 地址，与沙箱所在服务器独立。远端保存使用名称锁和同盘重命名，完整写入后才发布；检索只读取 task.json。读取远端任务时将材料下载到临时目录，Task 及其 Verifier 释放后清理本地副本。任务池的 SSH 文件操作使用系统默认 OpenSSH 配置。Runtime 的 `root` 可省略，省略时不写环境记录；填写时支持本地目录或 SSH 地址，与 `host` 独立，只保存环境记录：镜像 ID 或磁盘路径、启动配置和服务器地址。Docker 镜像由目标 Docker 服务存储；QEMU 的运行文件、差分磁盘和固件存入必填的 `config.disk_dir`，该目录位于启动虚拟机的服务器上。

Task 格式版本保持 1，只有用户明确要求时才修改。包版本保持 0.1.0。

TaskPool.save 写入 task.json 时，对做题环境和验证环境均清理内置后端的 EnvWeaver 操作配置：Docker 去掉 startup_timeout，保留镜像内的 python 路径；QEMU 去掉 save_mode、disk_dir、remote_python、binary、image_binary、startup_timeout、shutdown_timeout。Runtime.save 返回的完整配置和环境记录不变；自定义后端配置原样保留。下游使用 EnvWeaver 启动 QEMU 时自行补充 disk_dir，必要时指定工具路径。

RuntimeRef.connection 保存 SSH 用户名、Python 路径或 VNC 密码环境变量名。QEMU 支持 ssh.user、ssh.python；Docker 支持 vnc.password_env（变量名）。create 校验这些字段；Runtime.start 统一递归合并本次连接配置，再传给后端；后端不重复合并。同名字段以本次传入的为准，不修改原引用。save 保留实际使用的 ssh.user、ssh.python 或 vnc.password_env，不保存服务器地址、端口、密码或私钥路径。自定义后端在 create/save 中约定哪些连接设置可以保存。Task 只保存两个 RuntimeRef，不再单独保存连接配置。

文件地址统一使用普通路径或 `ssh://服务器/绝对路径`，用于 Runtime 和 TaskPool 的 `root`、`Runtime.describe/connect` 的 `path`、上传来源、下载目的地、Goal 图片来源、Context 来源、`Verifier.entry` 和 `Verifier.run(files=...)` 的源路径。所有 SSH 文件地址使用系统默认 OpenSSH 配置、默认密钥和 SSH Agent，文件服务器需有 `python3`。公共文件模块负责地址解析和 SSH 传输；Runtime 后端只接收本地文件路径和实例内路径。SSH 文件经当前机器的临时目录中转，操作结束后删除临时副本。Task 保存时将 Goal 图片和 Context 复制进任务包，使用系统默认 SSH 配置读取来源文件。Verifier.entry 支持本地和 SSH；保存 Task、执行 Verify 时均复制 run.py 所在目录，SSH 文件操作使用系统默认配置。Task 内只记录归档后的相对入口路径，不保留材料服务器地址。当前支持本地和 SSH，其他协议会报错；云存储后续接入公共文件模块。

- Docker 使用原生镜像层共享，保存时不导出 tar。
- QEMU 每个实例使用独立差分盘；保存时关机，生成依赖基础盘的差分文件，再重启。基础盘须保留且不修改。
- QEMU 的 `config.save_mode` 默认为 `"incremental"`。重复保存同一个 QEMU 实例不加深依赖链；从已保存环境创建新环境再保存，会增加一层。
- QEMU 的 `config.save_mode="consolidated"` 将累计变化保存为直接依赖差分链最底层基础盘的新快照，不修改基础盘或中间快照。保存可能更慢；保留多个版本时，累计变化可能重复占用空间。Docker 使用原生镜像层，不配置 save_mode。
- Runtime.save 和 TaskPool.save 不接收保存模式；QEMU 按实例的 config.save_mode 保存，并在返回的 RuntimeRef 中保留此配置。Task 交付时去掉 save_mode。

`create` 检查目标服务器上的现有镜像或磁盘，返回 RuntimeRef，不启动环境或构建镜像。Docker 引用固定为镜像 ID；QEMU 引用使用绝对磁盘路径。`start` 接收 RuntimeRef 并启动实例，`save` 保存实例并返回 RuntimeRef。

启动位置只由 Runtime.host 决定；RuntimeRef 不保存服务器地址。重新使用已保存的环境时，需指定存有镜像或磁盘的服务器。Docker 连接不读取 `DOCKER_HOST` / `DOCKER_CONTEXT`，未指定服务器时使用本地 `default` context。

## 通信恢复

通信恢复统一使用 `_io/retry.py`：单次操作最多尝试三次，间隔为 0.5、1 秒；同线程内的嵌套调用共享次数上限，避免 Runtime、SSH 和 Agent 分别重试导致次数相乘。认证、主机密钥、权限和未知错误直接报告。通信诊断只解释 OpenSSH 或远端 Docker 连接报错，不扫描用户命令输出或模型消息。

可安全重复的状态、日志、原生轨迹读取及停止请求允许重连后重发；日志请求沿用原字节偏移，收到完整响应后才推进位置。等待重试时响应 Agent 取消，停止和停止确认仍能执行。普通命令及 Agent 启动请求只在确认连接尚未建立时重试；发送后回复丢失时不盲目重放，也不自动重新启动 Agent。SSH 文件读取、覆盖写入及发布允许重传同一份内容；当前是重传该次文件操作，不是字节级断点续传。SSH 解包逐文件通过临时文件和替换写入，目录仍按原接口合并，不承诺整个目录事务。创建目录、保存环境等非幂等操作不重放。

## 清理与迁移

`close` 删除运行实例及其连接记录，保留已保存的环境。删除 Task 也不会删除其引用的镜像或磁盘。

确认环境不再被任务或实例使用后：

- Docker：在 `metadata.json` 的 `host` 指定的服务器上，用 `tag` 执行 `docker image rm <tag>`，再删除 `root` 中的记录目录。不要用全局 prune 代替，Docker 不识别 Task Pool 的引用。
- QEMU：在目标服务器上用 `qemu-img info --backing-chain <disk.qcow2>` 检查依赖，先删不再使用的子盘，再删无人依赖的基础盘，最后删除 `root` 中的记录目录。

清理时暂停相关 Pipeline。迁移时需同时搬运 Docker 镜像或 QEMU 完整磁盘链，并更新地址与路径；只复制 `root` 不够。

TaskPool.save 将任务保存到 `root/<name>/`，返回 task.json 地址。目录包含元信息、Goal 图片、Context 和验证材料，不包含镜像或虚拟机磁盘，格式版本保持 1。search 读取各任务目录的元信息并返回 task_ref；load 支持本地和 SSH。SSH 材料下载到临时目录，在 Task 或其 Verifier 存活期间保留。保存前预留任务名称，重名或并发保存同名任务均报错；材料准备完成后原子发布目录，失败则清理临时材料和名称锁。保存失败可能留下 Runtime 资产。异常退出留下的 `.lock` 文件，确认保存进程已退出后再删除。

## 实例连接

`start` 创建实例；`connect` 连接已有实例，两者返回 Instance，可执行命令、传文件、截图和关闭。只有 `start` 返回的实例可以保存；`connect` 返回的实例调用 `save` 报错。

`describe` 写出 version、backend、host 和后端连接字段，不写 root，也不复制整个 Instance。Docker 使用容器 ID，通过 inspect 读取配置；QEMU 使用 QMP 地址、SSH 连接信息和启动配置，通过 QMP 检查运行状态、磁盘布局和热插拔内存，不为连接到的实例回填 CPU、内存启动参数。QMP 不能完整还原启动命令，固件、设备型号及额外参数由启动程序记录。

连接地址、端口、SSH 私钥路径和 VNC 密码不放进 Task。连接记录可以保存 SSH 配置文件和私钥的路径，不包含私钥内容或 VNC 密码。Runtime.host 和 Runtime.ssh 不进入保存后的 RuntimeRef。

QEMU 实例的 SSH 转发端口只保存在 `Instance.data["ssh"]["port"]`；启动、保存后的重启和 SSH 连接统一读取该字段。

`Runtime.ssh` 仅配置沙箱服务器连接；`connection.ssh` 配置虚拟机连接。两者支持 `config_file`、`known_hosts_file`、`identity_file` 和 `host_key_alias`，不填时使用 OpenSSH 配置。文件路径按调用时的当前目录解析，不修改用户的 SSH 配置，也不自动信任未知主机。实验密钥、SSH Agent 和共享连接策略写在专用配置文件中。

Instance 和连接记录保存服务器的 `host_ssh` 及虚拟机的 `ssh` 配置，只记录文件路径，不复制私钥内容；重新连接时，Runtime 未传 ssh 或传 None 则沿用记录的 host_ssh，显式传 {} 则使用系统默认配置，非空字典递归合并记录中的配置。本次配置不改写连接记录；虚拟机的 connection.ssh 保持独立。QEMU 跳板使用服务器配置，登录虚拟机使用虚拟机配置。Docker 的 SSH 选项通过单次命令的临时包装程序传递，不修改进程全局 PATH，结束后删除包装程序。

Docker start 不允许挂载；connect 允许连接带挂载的容器，但不能保存。Docker 操作统一读取 Instance 中的 Python 和 SSH 配置；connect 不构造用于保存镜像的 RuntimeRef。

Docker save 直接使用默认暂停容器进程的 commit 保存文件系统，不执行全局 sync，避免等待宿主机其他文件系统的 I/O。应用自身的缓冲由调用者在保存前写入文件；快照不包含进程内存。

Docker 通过原生命令操作；QEMU 通过 SSH 执行临时的 Python 标准库代码，通过 QMP 管理虚拟机和截图。虚拟机内需要 OpenSSH 和 Python，不需要 EnvWeaver。远端 QEMU 服务器仍需安装 EnvWeaver，虚拟机 SSH 认证从当前机器经跳板连接。

QEMU 连接目前限单个 qcow2 系统盘、可选 pflash、用户网络和 localhost SSH 端口转发。关闭外部虚拟机保留原磁盘，只删除 EnvWeaver 创建的实例目录。Docker close 删除目标容器。

QEMU 启动失败时，回收本次虚拟机及磁盘目录；无法确认虚拟机已停止时保留磁盘，并在原异常中记录清理失败原因。

QEMU 截图通过 QMP 读取 PPM，再用包依赖 Pillow 转成 PNG 字节；本地与远端使用同一实现，不要求 QEMU 7.1 新增的 PNG 输出参数。临时截图在读取结束或失败后删除。

## 验证与扩展

`Verifier` 接收 entry、runtime、timeout 和可选的 required_env、requires_task_instance。`run(verifier_instance=..., task_instance=...)` 在调用者提供的验证沙箱执行；requires_task_instance 默认为 True；为 True 时必须传 task_instance，为 False 时不能传。Task 文件省略该字段时也按 True 加载；不需要任务沙箱的验证器须显式填写 False。调用不匹配时，在传输材料和执行 Verify 前报错。Verifier 不启动或销毁沙箱，调用者使用 Runtime.start 和 Runtime.close 管理生命周期。

TaskPool.save 分别接收 verifier 和 verifier_runtime_ref。load 返回 task.verifier 和 task.verifier_runtime_ref；前者使用 TaskPool 的 Runtime 执行 Verify，后者用于启动验证沙箱。Task 和 task.json 统一用 task_runtime_ref、verifier_runtime_ref 表示任务和验证环境的镜像引用；验证程序要求放在 verifier 中，格式版本保持 1。Task 不保存 Runtime 对象或本次连接凭据。

每次 run 在验证沙箱创建独立验证目录，复制材料并执行 run.py。是否复用沙箱由调用者决定；Verifier 不恢复沙箱状态。files 使用 source、target 文件映射列表，target 为相对路径时以 run.py 所在目录为基准，为绝对路径时指向验证沙箱内的位置。files 和 env 是本次输入，不写入 Task；required_env 中的变量必须通过 run(env=...) 提供，缺少时报错，不读取调用进程的环境变量。

Eval 在每次尝试的 preflight 阶段，按 task.verifier.required_env 从调用进程的 os.environ 读取变量；config.verifier_env 补充或覆盖，同名值优先，额外显式变量也保留。只读取 Task 声明的名称，不复制整个进程环境；空字符串视为已提供。两处都缺失时，该题在创建沙箱前报配置错误，不影响其他任务。解析后的字典仅传给本次 Verifier.run，不修改 Task、EvalConfig 或 os.environ，也不将自动读取的值写入评测配置存档。

传入 task_instance 时，验证程序通过临时连接文件调用 exec、upload、download、screenshot。操作由调用者的 Runtime 转发，不向验证沙箱复制 SSH 凭据或挂载 Docker socket；该连接不能保存或关闭任务实例。

转发失败时向调用者抛错，沙箱和残留验证进程由调用者处理。timeout 从执行 run.py 开始计算，不包含启动和材料复制；执行失败、超时或结果格式错误均不计为零分。

Task 不依赖 Verifier 的内部转发方式。下游也可以自行启动验证环境、传入文件与变量、执行 `run.py` 并读取结果。

Runtime 后端实现 create、start、connect、describe、exec、upload、download、screenshot、save、close；`register` 接收类并统一传入 `root`、`host`、`ssh`，未配置 SSH 时传入 None。后端须接受当前 RuntimeBackend 定义的参数，create 和 start 始终接收 connection。无需继承基类。

内置 Docker、QEMU 支持 SSH 远程运行；QEMU 通过远端已安装的 EnvWeaver 执行操作。QEMU 远端后端不配置记录目录，返回实例后由调用端填写 Instance.root，并负责保存和删除记录。自定义后端自行解释 host 并连接对应平台。

TaskPool 和 Verifier 共用调用者传入的 Runtime，保留其后端注册和连接配置。Verifier 通过内部辅助函数调用 exec 执行 Python 程序，解释器取 Instance.data.python，默认 python3；连接记录由后端 describe 生成并由 connect 读取。


## 验证范围

Verifier 已在 casia23 的真实 Docker 上完成制作与下游消费流程：保存 Task，制作端检查错误解和 Oracle，移动材料包并删除制作目录，再在下游启动新实例做题、评分。两端分别检查 0、0.5、1 分；隐藏测试注入和文件回传通过调用者转发。测试调用者为每次验证创建沙箱，并在结束后销毁。

不用 EnvWeaver 启动环境的原生下游也通过全部 13 组检查，包括依赖缺失、错误连接、缺少材料、非法评分、超时、实例隔离和挂载输入。测试通过写入固定答案检查评分，未调用真实模型。QEMU 验证环境的 Windows 路径和 Python 选择另有模拟测试。

不传 instance 的 patch 流程也已在真实 Docker 上通过：用 git diff 导出提交，run.py 在验证环境中 git apply 后运行测试；使用独立容器得到 0、1、1、0 分，损坏或缺失 patch 抛异常。独立下游只用标准库和 Docker CLI 完成启动、做题和评分，run.py 不导入 EnvWeaver。两条端到端测试均通过，测试容器和保存的测试镜像已清理。

已有真实 Docker 任务保存/恢复、规则验证器、非 root 文件编辑验证。另有真实模型的小型 Coding 修复闭环记录。

2026-10-17 已验证 Docker 原生镜像层共享、任务保存/恢复及实例隔离；使用真实 qemu-img/qemu-io 验证了 raw、qcow2 基础盘的两种保存模式、快照不被后续写入改变、派生链与实例清理。consolidated 模式连续保存后仍只依赖基础盘，移除中间快照后数据保持完整。这些磁盘测试未启动虚拟机。

已通过 Runtime(host="ssh://casia23") 实测远端启动、文件传输、保存恢复、Verifier 跨进程连接和目标服务二进制传输；错误的 Docker 环境变量不会改变目标服务器。

QEMU 的远端生命周期、本地记录与远端磁盘的分离另有自动化测试。

2026-10-19 在 35 上使用 QEMU 6.2 实测 QMP 截图，TCG 和 KVM 均返回有效 PNG；测试使用虚拟显示设备，无需系统镜像。像素一致性、远程截图转发和转换失败后的临时文件清理通过自动化测试。

2026-10-19 在 35 上使用 QEMU 6.2/KVM 和 Ubuntu 24.04 完成真实虚拟机保存恢复、UEFI 启动与快照、Verifier、并发 Eval、Setup/Agent/验证失败与超时回收；Docker 同步覆盖这些 Eval 流程。QEMU 启动失败测试覆盖程序缺失、磁盘工具缺失、NVRAM 文件缺失和 SSH 未就绪。

本轮内置 Agent 安装检查：Codex CLI 0.163.0、Pi 1.3.0、mini-swe-agent 2.4.6 的 prepare 通过；mini-swe-agent 使用预下载的发行包。该轮安装检查未调用模型。

补测通过真实 Docker 桌面截图及 VNC 密码、SSH 产物同步与失败保留、SSH 任务池的目录交付和并发名称冲突。远端 QEMU 使用独立 OpenSSH 服务连接当前物理机，覆盖保存恢复与截图，不等同于另一台物理服务器。两轮各 6 道题、并发 3 的 QEMU Eval 均通过，并检查每轮资源回收。

逐项验收记录保存在 `tests/acceptance.json`；自动化覆盖与真实环境通过分别标注。真实模型驱动的 CUA 和复杂生产 Pipeline 尚未覆盖；模型型 Verifier 的后续实测见下方。Windows Server 2022 在 QEMU 6.2/KVM 上通过命令参数、中文路径、截图、超时进程树回收、Windows/Linux 交叉 Verifier、Windows Eval 及镜像保存恢复；使用确定性 Agent，未调用模型。测试采用 BIOS、IDE、e1000 和 Hyper-V 优化参数，具体配置见验收记录。

2026-10-19 补充真实模型专项：通过 GPU6、GPU7 的统一认证接口调用 Qwen3.8-27B，在 Linux Docker 中分组通过 13 项测试。覆盖 Codex、Pi、mini-swe-agent 的代码修改和评分，Codex/Pi 图片输入，4 道题并发 2，运行中停止并保留题目服务，超时与认证失败回收，以及模型 Verifier 的 0/1 分与产物保存。统一入口另通过认证拒绝、模型列表和双卡流式分流检查。模型评分曾在缺少明确 JSON 输出要求时误判，补充提示后复测通过；结构化输出不保证评分语义正确。

测试从仓库根目录运行；外部集成测试按需配置：

```bash
uv run pytest
ENVWEAVER_TEST_DOCKER=python:3.11-slim-bookworm uv run pytest tests/runtime/test_docker_integration.py
# 通过 API 指定远端，不使用 DOCKER_HOST。
ENVWEAVER_TEST_DOCKER_HOST=ssh://casia23 ENVWEAVER_TEST_DOCKER=python:3.11-slim-bookworm uv run pytest tests/runtime/test_docker_integration.py
# 独立下游交付与验证流程；PATCH_IMAGE 需预装 Python 3 和 Git。
ENVWEAVER_TEST_DOCKER_HOST=ssh://casia23 ENVWEAVER_TEST_DOCKER=python:3.11-slim-bookworm ENVWEAVER_TEST_PATCH_IMAGE=your-python-git-image uv run pytest tests/integration/test_downstream_integration.py
# 已安装 qemu-img 与 qemu-io 时验证差分文件，无需启动虚拟机。
uv run pytest tests/runtime/test_qemu_disk_integration.py
# 截图测试无需系统镜像；默认 TCG，有 KVM 权限时可设置 ENVWEAVER_TEST_QEMU_ACCEL=kvm。
uv run pytest tests/runtime/test_qemu_screenshot_integration.py
# REF 为本地镜像配置 JSON；DISK_DIR 填启动虚拟机的服务器上的绝对目录，远程测试另设 ENVWEAVER_TEST_QEMU_HOST=ssh://server。
ENVWEAVER_TEST_QEMU_CONNECTION='{"ssh":{"user":"worker"}}' ENVWEAVER_TEST_QEMU_REF=/path/to/ref.json ENVWEAVER_TEST_QEMU_DISK_DIR=/srv/envweaver-test uv run pytest tests/runtime/test_qemu_integration.py
# Eval 测试的镜像需安装 EnvWeaver；QEMU 引用需包含部署目录和 SSH 连接配置。
ENVWEAVER_TEST_EVAL_DOCKER=your-envweaver-image ENVWEAVER_TEST_QEMU_REF=/path/to/ref.json uv run pytest tests/evaluation/test_eval_integration.py
# 真实模型测试：镜像需安装 EnvWeaver 和三个 CLI；接口须能从沙箱访问。
ENVWEAVER_TEST_MODEL_IMAGE=your-agent-image \
ENVWEAVER_TEST_MODEL_BASE_URL=http://model-server:8009/v1 \
ENVWEAVER_TEST_MODEL_NAME=your-model \
ENVWEAVER_TEST_MODEL_KEY="$MODEL_API_KEY" \
uv run pytest tests/agents/test_model_integration.py
```

本次连接改造验证：casia23 上通过 4 项真实 Docker 集成测试，包含外部容器连接、保存恢复、资源限制保留，以及 Verifier 跨进程连接。原生 OpenSSH 的命令、二进制传输和超时已在服务器实测；VNC 客户端通过本地 RFB 测试服务验证截图。QEMU 拒绝保存外部实例另有自动化测试。


## 安装包缓存

DownloadCache 在当前机器保存原始下载文件，以来源地址和 SHA-256/SHA-512 校验值区分内容。命中时重新检查文件完整性；同一文件用进程锁合并并发下载，临时文件通过校验后原子发布。交付文件使用独立副本，不允许任务修改缓存。缓存超出容量时清理未被使用的旧文件；代理和软件源由调用者配置。

内置 CLI 的准备阶段通过独立文件请求目录传递带校验值的下载请求。pip 先加载原生 install 命令，再替换 Downloader 的下载步骤，保留原解析器和模块初始化顺序；npm 仍使用原解析器和安装器，只替换 pacote 的完整性标识 tarball 下载；Node 发行包使用官方校验表。缺少校验值时不接入公共缓存。不复用依赖解析结果、虚拟环境或任务产生的文件。mini-swe-agent 和 Terminus 2 的私有 pip 安装使用 --no-compile，不提前编译全部依赖的字节码；保留完整源码，由 Python 导入时按需处理。准备结束或失败时删除请求目录和辅助入口；run 不接入下载服务，也不从沙箱回收任何文件写入公共缓存。验证程序的依赖由任务适配代码在验证阶段显式准备，不提前提供给做题 Agent。

缓存职责分为三层：DownloadCache 管理校验、存储、并发复用和容量；PreparationDownloads 只通过显式传入的 Runtime、Instance、环境和取消信号交付文件，不读取 Agent 内部状态；安装器接入代码提供本次选中的文件及校验值。各 Agent 显式选择安装命令并声明需要的辅助脚本，公共准备流程只负责执行、上传和清理，不根据命令内容改写安装方式。uv 的 Python 和 wheel 下载接入集中在 _uv_install.py；Terminus 安装入口只组织依赖解析与安装。cache=False 不上传缓存脚本。

公共缓存不按阶段分目录；同一 URL 和校验值可跨准备与验证用途复用，文件必须仍通过完整性检查。隔离由交付范围和时间保证，不依赖目录命名。Codex、Pi、mini-swe-agent、Terminus2Agent 的 cache 参数只控制自身准备阶段；没有声明该配置的适配器不启动下载服务。Docker 和 QEMU 在准备期间保持一条标准输入输出连接，以二进制流交付下载文件，不逐包调用 Runtime.exec 或 Runtime.upload，也不开放网络端口。最多并发下载 8 个文件，文件完整写入后才发布响应；取消时中断下载和传输，准备结束或失败时关闭连接、回收辅助进程。自定义后端不提供流式连接时，使用 Runtime.exec 和 Runtime.upload 批量交付文件，保持现有后端接口。准备时只上传所用安装器的辅助文件。准备命令使用沙箱内有界等待查询退出状态，命令结束即可返回，不额外等待一个轮询周期。Terminus 2 在本次 Agent 目录内使用独立 tmux server，环境变量由新 server 继承，不修改任务已有的 tmux 会话，也不依赖 new-session -e。适配器固定关闭终端录像，环境桥接只保留命令执行和目录检查。Terminus 2 每次通过 uv pip compile 生成当前 Python 环境的 pylock，再通过 uv pip sync 安装；仅将唯一选中、带 SHA-256/SHA-512 的 wheel 交给公共缓存，锁文件和下载副本用完即删。uv 本身通过 pip 缓存安装；Terminus 的 uv venv 通过准备期间的沙箱回环地址获取 Python 发行包；只有 uv 实际请求的版本才调用公共下载接口。校验值来自当前 uv 版本对应的官方发布目录，或调用者的 UV_PYTHON_DOWNLOADS_JSON_URL。uv 仍决定版本、架构、是否下载和安装方式；已有 Python 不触发缓存。目录无法获取、URL 不匹配或缺少校验值时转回原下载地址。临时服务、发行包副本和线程随安装调用结束清理，不向宿主机开放端口。源码构建依赖和其他任意 uv 命令尚未接入公共缓存。Verifier.run 不自动缓存任意验证脚本的下载；Task 转换程序可用 DownloadCache 准备依赖，再把独立副本打包成验证材料。公共缓存、转换暂存文件、Task 材料和沙箱内安装目录是不同用途，公共缓存的容量上限不包括这些副本。

Docker config.python_prepare_command 只在默认 python3 缺失时执行一次，用于安装前的源配置等准备；不替换内置安装或缓存流程。config.python_install_timeout 统一限制准备、索引更新、缓存下载和安装的总时间。config.python_install_command 只在默认 python3 缺失时执行，先于 context 和 task.setup；未设置时使用内置安装命令。内置 APT 流程先更新索引，再用 apt-get --print-uris 获取本次安装的 URL、文件名、大小和 SHA-256。Runtime 通过同一个 DownloadCache 并发下载，批量复制独立副本到当前容器；APT 安装阶段始终使用该临时目录；未缓存的包仍由 APT 下载，临时目录在成功或失败后清理。软件源索引不缓存；私有地址、无校验值或仅沙箱可达的下载交还 APT。此缓存只用于 Runtime 自带的 Python 安装，不接管 task.setup、自定义安装命令或 Agent 做题期间的 apt-get。软件源和代理策略由调用者的准备命令或环境配置提供，核心不指定地区镜像源。


## 资产管理

管理服务放在 `envweaver.management`，浏览器界面放在仓库 `web/`。页面仅通过 HTTP API 操作，资产规则集中在管理服务；不在前端或核心 Runtime、Task 接口里重复实现。可选依赖通过 `management` extra 安装，包版本仍为 0.1.0，Task 格式版本仍为 1。管理 HTTP 接口由服务的 `/docs` 和 `/openapi.json` 提供说明，不加入核心接口导航。

服务启动不指定管理目录，不预设或恢复 Task Pool、Runtime Pool。首次使用时分别选择目录；当前目录保存在服务内存中，刷新、重新打开页面继续使用。切换目录更新对应类型的当前选择，服务重启后重新选择。服务在内存中保存已连接目录、文件索引、关联、删除确认和后台操作，重启即清空。没有 SQLite 或独立管理数据库。Task 按 `*/task.json` 发现，Runtime 按 `runtimes/*/runtime.json` 发现；支持本机和 SSH。SDK 不依赖管理服务，save 后页面轮询即可发现，编辑直接写回原文件。 Runtime Pool 不提供手动登记入口；环境通过 SDK 保存，管理界面提供查看、编辑和删除；复制仅保留 HTTP API，不提供页面入口。

Task 标签保存在 `task.json` 的 `metadata.tags`，Runtime 标签保存在 `runtime.json` 的 `metadata.tags`，均为该记录的 metadata；索引中的 tags 只是该字段的展示值。标签写入复用目录锁和文件指纹，只原子替换对应的 JSON 文件，不复制任务材料或环境文件。`.envweaver/task-pool.json` 保存标签颜色、未关联任务的标签及记录修改信息，不保存 Task 的标签关联；`.envweaver/runtime-pool.json` 同样只保存颜色、未关联环境的标签及记录修改信息，不保存 Runtime 的标签关联。同一目录同时作为两类 Pool 时使用不同文件。纯浏览不创建这些文件；修改时使用文件锁、内容指纹检查及原子替换，拒绝覆盖其他服务已经写入的内容。资产索引和完整 Task、RuntimeRef 不重复保存；文件是数据源。目录和资产 ID 根据类型、地址及子目录生成，复制整个 Pool 后标签保留而 ID 独立。

`pools.py` 负责位置和增量索引，`store.py` 负责内存查询、索引指纹及持久化注释，`_pool_files.py` 在本机和 SSH 服务器运行同一组文件操作。目录内的路径、inode、大小和修改时间识别材料变化。材料编辑使用名称锁和文件指纹，在源目录暂存后替换；中断留下的发布日志在下次访问时恢复旧目录或保留已发布目录。材料和注释文件不组成跨文件事务；失败后以实际源文件重新索引。服务内锁串行化索引和管理写入，后台操作最多并发两个。资产 revision 阻止旧编辑覆盖；仅从列表移除时保存文件指纹，文件变化或重新保存后再次显示。

列表通过 `/api/assets/page` 一次返回表格摘要和 Pool 筛选选项，共用一次扫描；完整 JSON 统一通过资产详情接口按需读取，不保留另一套完整列表查询或独立的后端、创建者选项接口。扫描仍检查目录内所有文件的指纹，未变化的记录跳过 JSON 解析和索引更新；正常的多条记录共用事务检查点，遇到无效记录时逐条隔离，保留有效记录。批量标签写入按 Pool 合并为一次文件操作，SSH 在远端执行同一批操作；每条记录仍独立加锁、检查指纹并原子替换，中途冲突保留已成功写入的文件，下次扫描恢复实际状态。

页面取消被新筛选替代的请求，并用请求序号拒绝过期响应。轮询在上一次完成三秒后继续，列表加载期间不新发轮询请求，隐藏页面时暂停，恢复可见时立即检查。未变化的标签列表保留 DOM，避免刷新重置焦点和局部滚动。

Task 的做题和验证环境引用自动建立两条关联。相同服务器上 backend、image、config、connection 相同的引用复用 Runtime 记录；name、metadata 不参与环境关联匹配；没有对应 Runtime Pool 记录的引用只在内存中用于关联查看，不创建默认环境池或环境文件。修改已被引用的 Runtime 的引用或服务器时拒绝写入，避免暗中改变多道题；用户可先复制再调整 Task。删除后保留关联信息，Task 显示环境记录已删除或实体缺失，编辑和复制不会隐式恢复这些依赖。

删除先生成十分钟有效的确认记录，列出选择项及受影响的 Task 和环境。实体删除时按 Docker daemon/image ID 或 QEMU 服务器/真实磁盘路径检查别名，并检查 QEMU backing-file 链。最终执行在服务写锁内重新检查目录状态及实体身份，变化时拒绝旧确认。失败项目保留记录和错误；Docker 删除不使用 force。预览和执行前同步所有已选择的 Pool，检查跨 Pool 依赖；有 Pool 不可访问或无法解析时拒绝删除，不把它当作没有依赖。未选择的目录不在检查范围内。外部实体操作和 Pool 注释不具备跨系统原子性；异常中断后需检查实际实体状态，不自动重放。

管理服务直接操作选中的 Pool 目录，不提供资产导入、导出或跨服务器迁移接口。后台删除队列与状态由 `jobs.py` 管理。

删除环境实体只删除镜像或磁盘，不顺带删除可能共享的固件及其他配置文件。

管理页面和 HTTP API 不设置登录或 Token 认证；服务默认监听 127.0.0.1，操作记录中的创建者、修改者使用服务进程的系统用户名。删除预览的确认标识仅用于对应影响清单、防止过期或重复执行，由页面自动传递，与登录凭据无关。

管理页面表头全选作用于全部筛选结果，不受分页限制；前端保存全选状态和排除 ID，`Selection.exclude_ids` 在统一查询层排除取消勾选的记录，批量标签与删除预览共用同一选择范围。

管理筛选使用 `Selection.backends` 列表，多个后端在统一查询层按 OR 匹配，并与标签、名称等条件按 AND 组合；列表、批量标签和删除预览统一使用该查询。后端菜单通过列表摘要中的 `backends` 获取当前 Pool 资产中的后端名称，新增自定义后端无需修改前端选项。

RuntimeRef.name 保存环境名称，不填时使用 image；Runtime.create 接收 name，Docker/QEMU 启动与保存快照时保留。Runtime Pool 的名称直接读取 runtime.json 的 name，改名也写入该字段；名称不作为记录标识或目录名，允许不同环境同名；编辑与复制统一使用 RuntimeRef 的名称校验。RuntimeRef.metadata 为用户自定义 JSON 字典，默认 {}。Runtime.create 接收 metadata，Docker/QEMU 启动与保存快照时保留；环境记录的 metadata.json 只记录服务器、镜像 tag 等位置说明，不另存展示名称，与 RuntimeRef.metadata 分开。

创建时间与最后修改时间分别使用 created_after/created_before、updated_after/updated_before 筛选，两个范围和其他条件按 AND 组合。列表、全选和批量操作共用查询层；页面将所选日期按浏览器时区转换为当天起止时间。

管理界面的创建者筛选按 Pool 查询名称，使用 Selection.creators 列表统一筛选查询结果与批量操作；多个名称之间取并集，与其他条件取交集。创建者和后端多选共用前端菜单组件。

管理页面不显示、筛选或编辑资产服务器位置；Selection 不接收 host 筛选。后端仍保留执行环境操作和维护关联所需的 host，不从 Pool 目录推断环境位置。

管理页面的列表摘要包含名称、标签、后端、创建者、创建时间、最后修改者和最后修改时间。列宽是浏览器偏好，按 Task/Runtime 类型分别保存，不写入 Pool 或资产文件。


详情请求同时返回所属 Pool 的标签定义，与列表响应共用标签读取逻辑，不保留独立标签查询入口；API JSON 使用 gzip 压缩，完整资产与关联数据仍保留，文件下载不压缩。目录指纹使用 scandir 获取文件属性，路径顺序与指纹字段保持一致；每次请求仍同步文件变化。详情内的 Task JSON 在展开时高亮，文件列表与图片独立加载，图片最多四个并发请求；详情使用本次请求的 AbortSignal 判断是否仍有效；切换或关闭时中止该信号，忽略已到达但尚未渲染的过期响应。


## ATIF 轨迹

ATIF 固定为 Harbor v1.8，Schema 来源提交 `509b119db0d97b141dcd9c80f4f7ab861c47c2d6`，与 Task 格式版本和包版本独立。升级时同时更新 Schema、转换器和回归样本；不在运行时下载协议。Schema 保留上游许可证，补充检查步骤序号、媒体类型、工具结果及子轨迹关联。

`agents/trajectory` 只处理存储、校验、媒体归档和原生文件采集。每个 `agents/adapters/<agent>/trajectory.py` 解释自身框架的数据；公共层不识别厂商消息或推测工具协议。`logs/events.jsonl` 保存执行事件，`logs/native` 保存采集的原生记录，`artifacts.trajectory` 指向 `trajectory.json`。新增适配器通过 `AgentOutput.save_trajectory` 发布 ATIF，并在模型调用边界保存上下文。 采集器显式接收 Runtime、Instance、Python、产物目录对象和脱敏函数，不读取 Agent 的私有属性。转换器返回 ATIF 文档；独立子轨迹及续接轨迹通过 `save_trajectory(related=...)` 统一校验、写入并登记，先保存关联文件，再保存主轨迹。 原生 JSONL 只按 LF 分隔记录，保留字符串中的 Unicode 分隔字符；并发写入留下的半个 UTF-8 字符和未完成记录等待下一次快照。Pi 无法识别的压缩响应保留原文，并标记 `response_conversion_missing`。

Codex 使用 rollout trace v2 的请求、响应、工具结果和线程关系；Pi 通过扩展采集 provider request、消息及压缩事件，并记录 fetch 的请求正文和原生响应以覆盖压缩请求；mini 在沙箱入口记录 LiteLLM 调用并结合原生消息；Terminus 保留 Harbor 的 raw_content 和 linear_history 轨迹，补充 LiteLLM 上下文。LiteLLM 快照是调用参数，不冒充供应商在服务端展开后的提示词；Pi 优先保存 fetch 请求正文，无法观察自定义传输时保留 before_provider_request 快照，不推测后续改写。

每次请求独立保存，不能把最终完整历史当作早先步骤的输入。重试和失败请求也保留；无法可靠匹配的调用在缺失说明及原生记录中保留，不按相似文字猜测。Pi 和 Terminus 根轨迹的 extra.model_calls 列出捕获的全部调用上下文及结果，包括传输重试或尚未匹配到标准步骤的调用。加密压缩状态、隐藏推理和供应商内部上下文不能恢复为训练文本；可见推理与加密原文分别保留。模型服务未提供的 token IDs、logprobs 不补造。

Codex 中断的推理保留原生部分响应中的文字、推理和工具调用，并标记 `response_status=failed`、`response_incomplete=true`；不把它当作完整回复。工具调用 ID 在单个步骤内唯一，结果引用同一步骤的调用；不同推理步骤可以复用 ID。Codex 根据工具开始事件记录所属推理，将结果关联到该步骤，不依赖工具结束与模型完成事件的到达顺序；尚无步骤的线程记录为 pending_thread_ids，等待后续快照补齐。Mini 对未变化且已完整归档的快照跳过重复转换，成功保存后才更新快照标记；原生文件未写完或媒体缺失时继续重试；Mini 按原生 assistant 消息中的工具调用及出现顺序关联结果，同一 ID 跨轮复用时不串到其他步骤；请求上下文和最终快照使用同一关联逻辑，保留工具结果的 extra 信息；上下文重写后重复出现的同一次工具结果不重复追加；原生快照尚未写完时，保留上次有效快照中的工具结果，并继续导出新采集的模型调用。

子 Agent 保持独立轨迹及上下文，通过原生线程或 Harbor 的引用关联。任意工具自行启动的外部 Agent，若未输出可识别的调用与子轨迹，不推断其内部轨迹。Mini、Terminus 当前仍只接受文字 goal；多模态转换不代表为原框架增加新的输入能力。ATIF 到训练框架的转换不属于 EnvWeaver。

## Traj Pool

SDK 通过 TrajPool 指定目录和归档条件，Eval 调用 save 自动归档；列表与详情由管理服务直接读取文件。记录目录以执行 ID 命名，result.json 保存评测结果，agent/ 保存轨迹及附件。结果中的 started_at、ended_at 为 UTC 时间，duration_seconds 为耗时秒数，configuration 为脱敏配置；Eval 返回的 traj_ref 指向池内记录。池内结果不保存 output_dir 或 traj_ref，Agent 产物路径相对于该记录的 agent/ 目录。

`trajectory/pool.py` 归档独立 rollout，复用 Eval 结果和 Agent 产物，不复制 Task、环境或引入任务指纹、采样与重试关联。Eval 在执行结束并保存结果后调用 TrajPool；原始评测目录保留排查和续跑产物。每次执行具有随机 UUID，重复归档以该 ID 定位原记录，不覆盖已编辑的 metadata。成品判断复用 AgentStopReason 和 ErrorType：已有 ATIF、有效评分、正常完成或执行限制、错误仅含 Agent 阶段的执行限制。任何清理故障均不作为成品；上下文完整性仍由 ATIF 表达，不以分数推断。

归档仅复制该次执行的 result.json 和 agent 目录，不复制嵌套的其他尝试。临时目录准备完成后在存储服务器原子发布，读者看不到半份记录；本地和 SSH 使用相同目录结构。写入采用每个 ID 独立的进程锁，同一 ID 等待已有写入完成后返回原记录，不同记录可并发保存。归档失败保留 Eval 结果并上报，续跑先补归档已有结果，再决定是否需要执行。配置沿用 Eval 的脱敏快照，池内不保存配置指纹。Task 关联按 task_ref 地址匹配，仅代表原始来源，不承诺源文件内容未变化。

管理服务扫描 result.json 建立可重建索引，Traj Pool 独立维护标签颜色，记录标签直接修改 metadata.tags；轨迹和评分保持只读。按 Task 名称、Agent、模型、分数、退出原因、完成情况、标签和时间筛选。详情按需读取 ATIF、上下文、日志和图片，导出包含完整记录目录；删除 Task 时预览已连接 Traj Pool 中的来源关联，删除轨迹不会删除 Task 或环境。

每次 rollout 创建独立的空 metadata，不继承 Task 的 metadata 或标签。管理页面不提供轨迹详情编辑入口，标签通过列表操作管理；不增加 SDK 参数；Agent 提供的模型、用量等信息继续保存在 agent_result.metadata。

Traj Pool 列表按 task_ref 分组，默认折叠，展开后读取该 Task 的 rollout。不同源路径即使同名也分开；缺少 task_ref 的记录单独成组。筛选先作用于 rollout，再统计分组和分页；外层按 Task 分页，组内分批加载，跨页全选仍选择所有匹配的 rollout。分组不依赖源 Task 是否仍存在，也不要求先连接 Task Pool。

每个 Task 分组的复选框选择该组全部匹配轨迹，包括折叠或尚未加载的记录；部分选中时显示半选状态。前端保存分组键和单条记录的选择例外，不为全选下载组内记录。`Selection.task_groups` 与 `ids` 取并集，`include_all` 选择全部匹配记录；`exclude_task_groups` 排除整组，`ids` 可重新选入其中的记录，`exclude_ids` 始终优先排除。筛选条件仍作用于最终选择范围，批量标签、导出和删除共用此规则。

轨迹使用 Reader 视图：消息、推理、工具调用和结果按记录顺序展示，整个区域和单条长内容分别限制最大高度并滚动。面板四周留白可直接滚动整条轨迹；消息到达上下边界后，滚动交给轨迹面板；轨迹到达上下边界后，继续滚动外层详情面板。请求上下文文件不显示为顶部链接，仍保留在产物文件中。子 Agent 的入口放在对应步骤，点击后以同样样式打开弹窗；内嵌引用按当前父轨迹内的 trajectory_id 解析。缺少调用位置的子轨迹单独折叠保留，不推断其发生位置。明确记录的上下文压缩显示标记，有压缩子轨迹或摘要时提供查看入口；不从普通消息推断压缩。continued_trajectory_ref 指向的后续步骤在滚动时接入同一视图，跳过 is_copied_context 标记的重复上下文；循环引用停止加载并提示，加载失败可重试，关闭视图取消请求。轨迹详情在信息表内通过“关联 Task”按钮跳转到源任务，汇总指标读取当前 ATIF 的 final_metrics，在顶部属性区以 JSON 展示。完整评测结果通过产物文件列表中的 result.json 查看。产物列表中的文本文件按原文展示，不自动解析为轨迹视图；原始 ATIF 保留全部扩展信息。


轨迹列表的耗时、有效分数、Agent 退出原因和错误数量直接读取 Eval 结果；错误详情点击后加载，展示 stage、type 和 message，不因错误列表非空就隐藏有效分数。Token 汇总集中在 trajectory/usage.py，管理索引首次扫描或记录变化时调用，本地和 SSH 共用实现，翻页不重复读取 ATIF，也不改写原始产物。

每个 ATIF 文档优先使用 final_metrics.total_prompt_tokens 和 total_completion_tokens；没有该项总数时，累加 Agent 步骤的 metrics.prompt_tokens 和 completion_tokens。包含 is_copied_context 的文档仅统计未复制的模型步骤，不使用可能包含复制部分的文档总数；llm_call_count=0 的步骤不计入。输入用量包含缓存 Token，不重复加 cached_tokens。子轨迹和续接文件均参与汇总，同一外部文件只访问一次，内嵌 ID 在父轨迹内解析，嵌入和文件双重引用优先采用嵌入表示。缺少某项用量时该项显示未知；缺失引用、采集缺失或存在无法对应到步骤的调用时，两项总数均显示未知，不估算、不把部分用量当作完整总数。
