import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

from ._util import run_process
from .errors import VerifierError
from .models import Instance
from .runtime import Runtime


def run(entry_path: str | Path, instance: Instance, *, timeout: float = 300) -> dict:
    """用 Pipeline 当前的 Python 解释器执行验证包。

    Args:
        entry_path: 验证入口 run.py 的文件路径。
        instance: 本次要检查的运行实例。
        timeout: 验证器最长执行秒数。

    Returns:
        标准输出中的 JSON 对象；score 必须为 0～1 的有限数值，允许其他 JSON 字段。

    Raises:
        ValueError: 入口文件名不是 run.py。
        FileNotFoundError: 入口不存在。
        VerifierError: 验证进程超时、非零退出、输出格式不合法或分数不合法。

    自动执行 `python run.py --instance <临时描述文件>`，工作目录为验证包目录。
    run.py 使用 runtime.connect 读取目标，再通过 Runtime、LLM 或 Agent 进行检查。
    stdout 只输出一个 JSON 对象，日志写 stderr。执行失败与合法 score=0 严格区分。
    """
    entry = Path(entry_path).resolve(strict=True)
    if entry.name != "run.py":
        raise ValueError("Verifier entry must be named run.py")
    with tempfile.TemporaryDirectory(prefix="envweaver-verifier-") as directory:
        descriptor = Path(directory) / "instance.json"
        Runtime.describe(instance, descriptor)
        try:
            result = run_process(
                [sys.executable, str(entry), "--instance", str(descriptor)],
                cwd=entry.parent,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise VerifierError(f"Verifier timed out after {timeout}s") from error
    diagnostics = result.stderr.decode("utf-8", errors="replace")
    if result.returncode:
        raise VerifierError(f"Verifier exited with {result.returncode}: {diagnostics}")
    try:
        output = json.loads(result.stdout)
        score = output["score"]
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not 0 <= score <= 1
        ):
            raise ValueError("score must be a finite number in [0, 1]")
        # Validate auxiliary fields too: NaN/Infinity are not valid JSON.
        json.dumps(output, allow_nan=False)
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        raise VerifierError(f"Invalid verifier result: {error}; stderr: {diagnostics}") from error
    return output
