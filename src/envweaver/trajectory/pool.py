"""Archive Eval outputs without copying Tasks or introducing another trajectory format."""

import base64
import json
import math
import re
import shutil
import tempfile
from pathlib import Path

from .._io.files import pack
from .._io.output_lock import output_lock
from .._io.paths import join_address, normalize_address
from .._io.storage import _dispatch, local_source
from .._util import metadata_tags, read_json, write_json
from ..errors import AgentStopReason, ErrorType
from .atif import validate_trajectory


def completed(result):
    """Completion is independent of score magnitude and ATIF context completeness."""
    try:
        reason = AgentStopReason(result["agent_stop_reason"])
        score = result["verification_result"]["score"]
        return (
            result["score_valid"] is True
            and reason.should_verify
            and bool(result["agent_result"]["artifacts"]["trajectory"])
            and type(score) in (int, float)
            and math.isfinite(score)
            and 0 <= score <= 1
            and all(
                e["stage"] == "agent" and ErrorType(e["type"]).is_limit for e in result["errors"]
            )
        )
    except (KeyError, TypeError, ValueError):
        return False


def validate_record(result):
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("id"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", result["id"])
    ):
        raise ValueError("Rollout requires a 32-character hexadecimal id")
    if not isinstance(result.get("name"), str) or not isinstance(result.get("metadata"), dict):
        raise ValueError("Rollout requires name and metadata")
    if not isinstance(result.get("errors"), list):
        raise ValueError("Rollout requires errors")
    metadata_tags(result["metadata"])
    json.dumps(result, allow_nan=False)
    return result


def validate_artifacts(result, directory):
    agent = result.get("agent_result")
    if agent is None:
        return
    from ..agents.output import validate_result

    validate_result(agent, directory)
    trajectory = agent["artifacts"]["trajectory"]
    seen = set()

    def file(relative):
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ValueError("ATIF references must be relative to the Agent directory")
        path = directory / relative
        if not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("ATIF reference escapes the Agent directory")
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def references(node):
        if isinstance(node, list):
            for item in node:
                references(item)
        elif isinstance(node, dict):
            if node.get("media_type") and isinstance(node.get("path"), str):
                file(node["path"])
            context = node.get("context")
            if isinstance(context, dict) and context.get("path"):
                file(context["path"])
            for key in ("trajectory_path", "continued_trajectory_ref"):
                if node.get(key):
                    check(node[key])
            # Tool arguments and raw payloads are not artifact declarations.
            for key, value in node.items():
                if key not in {"arguments", "raw_content", "raw_response"}:
                    references(value)

    def check(relative):
        path = file(relative)
        if path in seen:
            return
        seen.add(path)
        value = read_json(path)
        validate_trajectory(value)
        references(value)

    if trajectory:
        check(trajectory)


class TrajPool:
    """
    Args:
        root: 轨迹池目录。
        completed_only: 是否只保存已有 ATIF、正常结束或达到执行限制、取得有效评分且无其他故障的 rollout，默认 True；零分也保存。

    Raises:
        ValueError: root 为空或 SSH 地址无效。
        TypeError: completed_only 不是布尔值。
    """

    def __init__(self, root: str | Path, *, completed_only: bool = True):
        if not isinstance(completed_only, bool):
            raise TypeError("completed_only must be a boolean")
        self.root = normalize_address(root)
        self.completed_only = completed_only

    def save(self, output_dir: str | Path) -> str | None:
        """
        Args:
            output_dir: 一次已结束评测的产物目录，包含 result.json 和已有的 agent 目录；支持本地路径及 SSH 地址。

        Returns:
            池内 result.json 地址；重复保存返回原地址，不覆盖池内记录；completed_only=True 且不满足成品条件时返回 None。

        Raises:
            ValueError: 结果、ATIF、附件路径无效，或产物包含符号链接、特殊文件。
            OSError: 文件缺失、无法访问或归档失败。
            envweaver.errors.RuntimeError: SSH 传输失败。
            TimeoutError: SSH 传输或获取写锁超时。
        """
        address = normalize_address(output_dir)
        with local_source(join_address(address, "result.json")) as result_path:
            result = validate_record(read_json(result_path))
        if self.completed_only and not completed(result):
            return None
        destination = join_address(self.root, result["id"])
        ref = join_address(destination, "result.json")
        locks = join_address(self.root, ".envweaver/trajectory-locks")
        _dispatch(locks, {"action": "mkdir", "exist_ok": True})
        with output_lock(join_address(locks, result["id"]), wait=True):
            existing = _dispatch(ref, {"action": "read_json"})
            if existing is not None:
                # Tags may have been edited in the Pool; never replace an existing asset.
                if existing.get("id") != result["id"]:
                    raise FileExistsError(str(ref))
                return str(ref)
            with tempfile.TemporaryDirectory(
                prefix=".archive-",
                dir=self.root if isinstance(self.root, Path) else None,
            ) as temporary:
                stage = Path(temporary) / "rollout"
                stage.mkdir()
                # Read only this attempt, never the retry directories nested below it.
                agent_address = join_address(address, "agent")
                if _dispatch(agent_address, {"action": "exists"}):
                    with local_source(agent_address) as source:
                        for path in [source, *source.rglob("*")]:
                            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                                raise ValueError(
                                    "Rollout artifacts require regular files and directories"
                                )
                        shutil.copytree(source, stage / "agent")
                validate_artifacts(result, stage / "agent")
                result.pop("output_dir", None)
                result.pop("traj_ref", None)
                write_json(stage / "result.json", result, private=True)
                if isinstance(self.root, Path):
                    _dispatch(destination, {"action": "publish_directory", "source": str(stage)})
                else:
                    _, body = pack(stage)
                    _dispatch(
                        destination,
                        {"action": "publish_directory", "body": base64.b64encode(body).decode()},
                    )
        return str(ref)
