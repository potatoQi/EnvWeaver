import json
import os
import random
import re
import threading
import time
import uuid
import warnings
from collections.abc import Iterable, Sized
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import copy, deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from .._io.output import OutputDirectory
from .._io.paths import join_address
from .._util import merge_dicts, positive, validate_setup, write_json
from ..agents import Agent
from ..agents.execution import AgentExecution
from ..agents.output import validate_result
from ..errors import AgentError, AgentStopReason, ErrorType
from ..models import Task
from ..runtime._attempt import _AttemptRuntime
from ..trajectory import TrajPool
from .failure import StageTimeout, can_retry, classify, stop_reason
from .panel import _Panel
from .store import EvalStore, fingerprint

_CLEANUP_TIMEOUT = 30


@dataclass
class EvalConfig:
    """
    Args:
        setup_timeout: Setup 总时限，单位秒，默认 600；小于 task.setup.timeout 时 warning，继续执行。
        agent_timeout: Agent Run 总时限，单位秒，默认 10800（3 小时）。
        verify_timeout: 验证程序时限，不包含材料复制；默认使用 task.verifier.timeout，小于它时 warning，继续执行。
        task_runtime_config: 与 task_runtime_ref.config 递归合并，同名值以本次为准，默认 {}。
        verifier_runtime_config: 与 verifier_runtime_ref.config 递归合并，同名值以本次为准，默认 {}。
        task_connection: 与任务引用的 connection 递归合并，同名值以本次为准，默认 {}。
        verifier_connection: 与验证引用的 connection 递归合并，同名值以本次为准，默认 {}。
        verifier_env: 传给 run.py 的环境变量，默认 {}；task.verifier.required_env 从 os.environ 读取，此处补充或覆盖。
        max_retries: 整题重试次数，非负整数，默认 0（不重试）；不影响 Agent 内部的请求重试。
        retry_delay: 整题重试的初始等待秒数，默认 1；指数退避并加入随机抖动，最长 60 秒，可被取消。

    Raises:
        ValueError: 时限、重试次数或等待时间无效，或字典、环境变量格式错误。
    """

    setup_timeout: float = 600
    agent_timeout: float = 10800
    verify_timeout: float | None = None
    task_runtime_config: dict = field(default_factory=dict)
    verifier_runtime_config: dict = field(default_factory=dict)
    task_connection: dict = field(default_factory=dict)
    verifier_connection: dict = field(default_factory=dict)
    verifier_env: dict[str, str] = field(default_factory=dict)
    max_retries: int = 0
    retry_delay: float = 1

    def __post_init__(self):
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or self.max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer")
        positive(self.retry_delay, "retry_delay")
        for name in ("setup_timeout", "agent_timeout", "verify_timeout"):
            value = getattr(self, name)
            if name == "verify_timeout" and value is None:
                continue
            positive(value, name)
        for name in (
            "task_runtime_config",
            "verifier_runtime_config",
            "task_connection",
            "verifier_connection",
            "verifier_env",
        ):
            value = getattr(self, name)
            if not isinstance(value, dict):
                raise ValueError(f"{name} must be a dictionary")
        for key, value in self.verifier_env.items():
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or not isinstance(value, str)
                or "\0" in value
            ):
                raise ValueError("verifier_env must map environment names to strings without NUL")


