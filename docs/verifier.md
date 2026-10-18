# Verifier

Verifier 在验证沙箱中执行 Verify，评估任务完成情况并返回得分，可用于 Data Pipeline、Training、Evaluation。

## Verifier：配置验证

::: envweaver.Verifier
    options:
      show_root_heading: false
      members: []

```python
from envweaver import Verifier

checker = Verifier(
    entry="ssh://server/data/verify/run.py",
    runtime=runtime,
    timeout=300,
    required_env=["LLM_BASE_URL", "LLM_API_KEY"],
    requires_task_instance=True,
)
```

## Verifier.run：执行 Verify {#run}

::: envweaver.Verifier.run
    options:
      show_root_heading: false

```python
result = checker.run(
    verifier_instance=verifier_instance,
    task_instance=task_instance,
    files=[
        {"source": "./answer.json", "target": "answer.json"},
        {"source": "ssh://server/data/screenshots", "target": "/data/screenshots"},
    ],
    env={"LLM_BASE_URL": base_url, "LLM_API_KEY": api_key},
)
```

<details markdown="1" id="entry">
<summary>run.py 入口</summary>

- 文件名为 `run.py`，作为 Python 脚本在验证沙箱内执行，不要求定义特定函数或类。
- 标准输出只写一个 JSON 对象，必须包含数值 `score`，范围为 0～1；可以包含其他 JSON 字段。日志写标准错误。
- 验证正常完成时退出码为 `0`，包括任务得零分的情况。非零退出、超过 `timeout` 或输出格式错误属于执行失败。
- 需要任务沙箱时，脚本接收 `--instance <连接文件路径>`，用 `Runtime.connect` 连接。不需要时，无须处理这个参数。

**示例：在任务沙箱中运行测试**

设置 `requires_task_instance=True`。`tests/` 与 `run.py` 放在一起，测试在任务沙箱的 `/work/repo` 中运行。

```python
import argparse
import json
from envweaver import Runtime

parser = argparse.ArgumentParser()
parser.add_argument("--instance", required=True)
runtime = Runtime()
task_instance = runtime.connect(path=parser.parse_args().instance)

runtime.upload(
    instance=task_instance,
    source="./tests",
    target="/work/repo/tests",
)
code, stdout, stderr = runtime.exec(
    instance=task_instance,
    command="python3 -m pytest -q",
    cwd="/work/repo",
)
if code not in (0, 1):
    raise RuntimeError(stderr)
print(json.dumps({"score": float(code == 0)}))
```

**示例：在验证沙箱中运行测试**

设置 `requires_task_instance=False`。`repo/` 与 `run.py` 放在一起，`submission.patch` 通过 `files` 传入。

```python
import json
import subprocess
import sys
from pathlib import Path

subprocess.run(
    ["git", "apply", str(Path("submission.patch").resolve())],
    cwd="repo", check=True, capture_output=True,
)
result = subprocess.run(
    [sys.executable, "-m", "pytest", "-q"],
    cwd="repo", capture_output=True,
)
if result.returncode not in (0, 1):
    raise RuntimeError(result.stderr.decode())
print(json.dumps({"score": float(result.returncode == 0)}))
```

</details>
