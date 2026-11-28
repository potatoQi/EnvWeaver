# Agent {#agent}

Agent 为 [Eval](evaluation.md) 提供统一的执行接口。创建 Agent 对象时配置模型、环境变量和原生参数，再将对象传给 `eval(agent=...)`。

带 `cache` 参数的 Agent 默认复用准备阶段有校验值的安装包，可传 `cache=False` 关闭。缓存目录通过当前进程的 `ENVWEAVER_CACHE_DIR` 设置，默认 `~/.cache/envweaver/downloads`，容量为 30 GiB。

运行中读取 CLI 轨迹遇到暂时的传输故障时，面板保留上次进度，下一次刷新继续采集。

已支持的 Agent：[Claude Code](#claude-code-agent)、[Codex CLI](#codex-agent)、[Pi](#pi-agent)、[OpenClaw](#openclaw-agent)、[ALE Claw](#ale-claw-agent)、[Stirrup](#stirrup-agent)、[mini-swe-agent](#mini-swe-agent)、[Claw-Eval](#claw-eval-agent)、[Terminus 2](#terminus-agent)、[自定义 Agent](#custom-agent)。

## ClaudeCodeAgent {#claude-code-agent}

<div class="table-responsive" markdown="1">

::: envweaver.ClaudeCodeAgent
    options:
      show_root_heading: false
      members: []
      show_bases: false

</div>

```python
from envweaver import ClaudeCodeAgent, eval

agent = ClaudeCodeAgent(
    model=model_name,
    env={"ANTHROPIC_BASE_URL": gateway_url, "ANTHROPIC_API_KEY": api_key},
)
results = eval(tasks=tasks, agent=agent, output_dir="outputs/claude")
```

## CodexAgent {#codex-agent}

<div class="table-responsive" markdown="1">

::: envweaver.CodexAgent
    options:
      show_root_heading: false
      members: []
      show_bases: false

</div>

```python
from envweaver import CodexAgent

agent = CodexAgent(model=model_name, env=agent_env)
```

## PiAgent {#pi-agent}

<div class="table-responsive" markdown="1">

::: envweaver.PiAgent
    options:
      show_root_heading: false
      members: []
      show_bases: false

</div>

```python
from envweaver import PiAgent

agent = PiAgent(model=model_name, env=agent_env)
```

## OpenClawAgent {#openclaw-agent}

<div class="table-responsive" markdown="1">

::: envweaver.OpenClawAgent
    options:
      show_root_heading: false
      members: []
      show_bases: false

</div>

```python
from envweaver import OpenClawAgent, eval

agent = OpenClawAgent(
    model="openai/gpt-4.1",
    env={"OPENAI_API_KEY": api_key},
)
results = eval(tasks=tasks, agent=agent, output_dir="outputs/openclaw")
```

## AleClawAgent {#ale-claw-agent}

<div class="table-responsive" markdown="1">

::: envweaver.AleClawAgent
    options:
      show_root_heading: false
      members: []
      show_bases: false

</div>

做题实例须已启动 CUA computer-server。控制器在执行 Eval 的 Linux 宿主机上访问模型。

`options["tool_calling"]` 控制 Agent 如何调用工具。默认 `auto` 按模型选择；`function_calling` 通过 Chat Completions 的 `tools` 字段传入工具；`prompt` 在提示词中描述工具。

```python
from envweaver import AleClawAgent, eval

agent = AleClawAgent(
    model="openai/Qwen3.8-27B",
    options={"tool_calling": "function_calling", "base_url": model_base_url, "api_key": api_key},
)
results = eval(tasks=tasks, agent=agent, output_dir="outputs/ale-claw")
```

## StirrupAgent {#stirrup-agent}

<div class="table-responsive" markdown="1">

::: envweaver.StirrupAgent
    options:
      show_root_heading: false
      members: []
      show_bases: false

</div>

```python
from envweaver import StirrupAgent, eval

agent = StirrupAgent(
    model="openai/Qwen3.5-9B",
    context_window_tokens=131072,
    max_tokens=16384,
    client_kwargs={"api_base": gateway_url, "temperature": 0.7},
    env={"OPENAI_API_KEY": api_key},
    view_image=True,
    submission_dir="/workspace",
    web=False,
)
results = eval(tasks=tasks, agent=agent, output_dir="outputs/stirrup")
```

## MiniSweAgent {#mini-swe-agent}

<div class="table-responsive" markdown="1">

::: envweaver.MiniSweAgent
    options:
      show_root_heading: false
      members: []
      show_bases: false

</div>

```python
from envweaver import MiniSweAgent

agent = MiniSweAgent(model=model_name, env=agent_env)
```

## ClawEvalAgent {#claw-eval-agent}

ClawEvalAgent 仅适用于用来作为评测 Claw-Eval 时候的 Harness。将 Claw-Eval 题目的 `task.yaml` 和 `sandbox_files` 引用的材料放在 `Task.cwd` 下。

<div class="table-responsive" markdown="1">

::: envweaver.ClawEvalAgent
    options:
      show_root_heading: false
      members: []
      show_bases: false

</div>

```python
from envweaver import ClawEvalAgent, eval

agent = ClawEvalAgent(
    model={
        "model": "Qwen3.8-27B",
        "base_url": base_url,
        "api_key": api_key,
        "context_window": 262144,  # 按部署的上下文窗口填写，单位为 token。
        "temperature": 0.0,
        # 可选，按所用模型服务设置。
        # "top_p": 0.95,
        # "top_k": 20,
        "reasoning_effort": "high",
    },
    cache=True,
)
results = eval(tasks=task, agent=agent)
```

## Terminus2Agent {#terminus-agent}

<div class="table-responsive" markdown="1">

::: envweaver.Terminus2Agent
    options:
      show_root_heading: false
      members: []
      show_bases: false

</div>

```python
from envweaver import Terminus2Agent

agent = Terminus2Agent(
    model="openai/Qwen3.8-27B",
    env={"OPENAI_API_KEY": api_key},
    options={
        "api_base": base_url,
        "model_info": {
            "max_input_tokens": 262144,
            "max_output_tokens": 8192,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
    },
)
```

<details id="custom-agent" class="mt-5" markdown="1">
<summary>自定义 Agent</summary>

自定义 Agent 需继承 `Agent` 基类。`run()` 必须实现；`preflight()`、`prepare()` 和 `stop()` 按需实现。Eval 自动调用这些方法并提供参数：

<div class="table-responsive" markdown="1">

| 方法 | Eval 调用时机 | 适配代码需要实现什么 |
| --- | --- | --- |
| `preflight()` | 沙箱启动前，以及 `run()` 执行前 | 检查输入和配置是否支持 |
| `prepare()` | 做题沙箱完成 context 上传和 task.setup 后 | 安装工具、配置环境或初始化客户端 |
| `run()` | `prepare()` 完成后 | 执行题目，保存轨迹和产物，返回运行结果 |
| `stop()` | 超时、取消或执行失败，需要停止 Agent 时 | 停止本次请求和进程，保留任务沙箱及题目服务 |

</div>

```python
from envweaver import Agent

class CustomAgent(Agent):
    def preflight(self, goal):
        # 检查 Agent 特有的输入要求。
        ...

    def prepare(self, *, runtime, instance, output, cwd: str):
        # 安装工具或创建客户端；参数由 Eval 提供。
        ...

    def run(self, *, goal, runtime, instance, output, cwd: str):
        # 执行 goal，将最终回复保存为文字和图片列表。
        reply = ...
        return output.finish(output=reply)

    def stop(self, timeout=30):
        # 停止自己创建的请求或进程，保留做题沙箱和题目服务。
        ...

# 自定义参数通过 self.config["参数名"] 读取；修改返回的字典不会改变 Agent 配置。
agent = CustomAgent(model=model_name, endpoint=base_url, temperature=0.7)
```

### Agent.preflight：启动前检查 {#agent-preflight .mt-4}

<div class="table-responsive" markdown="1">

::: envweaver.Agent.preflight
    options:
      show_root_heading: false

</div>

### Agent.prepare：准备 Agent {#agent-prepare .mt-4}

<div class="table-responsive" markdown="1">

::: envweaver.Agent.prepare
    options:
      show_root_heading: false

</div>

### Agent.run：执行任务 {#agent-run .mt-4}

<div class="table-responsive" markdown="1">

::: envweaver.Agent.run
    options:
      show_root_heading: false

</div>

<details markdown="1">
<summary>返回结果</summary>

<div class="table-responsive" markdown="1">

| 字段 | 含义 |
| --- | --- |
| `output` | 最终回复的文字和图片列表，无回复时为 []；使用与 goal 相同的字段，图片 path 相对于 output.directory |
| `artifacts.trajectory` | 已保存的轨迹文件路径，无轨迹时为 null |
| `artifacts.logs` | 已保存的日志文件路径列表，无日志时为 [] |
| `artifacts.files` | 已保存的附件路径列表，无附件时为 [] |
| `metadata` | 运行信息字典，包含以下固定字段，也可添加耗时、用量等自定义字段；未知数值使用 null |
| ↳ `agent` | Agent 名称 |
| ↳ `agent_version` | Agent 版本，未知时为 null |
| ↳ `model` | 模型名称，未知时为 null |

</div>

产物格式：

```text
agent/
├── result.json       # Agent 运行结果快照
├── trajectory.json   # ATIF 轨迹
├── logs/             # 原始事件、原生记录和日志
└── files/            # 图片和模型上下文快照
```

轨迹使用 [Harbor ATIF](https://harborframework.com/docs/atif) v1.8，`schema_version` 为 `ATIF-v1.8`；可通过 `envweaver.ATIF_VERSION` 读取当前版本。

模型步骤的 `extra.context.path` 指向该次调用的上下文快照；`source` 标明采集位置，`complete` 和 `missing` 标明是否存在已知缺失。图片、上下文和子轨迹文件路径均相对于 Agent 的产物目录。

Codex 的 ATIF 导出支持原生 rollout trace v1、v2 和 v3。导出失败时，可在 `agent/result.json` 的 `metadata.trajectory_export_error` 查看原因，在 `agent/logs/native/codex/` 查看已采集的原始记录。Eval 仍按做题的停止原因决定是否评分。

Codex 子 Agent 使用独立子轨迹；Terminus 的压缩和续接沿用 Harbor 的轨迹引用。加密内容保存在原生记录中，不还原为推理文字。未采集到原生模型调用时，轨迹中的 `extra.capture` 会注明缺失。

</details>

### Agent.stop：停止 Agent {#agent-stop .mt-4}

<div class="table-responsive" markdown="1">

::: envweaver.Agent.stop
    options:
      show_root_heading: false

</div>

### 适配建议 {#agent-output}

**1. 强烈建议使用 `output` 保存产物。**

Eval 将 `output` 传给 `prepare()` 和 `run()`。新增 Agent 适配需要将原生轨迹转换为 ATIF，并保存每次模型调用的上下文；原始记录单独保存为日志。

`output.directory` 是当前机器上的产物目录，结果中的产物路径相对于该目录。以下写入方法默认会同步产物；直接写入目录的文件可通过 `sync()` 同步。

<details markdown="1">
<summary>AgentOutput.save_trajectory：保存 ATIF 轨迹</summary>

<div class="table-responsive" markdown="1">

::: envweaver.AgentOutput.save_trajectory
    options:
      show_root_heading: false

</div>

```python
from envweaver import ATIF_VERSION

trajectory = {
    "schema_version": ATIF_VERSION,
    "agent": {"name": "my-agent", "version": "1"},
    "steps": [
        {"step_id": 1, "source": "user", "message": prompt},
        {"step_id": 2, "source": "agent", "message": reply},
    ],
}
output.save_trajectory(trajectory=trajectory)

# 有续接轨迹时一并保存。
trajectory["continued_trajectory_ref"] = "trajectory.cont-1.json"
output.save_trajectory(
    trajectory=trajectory,
    related={"trajectory.cont-1.json": continuation},
)
```

</details>

<details markdown="1">
<summary>AgentOutput.record：记录原始事件</summary>

<div class="table-responsive" markdown="1">

::: envweaver.AgentOutput.record
    options:
      show_root_heading: false

</div>

```python
output.record(event_type="message", data={"role": "assistant", "text": text})
```

</details>

<details markdown="1">
<summary>AgentOutput.write_log：写入日志</summary>

<div class="table-responsive" markdown="1">

::: envweaver.AgentOutput.write_log
    options:
      show_root_heading: false

</div>

```python
output.write_log(name="stdout.log", text=text)
```

</details>

<details markdown="1">
<summary>AgentOutput.save_file：保存附件</summary>

<div class="table-responsive" markdown="1">

::: envweaver.AgentOutput.save_file
    options:
      show_root_heading: false

</div>

```python
path = output.save_file(data=image_bytes, suffix=".png")
```

</details>

<details markdown="1">
<summary>AgentOutput.update：更新结果</summary>

<div class="table-responsive" markdown="1">

::: envweaver.AgentOutput.update
    options:
      show_root_heading: false

</div>

```python
output.update(metadata={"usage": usage})
```

</details>

<details markdown="1">
<summary>AgentOutput.finish：保存最终回复</summary>

<div class="table-responsive" markdown="1">

::: envweaver.AgentOutput.finish
    options:
      show_root_heading: false

</div>

```python
reply = [{"type": "text", "text": "任务已完成"}]
return output.finish(output=reply)
```

</details>

<details markdown="1">
<summary>AgentOutput.result：读取结果</summary>

<div class="table-responsive" markdown="1">

::: envweaver.AgentOutput.result
    options:
      show_root_heading: false

</div>

```python
result = output.result
```

</details>

<details markdown="1">
<summary>AgentOutput.sync：同步直接写入的文件</summary>

<div class="table-responsive" markdown="1">

::: envweaver.AgentOutput.sync
    options:
      show_root_heading: false

</div>

```python
# 写入自行生成的文件，再同步到产物存储目录。
(output.directory / "report.txt").write_text(report_text, encoding="utf-8")
output.sync()
```

</details>

**2. 强烈建议通过 `AgentError` 报告问题。**

`type` 使用 [ErrorType](evaluation.md#error-type)。

<div class="table-responsive" markdown="1">

::: envweaver.errors.AgentError
    options:
      show_root_heading: false
      members: []
      show_bases: false

</div>

```python
from envweaver import ErrorType
from envweaver.errors import AgentError

raise AgentError(type=ErrorType.STEP_LIMIT, message="已达到最大步数")
```

</details>