class _Worker:
    def __init__(self, function):
        self.done = threading.Event()
        self.error = None
        self.value = None

        def run():
            try:
                self.value = function()
            except BaseException as error:
                self.error = error
            finally:
                self.done.set()

        threading.Thread(target=run, daemon=True, name="envweaver-eval-stage").start()

    def wait(self, timeout=None, cancel=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Evaluation cancelled")
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise StageTimeout(f"Stage exceeded {timeout} seconds")
            if self.done.wait(0.05 if remaining is None else min(0.05, remaining)):
                if self.error is not None:
                    raise self.error
                return self.value


class _Concurrency:
    def __init__(self, limits):
        names = ("setup", "agent", "verify")
        staged = isinstance(limits, dict)
        if staged:
            if set(limits) != set(names):
                raise ValueError("concurrency requires setup, agent and verify limits")
            limits = dict(limits)
        else:
            limits = dict.fromkeys(names, limits)
        if any(type(value) is not int or value <= 0 for value in limits.values()):
            raise ValueError("concurrency limits must be positive integers")
        self.workers = sum(limits.values()) if staged else limits["agent"]
        self.slots = {name: threading.BoundedSemaphore(value) for name, value in limits.items()}

    def start(self, stage, function, cancelled):
        slot = self.slots[stage]
        while not slot.acquire(timeout=0.05):
            if cancelled.is_set():
                raise InterruptedError("Evaluation cancelled")

        def run():
            try:
                if cancelled.is_set():
                    raise InterruptedError("Evaluation cancelled")
                return function()
            finally:
                # A timed-out caller does not imply that the operation has stopped.
                slot.release()

        try:
            return _Worker(run)
        except BaseException:
            slot.release()
            raise


def _config(config):
    if config is None:
        return EvalConfig()
    if not isinstance(config, EvalConfig):
        raise TypeError("config must be an EvalConfig")
    result = deepcopy(config)
    result.__post_init__()
    return result


def _result(task):
    return {
        "id": uuid.uuid4().hex,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "name": task.name,
        "task_ref": task.task_ref,
        "metadata": {},
        "agent_result": None,
        "agent_stop_reason": None,
        "verification_result": None,
        "score_valid": False,
        "errors": [],
    }


def _error_message(error):
    return "\n".join([str(error), *getattr(error, "__notes__", [])])


def _error_record(stage, error, *, operation=None):
    message = _error_message(error)
    return {
        "stage": stage,
        "type": classify(error, stage).value,
        "message": f"{operation}: {message}" if operation else message,
    }


def _evaluate(task, agent, config, output_dir, panel, key, cancelled, record_result, input_error, concurrency):
    # Reserve the Agent before allocating resources; a duplicate caller must not stop it.
    agent = AgentExecution(agent)
    output_store = OutputDirectory(output_dir)
    output_dir = output_store.path
    agent_dir = join_address(output_store.address, "agent")
    result = _result(task)
    json.dumps(result, allow_nan=False)
    result_lock = threading.RLock()
    persisted = False

    def cleanup_error(operation, error):
        with result_lock:
            result["errors"].append(_error_record("cleanup", error, operation=operation))
            if persisted:
                record_result(result)
                write_json(output_dir / "result.json", result, private=True)
                output_store.sync()

    def cleanup_instance(release):
        try:
            _Worker(release).wait(_CLEANUP_TIMEOUT)
        except Exception as error:
            cleanup_error("close", error)

    runtime = _AttemptRuntime(task.verifier.runtime, cleanup_instance)
    stage = "preflight"
    interrupted = None
    setup_worker = None
    stop_attempted = False

    def stop_agent():
        nonlocal stop_attempted
        stop_attempted = True
        try:
            _Worker(lambda: agent.stop(timeout=_CLEANUP_TIMEOUT)).wait(_CLEANUP_TIMEOUT)
        except Exception as error:
            cleanup_error("stop", error)
            return False
        return True

    def start_stage(function):
        panel.update(key, task.name, f"waiting_{stage}")
        current_stage = stage

        def run():
            panel.update(key, task.name, current_stage)
            return function()

        return concurrency.start(current_stage, run, cancelled)

    try:
        panel.update(key, task.name, stage)
        agent.preflight(goal=task.goal)
        validate_setup(task.setup)
        for item in task.context:
            if not isinstance(item, dict) or any(
                not isinstance(item.get(key), str) or not item[key]
                for key in ("source", "target")
            ):
                raise ValueError("context entries require nonempty source and target")
        verifier_env = dict(config.verifier_env)
        for name in task.verifier.required_env:
            if name not in verifier_env and (value := os.environ.get(name)) is not None:
                verifier_env[name] = value
        missing = [name for name in task.verifier.required_env if name not in verifier_env]
        if missing:
            raise ValueError("Missing verifier environment variables: " + ", ".join(missing))
        if input_error is not None:
            raise input_error
        if task.setup is not None and config.setup_timeout < task.setup["timeout"]:
            warnings.warn("setup_timeout is less than task.setup.timeout", stacklevel=3)
        if config.verify_timeout is not None and config.verify_timeout < task.verifier.timeout:
            warnings.warn("verify_timeout is less than task.verifier.timeout", stacklevel=3)
        task_ref = replace(
            task.task_runtime_ref,
            config=merge_dicts(task.task_runtime_ref.config, config.task_runtime_config),
        )
        verifier_ref = replace(
            task.verifier_runtime_ref,
            config=merge_dicts(task.verifier_runtime_ref.config, config.verifier_runtime_config),
        )

        def setup():
            task_instance = runtime.start(
                ref=task_ref,
                context=task.context,
                setup=task.setup,
                connection=config.task_connection,
            )
            verifier_instance = runtime.start(
                ref=verifier_ref, connection=config.verifier_connection
            )
            runtime._check()
            agent.prepare(
                runtime=runtime, instance=task_instance, output_dir=agent_dir, cwd=task.cwd,
            )
            runtime._check()
            return task_instance, verifier_instance

        stage = "setup"
        setup_worker = start_stage(setup)
        task_instance, verifier_instance = setup_worker.wait(config.setup_timeout, cancelled)
        stage = "agent"
        try:
            worker = start_stage(
                lambda: agent.run(
                    goal=task.goal, runtime=runtime, instance=task_instance, output_dir=agent_dir
                ),
            )
            result["agent_result"] = worker.wait(config.agent_timeout, cancelled)
        except BaseException as error:
            reason = stop_reason(error)
            result["agent_stop_reason"] = reason.value
            if not reason.should_verify:
                raise
            prior_errors = len(result["errors"])
            if not stop_agent():
                result["errors"].insert(prior_errors, _error_record(stage, error))
                return result
            if isinstance(error, StageTimeout) and worker.done.wait(_CLEANUP_TIMEOUT):
                if cancelled.is_set():
                    result["agent_stop_reason"] = AgentStopReason.CANCELLED.value
                    raise InterruptedError("Evaluation cancelled")
                cause = worker.error
                if isinstance(cause, AgentError) and cause.type == ErrorType.PROVIDER_TRANSIENT:
                    result["agent_stop_reason"] = AgentStopReason.ERROR.value
                    raise cause from error
            result["errors"].append(_error_record(stage, error))
        else:
            result["agent_stop_reason"] = AgentStopReason.COMPLETED.value
        if cancelled.is_set():
            raise InterruptedError("Evaluation cancelled")
        stage = "verify"
        verifier = copy(task.verifier)
        if config.verify_timeout is not None:
            verifier.timeout = config.verify_timeout
        result["verification_result"] = start_stage(
            lambda: verifier.run(
                verifier_instance=verifier_instance,
                task_instance=task_instance if verifier.requires_task_instance else None,
                env=verifier_env,
            ),
        ).wait(cancel=cancelled)
    except BaseException as error:
        result["errors"].append(_error_record(stage, error))
        if not isinstance(error, Exception):
            interrupted = error
    finally:
        # Prevent late startup from entering prepare after we have already stopped the Agent.
        runtime._cancel()
        agent.cancel()
        if not stop_attempted:
            stop_agent()
        snapshot_dir = agent.output.directory if agent.output is not None else None
        if result["agent_result"] is None and snapshot_dir is not None:
            try:
                with (snapshot_dir / "result.json").open(encoding="utf-8") as stream:
                    snapshot = json.load(stream)
            except FileNotFoundError:
                pass
            except Exception as error:
                cleanup_error("snapshot", error)
            else:
                try:
                    result["agent_result"] = validate_result(snapshot, snapshot_dir)
                except Exception as error:
                    cleanup_error("snapshot", error)
        runtime._finish()
        if setup_worker is not None and not setup_worker.done.wait(_CLEANUP_TIMEOUT):
            cleanup_error("setup", TimeoutError("Sandbox startup has not finished; cleanup is pending"))
        result["score_valid"] = (
            result["verification_result"] is not None
            and result["agent_stop_reason"] is not None
            and AgentStopReason(result["agent_stop_reason"]).should_verify
        )
        with result_lock:
            record_result(result)
            write_json(output_dir / "result.json", result, private=True)
            output_store.sync()
            persisted = True
        panel.update(
            key,
            task.name,
            "done" if result["verification_result"] is not None
            and not any(error["stage"] == "cleanup" for error in result["errors"]) else "failed",
            result,
        )
    if interrupted is not None:
        raise interrupted
    return result


def eval(
    tasks: Task | Iterable[Task],
    agent: Agent,
    config: EvalConfig | None = None,
    output_dir: str | Path = "eval",
    *,
    concurrency: int | dict[str, int] = 1,
    resume: bool = False,
    traj_pool: TrajPool | None = None,
) -> list[dict]:
    """
    Args:
        tasks: 一个 Task，或 Task 的可迭代对象。
        agent: 执行任务的 Agent 对象，例如 `CodexAgent(model=model_name)`。
        config: 任务使用的评测配置，不填使用 EvalConfig 默认值。
        output_dir: 评测产物的保存目录，默认 eval。
        concurrency: 正整数限制同时评测的任务数，默认 1；或填写 {"setup": 4, "agent": 40, "verify": 4}，分别限制三个阶段的并发。阶段排队不计入超时，同时进行中的任务最多为三个上限之和。
        traj_pool: 自动归档 rollout 的 [TrajPool](evaluation.md#trajpool)；默认 None，不入池。归档失败会报错，评测产物保留在 output_dir。
        resume: 是否继续上次评测，默认 False；已完成任务保留原结果，未完成任务使用本次 Agent 和 EvalConfig。
            历次尝试须包含 configuration 配置快照。

    Returns:
        按输入顺序排列的评测结果列表，每道题一个字典；errors 按发生顺序记录各阶段的问题；每次新执行的 metadata 默认为独立的空字典。

    Raises:
        TypeError: tasks 不是 Task 或可迭代对象、包含非 Task 元素，或 agent、config、traj_pool 类型错误。
        ValueError: concurrency、config 或输出地址无效，或续跑记录缺失、无效及任务与原批次不一致。
        OSError: 输出目录已存在、正在被其他 eval 使用、无法创建，或评测结果无法保存。
        envweaver.errors.RuntimeError: SSH 产物目录无法访问或写入。
        TimeoutError: SSH 操作或获取产物目录锁超时。
        KeyboardInterrupt: 用户中断；取消未开始的任务，保存已有结果并清理正在执行的评测。
    """
    concurrency = _Concurrency(concurrency)
    if not isinstance(resume, bool):
        raise TypeError("resume must be a boolean")
    if not isinstance(agent, Agent):
        raise TypeError("agent must be an Agent")
    if traj_pool is not None and not isinstance(traj_pool, TrajPool):
        raise TypeError("traj_pool must be a TrajPool")
    config = _config(config)
    total = 1 if isinstance(tasks, Task) else len(tasks) if isinstance(tasks, Sized) else None
    iterator = iter([tasks]) if isinstance(tasks, Task) else iter(tasks)
    with EvalStore(output_dir, resume) as batch:
        return _run_batch(iterator, agent, config, batch, concurrency, traj_pool, total)


def _run_batch(iterator, agent, config, batch, concurrency, traj_pool, total):
    output_store = batch.store
    output_dir = output_store.path
    cancelled = threading.Event()
    results = {
        int(index): entry["result"] for index, entry in batch.data["tasks"].items()
        if entry["result"] is not None
    }
    saved_results = {}
    saved_lock = threading.Lock()
    iterator = enumerate(iterator)
    with _Panel(total if total is not None else batch.data["count"]) as panel:
        for index, entry in batch.data["tasks"].items():
            if entry["complete"]:
                panel.finish(int(index), entry["result"])

        def archive(entry, attempts):
            if traj_pool is None:
                return
            published = False
            for item in attempts:
                result = item["result"]
                if result is None:
                    continue
                directory = join_address(batch.address, item["output_dir"])
                reference = traj_pool.save(output_dir=directory)
                if reference is not None:
                    with batch.lock:
                        result["traj_ref"] = reference
                        if entry["result"] and entry["result"].get("id") == result.get("id"):
                            entry["result"]["traj_ref"] = reference
                    archived = OutputDirectory(directory, exist_ok=True)
                    write_json(archived.path / "result.json", result, private=True)
                    archived.sync()
                    published = True
            # Persist references only after successful publication. Resume can repeat safely.
            if published:
                with batch.lock:
                    batch._save()

        def finish(index, result, complete, *, exist_ok=True):
            entry = batch.data["tasks"][str(index)]
            item = entry["attempts"][-1]
            result["output_dir"] = item["output_dir"]
            result["configuration"] = deepcopy(item["configuration"]["values"])
            result["ended_at"] = datetime.now(timezone.utc).isoformat()
            result["duration_seconds"] = max(0, (
                datetime.fromisoformat(result["ended_at"]) - datetime.fromisoformat(result["started_at"])
            ).total_seconds())
            store = OutputDirectory(join_address(batch.address, result["output_dir"]), exist_ok=exist_ok)
            write_json(store.path / "result.json", result, private=True)
            store.sync()
            with saved_lock:
                saved_results[index] = deepcopy(result)
            batch.finish(index, result, complete)
            archive(entry, (item,))
            if "traj_ref" in entry["result"]:
                reference = entry["result"]["traj_ref"]
                result["traj_ref"] = reference
                with saved_lock:
                    saved_results[index]["traj_ref"] = reference

        def attempt(index, task, current_agent, relative, input_error):
            directory = join_address(batch.address, relative)

            def record_result(result):
                result["output_dir"] = relative
                with saved_lock:
                    saved_results[index] = deepcopy(result)

            try:
                return _evaluate(
                    task,
                    current_agent,
                    deepcopy(config),
                    directory,
                    panel,
                    index,
                    cancelled,
                    record_result,
                    input_error,
                    concurrency,
                )
            except Exception as error:
                with saved_lock:
                    result = deepcopy(saved_results.get(index))
                if result is None:
                    result = _result(task)
                    result["errors"].append(_error_record("preflight", error))
                else:
                    result["errors"].append(_error_record("cleanup", error, operation="save_result"))
                record_result(result)
                failed_store = OutputDirectory(directory, exist_ok=True)
                write_json(failed_store.path / "result.json", result, private=True)
                failed_store.sync()
                panel.update(index, task.name, "failed", result)
                return result

        def create_agent():
            current_agent = agent.new()
            if not isinstance(current_agent, Agent):
                raise TypeError("Agent.new() must return an Agent")
            if current_agent is agent:
                raise ValueError("Agent.new() must return an independent Agent")
            if fingerprint(current_agent._identity()) != fingerprint(agent._identity()):
                raise ValueError("Agent.new() must preserve Agent configuration")
            return current_agent

        def run(index, task):
            entry, input_error = batch.register(index, task)
            archive(entry, entry["attempts"])
            if entry["complete"]:
                panel.update(index, task.name, "resumed", entry["result"])
                return entry["result"]
            for retry in range(config.max_retries + 1):
                if cancelled.is_set():
                    raise InterruptedError("Evaluation cancelled")
                relative = batch.start(index, agent, config)
                try:
                    current_agent = create_agent()
                except Exception as error:
                    result = _result(task)
                    result["errors"].append(_error_record("preflight", error))
                    finish(index, result, complete=False, exist_ok=False)
                    panel.update(index, task.name, "failed", result)
                    return result
                with saved_lock:
                    saved_results.pop(index, None)
                try:
                    result = attempt(index, task, current_agent, relative, input_error)
                except BaseException:
                    with saved_lock:
                        partial = saved_results.get(index)
                    if partial is not None:
                        finish(index, partial, complete=False)
                    raise
                errors = result["errors"]
                retry_allowed = (
                    bool(errors)
                    and all(can_retry(error) for error in errors)
                    and retry < config.max_retries
                )
                retrying = retry_allowed and not cancelled.is_set()
                complete = not retry_allowed and not any(
                    error["type"] == ErrorType.CANCELLED for error in errors
                )
                finish(index, result, complete=complete)
                if not retrying:
                    return result
                panel.update(index, task.name, "retry", result)
                delay = min(60, config.retry_delay * 2 ** min(retry, 20))
                if cancelled.wait(random.uniform(delay / 2, delay)):
                    raise InterruptedError("Evaluation cancelled")
                _, input_error = batch.register(index, task)

        executor = ThreadPoolExecutor(max_workers=concurrency.workers, thread_name_prefix="envweaver-eval")
        pending = {}
        submitted = 0
        exhausted = False

        def submit():
            nonlocal submitted, exhausted
            if exhausted:
                return
            item = next(iterator, None)
            if item is None:
                exhausted = True
                batch.exhausted(submitted)
                with panel.lock:
                    panel.total = submitted
                return
            index, task = item
            if not isinstance(task, Task):
                raise TypeError("tasks must contain Task objects")
            submitted += 1
            pending[executor.submit(run, index, task)] = index

        try:
            for _ in range(concurrency.workers):
                submit()
            while pending:
                completed, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in completed:
                    index = pending[future]
                    results[index] = future.result()
                    panel.finish(index, results[index])
                    del pending[future]
                    submit()
        except BaseException:
            cancelled.set()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            for future, index in pending.items():
                if future.cancelled():
                    continue
                if future.exception() is None:
                    results[index] = future.result()
                    panel.finish(index, results[index])
                else:
                    # Keep interrupted attempts even when SSH persistence has failed.
                    with saved_lock:
                        if index in saved_results:
                            results[index] = deepcopy(saved_results[index])
            ordered = [results[index] for index in sorted(results)]
            write_json(output_dir / "results.json", ordered, private=True)
            output_store.sync()
    return ordered
