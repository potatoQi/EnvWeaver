"""Atomic batch journal shared by local and SSH output directories."""

import hashlib
import json
import re
import sys
import threading
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from .._io.output import OutputDirectory
from .._io.output_lock import output_lock
from .._io.paths import join_address
from .._io.storage import _dispatch
from .._util import write_json
from ..errors import ErrorType
from ..verifier import _entry_directory


def fingerprint(value):
    def encode(value):
        if isinstance(value, Path):
            return str(value)
        raise TypeError(f"Configuration is not JSON serializable: {type(value).__name__}")

    body = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, default=encode)
    return hashlib.sha256(body.encode()).hexdigest()


def configuration(agent, config):
    """Record readable settings without persisting credentials or opaque CLI arguments."""
    values = {"agent": agent._identity(), "eval": asdict(config)}

    def redact(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key in ("env", "verifier_env") and isinstance(item, dict):
                    result[key] = {name: "[REDACTED]" for name in item}
                elif key == "extra_args":
                    result[key] = ["[REDACTED]" for _ in item] if isinstance(item, list) else "[REDACTED]"
                elif re.search(
                    r"password|secret|credential|authorization|api[_-]?key|private[_-]?key"
                    r"|(?:^|[_-])(?:key|token|auth)(?:$|[_-])", str(key), re.I,
                ):
                    result[key] = "[REDACTED]"
                else:
                    result[key] = redact(item)
            return result
        if isinstance(value, (list, tuple)):
            return [redact(item) for item in value]
        return str(value) if isinstance(value, Path) else deepcopy(value)

    return {"values": redact(values), "fingerprint": fingerprint(values)}


def inputs(task):
    for block in task.goal:
        if isinstance(block, dict) and block.get("type") == "image":
            yield block["path"], "file"
    for item in task.context:
        yield item["source"], None
    yield task.verifier.entry, "file"
    yield _entry_directory(task.verifier.entry), "directory"


def inspect_task(task):
    """Read inputs once, returning an identity and any preflight failure."""
    values = deepcopy({
        key: value for key, value in vars(task).items()
        if key not in {"verifier", "task_runtime_ref", "verifier_runtime_ref"}
    })
    values["task_runtime_ref"] = task.task_runtime_ref.to_dict()
    values["verifier_runtime_ref"] = task.verifier_runtime_ref.to_dict()
    values["verifier"] = {
        key: getattr(task.verifier, key)
        for key in ("entry", "timeout", "required_env", "requires_task_instance")
    }
    materials = {}
    input_error = None
    try:
        for address, kind in inputs(task):
            key = str(address)
            if key not in materials:
                materials[key] = _dispatch(address, {"action": "fingerprint"})
            if kind is not None and materials[key]["kind"] != kind:
                raise ValueError(f"Expected {kind}: {address}")
    except Exception as error:
        # The evaluation records invalid inputs as preflight failures.
        input_error = error
    # SSH Tasks are unpacked into a different temporary directory on each
    # load. Compare their content, not those temporary absolute paths.
    for blocks, key in ((values["goal"], "path"), (values["context"], "source")):
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and key in block:
                    block[key] = materials.get(str(block[key]), block[key])
    entry = values["verifier"]["entry"]
    values["verifier"]["entry"] = materials.get(str(entry), str(entry))
    directory = str(_entry_directory(entry))
    values["verifier"]["materials"] = materials.get(directory)
    return fingerprint([values, type(input_error).__name__ if input_error else None]), input_error


class EvalStore:
    def __init__(self, address, resume):
        self.lock = threading.RLock()
        manifest = join_address(address, "manifest.json")
        if resume and not _dispatch(manifest, {"action": "exists"}):
            raise ValueError("resume requires an existing Eval manifest")
        self.store = OutputDirectory(address, exist_ok=resume)
        self._lease = output_lock(join_address(address, ".eval.lock"))
        self._writer = self._lease.__enter__()
        try:
            if resume:
                self.data = _dispatch(manifest, {"action": "read_json"})
                self._validate(self.data)
            else:
                self.data = {"format": 1, "tasks": {}, "count": None}
            self._save()
        except BaseException:
            self._lease.__exit__(*sys.exc_info())
            raise

    def __enter__(self):
        return self

    def __exit__(self, *error):
        return self._lease.__exit__(*error)

    @staticmethod
    def _validate(data):
        error_types = {item.value for item in ErrorType}

        def valid_configuration(settings):
            return (
                isinstance(settings, dict)
                and isinstance(settings.get("values"), dict)
                and all(isinstance(settings["values"].get(key), dict) for key in ("agent", "eval"))
                and isinstance(settings.get("fingerprint"), str)
                and re.fullmatch(r"[0-9a-f]{64}", settings["fingerprint"]) is not None
            )

        def valid_result(result):
            return result is None or (
                isinstance(result, dict)
                and isinstance(result.get("errors"), list)
                and all(
                    isinstance(error, dict)
                    and set(error) == {"stage", "type", "message"}
                    and isinstance(error["stage"], str)
                    and error["stage"] in ("preflight", "setup", "agent", "verify", "cleanup")
                    and isinstance(error["type"], str) and error["type"] in error_types
                    and isinstance(error["message"], str)
                    for error in result["errors"]
                )
            )

        valid = (
            isinstance(data, dict) and type(data.get("format")) is int and data["format"] == 1
            and isinstance(data.get("tasks"), dict)
            and (data.get("count") is None or type(data["count"]) is int and data["count"] >= 0)
        )
        if valid:
            for key, entry in data["tasks"].items():
                valid = (
                    key.isdecimal() and isinstance(entry, dict)
                    and isinstance(entry.get("task"), str)
                    and type(entry.get("complete")) is bool
                    and isinstance(entry.get("attempts"), list)
                    and valid_result(entry.get("result"))
                    and (not entry["complete"] or isinstance(entry["result"], dict))
                    and all(
                        isinstance(attempt, dict)
                        and valid_configuration(attempt.get("configuration"))
                        and valid_result(attempt.get("result"))
                        for attempt in entry["attempts"]
                    )
                )
                if not valid:
                    break
        if not valid:
            raise ValueError("Invalid Eval manifest")

    @property
    def address(self):
        return self.store.address

    @property
    def path(self):
        return self.store.path

    def _save(self):
        if self._writer.poll() is not None:
            raise OSError("Eval output lock was lost")
        write_json(self.path / "manifest.json", self.data, private=True)
        self.store.sync()

    def register(self, index, task):
        task_signature, input_error = inspect_task(task)
        key = str(index)
        with self.lock:
            entry = self.data["tasks"].get(key)
            if entry is not None:
                if entry["task"] != task_signature:
                    raise ValueError(f"Task changed at index {index}")
            else:
                if self.data["count"] is not None and index >= self.data["count"]:
                    raise ValueError("Task count differs from the saved batch")
                entry = self.data["tasks"][key] = {
                    "task": task_signature,
                    "attempts": [], "complete": False, "result": None,
                }
                self._save()
            return entry, input_error

    def start(self, index, agent, config):
        settings = configuration(agent, config)
        with self.lock:
            entry = self.data["tasks"][str(index)]
            number = len(entry["attempts"]) + 1
            directory = f"{index + 1:06d}"
            if number > 1:
                directory += f"/attempt-{number:06d}"
            entry["attempts"].append({
                "output_dir": directory, "configuration": settings, "result": None,
            })
            self._save()
            return directory

    def finish(self, index, result, complete):
        with self.lock:
            result = deepcopy(result)
            entry = self.data["tasks"][str(index)]
            entry["attempts"][-1]["result"] = result
            entry["result"] = result
            entry["complete"] = complete
            self._save()

    def exhausted(self, count):
        with self.lock:
            if self.data["count"] not in (None, count) or any(
                int(index) >= count for index in self.data["tasks"]
            ):
                raise ValueError("Task count differs from the saved batch")
            self.data["count"] = count
            self._save()
