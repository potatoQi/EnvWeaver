# Eval

Eval 按统一的 Setup、Agent Run、Verify 流程评测 Task，并保存评测结果、轨迹和日志。

进行一道题的评测需要 [task](task.md#task_1)、[agent](agent.md) 和 [config](#config)。

<details markdown="1">
<summary>流程伪代码</summary>

```python
import json
import os
import warnings
from copy import copy, deepcopy
from dataclasses import replace
from envweaver.agents.execution import AgentExecution
from envweaver.evaluation.store import inspect_task
from envweaver.evaluation.failure import stop_reason
from envweaver.evaluation.runner import _error_record
from envweaver.errors import AgentStopReason

# 下文“阶段时限”到期时抛出 StageTimeout；底层操作的 TimeoutError 不视为阶段到期。

def evaluate(task, agent, config, output_dir):
    execution = AgentExecution(agent.new())  # 为本次尝试创建独立 Agent，并管理其执行流程。
    # 此处 output_dir 是当前机器上的工作目录。
    # 每次尝试单独登记由 start 启动的实例，使用原 Runtime 的后端。
    runtime = 本次尝试的运行环境(task.verifier.runtime)

    # 先建立结果记录，便于中途失败时仍能保存任务信息和错误。
    result = {
        "name": task.name,           # 任务名称。
        "task_ref": task.task_ref,   # 任务在 Task Pool 中的地址，用于追溯和重新加载。
        "metadata": {},  # 本次 rollout 的自定义信息。
        "agent_stop_reason": None,  # Agent 退出原因；尚未运行时为 None。
        "agent_result": None,       # agent.run 返回的结果，或失败后读取的已有结果快照。
        "verification_result": None, # 验证程序返回的 JSON，包含 score 及其他评测信息。
        "score_valid": False,       # 该次得分是否可以计入统计。
        "errors": [],   # 评测过程中的问题，按发生顺序记录。
    }

    task_instance = None      # Agent 执行任务的沙箱实例。
    verifier_instance = None  # 运行验证程序的沙箱实例。
    stop_attempted = False  # 是否已请求停止 Agent。

    stage = "preflight"  # 当前评测阶段：preflight（启动前检查）、setup、agent 或 verify。
    output_dir.mkdir(parents=True, exist_ok=False)  # 每次尝试使用新目录；目录已存在时在创建资源前报错。

    try:
        execution.preflight(goal=task.goal)
        _, input_error = inspect_task(task)  # 检查并计算输入材料指纹；批量执行时复用已计算的结果。
        if input_error is not None:
            raise input_error

        # 任务的 Verifier 对象
        verifier = task.verifier

        # 从 os.environ 读取必需变量，config.verifier_env 补充或覆盖。
        env = dict(config.verifier_env)
        for name in verifier.required_env:
            if name not in env and (value := os.environ.get(name)) is not None:
                env[name] = value
            if name not in env:
                raise ValueError(f"Missing verifier environment variable: {name}")

        setup_timeout = config.setup_timeout
        if task.setup is not None and setup_timeout < task.setup["timeout"]:
            warnings.warn("setup_timeout is less than task.setup.timeout; Setup may time out before the setup command finishes")

        # Setup
        stage = "setup"
        with 阶段时限(setup_timeout):
            # 递归合并本次任务环境配置，得到启动任务沙箱的引用。
            task_ref = task.task_runtime_ref
            if config.task_runtime_config:
                task_ref = replace(
                    task_ref, config=merge_config(task_ref.config, config.task_runtime_config)
                )

            # 递归合并本次验证环境配置，得到启动验证沙箱的引用。
            verifier_ref = task.verifier_runtime_ref
            if config.verifier_runtime_config:
                verifier_ref = replace(
                    verifier_ref, config=merge_config(verifier_ref.config, config.verifier_runtime_config)
                )

            # 准备做题实例和验证实例
            task_instance = runtime.start(
                ref=task_ref,
                context=task.context,
                setup=task.setup,
                connection=config.task_connection,
            )
            verifier_instance = runtime.start(
                ref=verifier_ref,
                connection=config.verifier_connection,
            )

            # 准备 Agent 执行任务所需的环境和工具，具体操作由各 Agent 适配代码实现。
            execution.prepare(
                runtime=runtime,
                instance=task_instance,
                output_dir=output_dir / "agent",
                cwd=task.cwd,
            )

        # Agent Run
        stage = "agent"
        agent_timeout = config.agent_timeout
        try:
            with 阶段时限(agent_timeout):
                result["agent_result"] = execution.run(
                    goal=task.goal,
                    runtime=runtime,
                    instance=task_instance,
                    output_dir=output_dir / "agent",
                )
        except BaseException as error:
            reason = stop_reason(error)
            result["agent_stop_reason"] = reason.value
            if not reason.should_verify:
                raise
            stop_attempted = True
            try:
                execution.stop(timeout=30)
            except Exception as stop_error:
                result["errors"].append(_error_record("agent", error))
                result["errors"].append(_error_record("cleanup", stop_error, operation="stop"))
                return result  # finally 仍会保存结果并关闭沙箱。
            if 阶段时限已到且停止后确认模型服务临时故障:
                result["agent_stop_reason"] = AgentStopReason.ERROR.value
                raise 已确认的模型服务错误
            result["errors"].append(_error_record("agent", error))
        else:
            result["agent_stop_reason"] = AgentStopReason.COMPLETED.value

        # Verify：正常完成或达到执行限制，均对已有成果评分。
        stage = "verify"
        # 在副本上设置本次验证超时，不修改 Task。
        if config.verify_timeout is not None:
            if config.verify_timeout < verifier.timeout:
                warnings.warn("verify_timeout is less than task.verifier.timeout")
            verifier = copy(verifier)
            verifier.timeout = config.verify_timeout
        result["verification_result"] = verifier.run(
            verifier_instance=verifier_instance,
            task_instance=(
                task_instance if verifier.requires_task_instance else None
            ),
            env=env,
        )

    except BaseException as error:
        result["errors"].append(_error_record(stage, error))
        if not isinstance(error, Exception):
            raise

    finally:
        runtime.停止接受新实例()
        # 停止本次 Agent
        if not stop_attempted:
            try:
                execution.stop(timeout=30)
            except Exception as error:
                result["errors"].append(_error_record("cleanup", error, operation="stop"))

        # run 未返回结果时，读取已保存的 Agent 结果快照。
        if result["agent_result"] is None:
            try:
                with (output_dir / "agent" / "result.json").open("r", encoding="utf-8") as file:
                    result["agent_result"] = json.load(file)
            except FileNotFoundError:
                pass
            except Exception as error:
                result["errors"].append(_error_record("cleanup", error, operation="snapshot"))

        # 按启动顺序逆序回收仍在登记的实例，将清理错误追加到结果。
        runtime.回收本次实例(result["errors"])

        result["score_valid"] = (
            result["verification_result"] is not None
            and result["agent_stop_reason"] is not None
            and AgentStopReason(result["agent_stop_reason"]).should_verify
        )

        # 保存本次评测结果
        with (output_dir / "result.json").open("w", encoding="utf-8") as file:
            json.dump(result, file, ensure_ascii=False, indent=2, allow_nan=False)

    return result


def merge_config(base, overrides):
    merged = deepcopy(base)
    for key, value in overrides.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


task = pool.load(task_ref=task_ref)
result = evaluate(task=task, agent=agent, config=config, output_dir=output_dir)
```

</details>

## eval：评测任务 {#eval}

<div class="table-responsive" markdown="1">

::: envweaver.evaluation.eval
    options:
      show_root_heading: false

</div>

AleClaw 的宿主机环境在首道待执行任务派发前准备。准备失败时 `eval()` 抛出异常；修复后可用同一输出目录续跑。

```python
from envweaver import eval

results = eval(
    tasks=tasks,
    agent=agent,
    config=config,
    output_dir="./runs/batch-001",
    concurrency={"setup": 4, "agent": 40, "verify": 4},
)
```

返回列表中的每个字典包含以下字段：
{: #eval-result }

<div class="table-responsive" markdown="1">

| 字段 | 含义 |
| --- | --- |
| `name` | 任务名称 |
| `task_ref` | 任务在 Task Pool 中的存储地址 |
| `metadata` | 本次 rollout 的自定义信息，默认 `{}` |
| `output_dir` | 本次尝试的产物目录，相对于传给 `eval` 的批次目录 |
| `agent_result` | [Agent 运行结果](agent.md#agent-run)，无结果时为 `None` |
| `agent_stop_reason` | Agent 退出原因，如 `completed`、`timeout`；尚未运行时为 `None` |
| `verification_result` | Verify 脚本返回的 JSON 结果，无结果时为 `None` |
| `score_valid` | 得分是否可计入统计；需取得有效验证结果、Agent 正常结束或达到执行限制 |
| `errors` | 评测过程中的问题列表，按发生顺序记录，无问题时为 `[]` |

</div>

<details markdown="1">
<summary>agent_stop_reason 退出原因</summary>

Eval 结果的 `agent_stop_reason` 保存 `AgentStopReason` 的字符串值；尚未进入 Agent Run 时为 `null`。退出原因如下：

<div class="table-responsive" markdown="1" id="agent-stop-reasons">

::: envweaver.AgentStopReason
    options:
      show_root_heading: false
      members: []
      show_bases: false

</div>

</details>

<details markdown="1">
<summary>errors 中每条记录的字段</summary>

<div class="table-responsive" markdown="1" id="error-fields">

| 字段 | 含义 |
| --- | --- |
| `stage` | 发生阶段：`preflight`、`setup`、`agent`、`verify` 或 `cleanup` |
| `type` | `ErrorType` 的字符串值，具体类型见下方 |
| `message` | 具体问题说明 |

</div>

ErrorType：问题类型

<div class="table-responsive" markdown="1" id="error-type">

::: envweaver.ErrorType
    options:
      show_root_heading: false
      members: []
      show_bases: false

</div>

</details>

## config：本次评测配置 {#config}

<div class="table-responsive" markdown="1">

::: envweaver.EvalConfig
    options:
      show_root_heading: false
      members: []

</div>

## Traj Pool

`TrajPool` 用于保存和管理 rollout 的轨迹、评测结果及相关产物，传入 `eval(traj_pool=...)` 即可自动归档。

### Traj Pool：轨迹池 {#trajpool}

::: envweaver.TrajPool
    options:
      members: []
      show_root_heading: false
      show_root_toc_entry: false

```python
from envweaver import TrajPool

traj_pool = TrajPool(root="/data/traj-pool", completed_only=True)
```
