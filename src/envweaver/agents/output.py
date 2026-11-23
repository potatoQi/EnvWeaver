"""Agent artifacts shared by execution management and adapters."""

import hashlib
import json
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .._io.output import OutputDirectory
from .._util import write_json
from ..trajectory.atif import validate_trajectory


def validate_result(result, directory):
    if not isinstance(result, dict):
        raise ValueError("Agent result must be an object")
    json.dumps(result, allow_nan=False)
    if not isinstance(result.get("output"), list) or not isinstance(result.get("metadata"), dict):
        raise ValueError("Agent result requires output and metadata")
    metadata = result["metadata"]
    if not isinstance(metadata.get("agent"), str) or not metadata["agent"]:
        raise ValueError("metadata.agent must be a nonempty string")
    for key in ("agent_version", "model"):
        if key not in metadata or (
            metadata[key] is not None and not isinstance(metadata[key], str)
        ):
            raise ValueError(f"metadata.{key} must be a string or null")
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, dict) or not {"trajectory", "logs", "files"} <= artifacts.keys():
        raise ValueError("Agent result requires trajectory, logs and files artifacts")
    paths = []
    for key in ("logs", "files"):
        if not isinstance(artifacts[key], list):
            raise ValueError(f"artifacts.{key} must be a list")
        paths.extend(artifacts[key])
    if artifacts["trajectory"] is not None:
        paths.append(artifacts["trajectory"])
    for block in result["output"]:
        if not isinstance(block, dict):
            raise ValueError("output entries must be text or image objects")
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            continue
        if block.get("type") == "image":
            paths.append(block.get("path"))
        else:
            raise ValueError("output entries must be text or image objects")
    for path in paths:
        if not isinstance(path, str) or not path or Path(path).is_absolute():
            raise ValueError("Artifact paths must be relative to output_dir")
        target = (directory / path).resolve()
        if not target.is_relative_to(directory.resolve()):
            raise ValueError("Artifact path escapes output_dir")
        if not target.is_file():
            raise FileNotFoundError(target)
    return result


class AgentOutput:
    """由 Eval 创建并传给 Agent；directory 是当前机器上的产物目录。

    Args:
        address: 产物存储目录。
        metadata: 运行信息，包含 agent、agent_version 和 model。

    Raises:
        OSError: 目录无法创建，或已有 result.json。
        envweaver.errors.RuntimeError: SSH 产物目录无法访问。
    """

    def __init__(self, address: str | Path, metadata: dict):
        self._lock = threading.RLock()
        self._seq = 0
        self._output_store = OutputDirectory(
            address, exist_ok=True, sentinel="result.json", source_lock=self._lock,
        )
        self.directory = self._output_store.path
        self._result = {
            "output": [],
            "artifacts": {"trajectory": None, "logs": [], "files": []},
            "metadata": deepcopy(metadata),
        }

    @property
    def result(self) -> dict:
        """读取当前结果，不写入文件；修改返回的字典不会改变已记录的结果。

        Returns:
            当前结果的独立字典副本，包含 output、artifacts、metadata。
        """
        with self._lock:
            return deepcopy(self._result)

    def finish(self, output: list[dict]) -> dict:
        """将最终回复保存到 result.json，并返回包含回复、产物路径和运行信息的完整结果。

        等价于 update(output=...) 后读取 result；不负责停止 Agent。

        Args:
            output: 最终回复的文字和图片列表；无回复时填写 []。

        Returns:
            已保存的完整 Agent 结果，可作为 Agent.run 的返回值。

        Raises:
            ValueError: 回复格式或附件路径无效。
            OSError: 结果或附件无法读写。
            envweaver.errors.RuntimeError: SSH 产物同步失败。
        """
        self.update(output=output)
        return self.result

    def sync(self) -> None:
        """将 output.directory 中新增或修改的文件同步到产物存储目录。

        直接写入 output.directory，或调用 update(sync=False) 后，可用此方法立即同步。
        此方法不从任务沙箱下载文件，也不将文件路径自动登记到运行结果中。

        Returns:
            None。

        Raises:
            OSError: 产物无法读取。
            envweaver.errors.RuntimeError: SSH 产物同步失败。
        """
        self._output_store.sync()

    def update(
        self, *, output: list[dict] | None = None, metadata: dict | None = None,
        artifacts: dict | None = None, sync: bool = True,
    ) -> None:
        """更新当前运行结果并保存到 result.json；未传入的字段保持原值。

        artifacts 只登记已保存文件的路径，不创建或复制这些文件。

        Args:
            output: 回复列表，填写时替换当前回复；默认 None。
            metadata: 补充或覆盖运行信息的同名字段；默认 None。
            artifacts: 补充或覆盖 trajectory、logs、files 字段；路径相对于 directory，文件须已存在。默认 None。
            sync: 是否立即同步到产物存储目录，默认 True。

        Returns:
            None。

        Raises:
            ValueError: 结果格式、数值或附件路径无效。
            OSError: 结果或附件无法读写。
            envweaver.errors.RuntimeError: SSH 产物同步失败。
        """
        with self._lock:
            value = deepcopy(self._result)
            if output is not None:
                value["output"] = deepcopy(output)
            if metadata is not None:
                value["metadata"].update(deepcopy(metadata))
            if artifacts is not None:
                value["artifacts"].update(deepcopy(artifacts))
            validate_result(value, self.directory)
            write_json(self.directory / "result.json", value, private=True)
            self._result = value
        if sync:
            self._output_store.sync()

    def record(self, event_type: str, data: Any) -> None:
        """向 logs/events.jsonl 追加原始事件，自动填写连续序号 seq 和 UTC 时间 time。

        event_type 保存为 type，data 保存为事件内容；文件登记到 artifacts.logs。

        Args:
            event_type: 事件名称，如 message、tool_call 或 Agent 原生事件名称。
            data: 可保存为 JSON 的事件内容。

        Returns:
            None。

        Raises:
            TypeError: 事件内容无法保存为 JSON。
            ValueError: 事件包含非有限数字。
            OSError: 轨迹无法写入。
            envweaver.errors.RuntimeError: SSH 产物同步失败。
        """
        with self._lock:
            event = {
                "seq": self._seq + 1,
                "time": datetime.now(timezone.utc).isoformat(),
                "type": event_type,
                "data": data,
            }
            text = json.dumps(event, ensure_ascii=False, allow_nan=False)
            path = self.directory / "logs/events.jsonl"
            path.parent.mkdir(exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(text + "\n")
            self._seq += 1
            if "logs/events.jsonl" not in self._result["artifacts"]["logs"]:
                self.update(artifacts={"logs": [*self._result["artifacts"]["logs"], "logs/events.jsonl"]}, sync=False)
        self._output_store.sync()

    def save_trajectory(
        self, trajectory: dict, *, related: dict[str, dict] | None = None,
    ) -> None:
        """写入 directory/trajectory.json，并更新 artifacts.trajectory。

        Args:
            trajectory: ATIF 字典；附件和上下文文件路径相对于 directory。
            related: 子轨迹或续接轨迹的文件名到 ATIF 字典的映射，默认 None。文件名填写
                trajectory.<名称>.json，文件保存到 directory 并登记到 artifacts.files。

        Returns:
            None。

        Raises:
            TypeError: 轨迹包含无法保存为 JSON 的值。
            ValueError: ATIF 结构、版本、调用关联、数值或关联文件名无效。
            OSError: 轨迹无法写入。
            envweaver.errors.RuntimeError: SSH 产物同步失败。
        """
        with self._lock:
            related = {} if related is None else related
            if not isinstance(related, dict):
                raise ValueError("Related trajectories must map filenames to ATIF documents")
            validate_trajectory(trajectory)
            for name, document in related.items():
                if (
                    not isinstance(name, str)
                    or not name.startswith("trajectory.")
                    or not name.endswith(".json")
                    or not name[len("trajectory."):-len(".json")]
                    or "/" in name or "\\" in name
                    or "\0" in name
                ):
                    raise ValueError("Related trajectory filename must be trajectory.<name>.json")
                validate_trajectory(document)
            for name, document in related.items():
                write_json(self.directory / name, document, private=True)
            write_json(self.directory / "trajectory.json", trajectory, private=True)
            paths = list(dict.fromkeys([*self._result["artifacts"]["files"], *related]))
            self.update(artifacts={"trajectory": "trajectory.json", "files": paths}, sync=False)
        self._output_store.sync()

    def save_file(self, data: bytes, suffix: str = "") -> str:
        """将附件内容保存到 output.directory/files，并将路径登记到 artifacts.files。

        文件名根据内容生成；相同内容和扩展名复用已有文件。

        Args:
            data: 附件内容。
            suffix: 文件扩展名，如 .png；默认空字符串。

        Returns:
            附件相对于 output.directory 的路径，可用于最终回复的图片 path 或轨迹中的附件引用。

        Raises:
            ValueError: 扩展名无效。
            OSError: 附件无法写入。
            envweaver.errors.RuntimeError: SSH 产物同步失败。
        """
        if suffix and (not suffix.startswith(".") or not suffix[1:].isalnum()):
            raise ValueError("Attachment suffix must be a file extension")
        with self._lock:
            relative = f"files/{hashlib.sha256(data).hexdigest()}{suffix}"
            path = self.directory / relative
            path.parent.mkdir(exist_ok=True)
            if not path.exists():
                path.write_bytes(data)
            paths = self._result["artifacts"]["files"]
            if relative not in paths:
                self.update(artifacts={"files": [*paths, relative]}, sync=False)
        self._output_store.sync()
        return relative

    def write_log(self, name: str, text: str) -> None:
        """向 output.directory/logs 下的日志文件追加原始文字，并将路径登记到 artifacts.logs。

        文件不存在时创建；不会自动添加时间、事件字段或换行。

        Args:
            name: 日志文件名，如 stdout.log，保存在 directory/logs 下。
            text: 追加写入的日志内容。

        Returns:
            None。

        Raises:
            ValueError: name 不是文件名。
            OSError: 日志无法写入。
            envweaver.errors.RuntimeError: SSH 产物同步失败。
        """
        if Path(name).name != name or name in ("", ".", ".."):
            raise ValueError("Log name must be a filename")
        with self._lock:
            path = self.directory / "logs" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(text)
            paths = self._result["artifacts"]["logs"]
            relative = f"logs/{name}"
            if relative not in paths:
                self.update(artifacts={"logs": [*paths, relative]}, sync=False)
        self._output_store.sync()
