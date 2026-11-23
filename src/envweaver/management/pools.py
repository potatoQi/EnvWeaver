"""Selectable Pool locations and an automatically refreshed asset index."""

import base64
import getpass
import logging
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from .._io.files import pack
from .._io.paths import _location, join_address, normalize_address
from .._io.ssh_transport import ssh
from .._task_store import validate_name
from .._util import metadata_tags, validate_goal
from ..errors import RuntimeError as RuntimeFailure
from ..trajectory.pool import validate_record
from . import _pool_files, _ssh
from .runtimes import normalize_ref, same_environment
from .store import ANNOTATIONS, Conflict, digest


class PoolFiles:
    def __init__(self, row):
        self.root = normalize_address(row["address"])
        self.kind = row["kind"]

    def address(self, directory):
        return join_address(
            self.root, "runtimes/" + directory if self.kind == "runtime" else directory
        )

    def call(self, action, **kwargs):
        payload = dict(action=action, kind=self.kind, **kwargs)
        if isinstance(self.root, Path):
            return _pool_files.pool_operation(dict(payload, root=str(self.root)))
        host, path = _location(self.root)
        source = (
            (Path(__file__).parents[1] / "_io/files.py").read_text()
            + "\n"
            + (Path(__file__).parents[1] / "trajectory/usage.py").read_text()
            + "\n"
            + Path(_pool_files.__file__).read_text()
        )
        source += """\nimport sys
try:
    print(json.dumps({'result': pool_operation(json.load(sys.stdin))}))
except (OSError, ValueError, KeyError) as error:
    print(json.dumps({'error': str(error), 'type': type(error).__name__}))
"""
        result = _ssh.call(ssh, host, source, dict(payload, root=path))
        if "error" in result:
            cls = {
                "FileNotFoundError": FileNotFoundError,
                "FileExistsError": FileExistsError,
                "ValueError": ValueError,
            }.get(result["type"], OSError)
            raise cls(result["error"])
        return result["result"]

    def publish(self, directory, stage, expected=None):
        if isinstance(self.root, Path):
            return self.call("publish", directory=directory, expected=expected, stage=str(stage))
        _, body = pack(stage)
        return self.call(
            "publish", directory=directory, expected=expected, body=base64.b64encode(body).decode()
        )


class Pools:
    def __init__(self, store, tasks):
        self.store, self.tasks = store, tasks
        self.lock = RLock()
        self.sync_locks = {}

    def list(self, kind=None):
        with self.store.read():
            return [
                deepcopy(p) for p in self.store.pools.values() if kind is None or p["kind"] == kind
            ]

    def add(self, value):
        address = str(normalize_address(value.address))
        if "://" not in address:
            address = str(Path(address).resolve(strict=True))
        row = dict(
            id=digest([value.kind, address]),
            name=value.name or Path(address).name or address,
            kind=value.kind,
            address=address,
            error=None,
            selected=True,
        )
        files = PoolFiles(row)
        saved = files.call("metadata")
        with self.lock, self.store.transaction(persist=False):
            if row["id"] not in self.store.pools:
                self.store.pools[row["id"]] = row
                self.store.tags[row["id"]] = deepcopy(saved["data"]["tags"])
                self.store.documents[row["id"]] = {**saved, "baseline": deepcopy(saved["data"])}
            else:
                self.store.pools[row["id"]]["name"] = row["name"]
            for pool in self.store.pools.values():
                if pool["kind"] == value.kind:
                    pool["selected"] = pool["id"] == row["id"]
        self.sync(row["id"])
        return self.store.pool_record(row["id"])

    def sync(self, pool_id=None, *, strict=False):
        for pool in self.list():
            if pool_id is not None and pool["id"] != pool_id:
                continue
            with self.lock:
                lock = self.sync_locks.setdefault(pool["id"], RLock())
            with lock:
                try:
                    errors = self._sync(pool)
                    with self.store.read():
                        self.store.pools[pool["id"]]["error"] = "; ".join(errors) or None
                    if errors and strict:
                        raise Conflict(f"Pool {pool['name']} 中有无法读取的资产，请先修复")
                except (OSError, ValueError, RuntimeError, RuntimeFailure) as error:
                    with self.store.read():
                        self.store.pools[pool["id"]]["error"] = str(error)
                    if strict:
                        raise Conflict(f"无法读取 Pool {pool['name']}：{error}") from error
                    logging.getLogger(__name__).warning("Pool %s: %s", pool["address"], error)

    def _sync(self, pool):
        store = self.store
        files = PoolFiles(pool)
        while True:
            with store.read():
                baseline = (store.fingerprint(), store.documents[pool["id"]]["stamp"])
                known = {
                    s["directory"]: s["stamp"]
                    for key, s in store.sources.items()
                    if store.assets[key]["pool_id"] == pool["id"] and s["present"]
                }
            # Network/disk work must not block unrelated reads or file previews.
            result = None
            if known and not isinstance(files.root, Path):
                result = files.call("index", known_digest=_pool_files.index_digest(known))
                if result["unchanged"]:
                    result["items"] = [
                        {"directory": directory, "stamp": stamp}
                        for directory, stamp in known.items()
                    ]
                else:
                    result = None
            if result is None:
                result = files.call("index", known=known)
            saved, items = result["metadata"], result["items"]
            with store.read():
                if baseline != (store.fingerprint(), store.documents[pool["id"]]["stamp"]):
                    # A concurrent edit won the race: never apply its older scan.
                    continue
                if (
                    saved["stamp"] == store.documents[pool["id"]]["stamp"]
                    and len(items) == len(known)
                    and all(
                        not item.get("error") and known.get(item["directory"]) == item.get("stamp")
                        for item in items
                    )
                ):
                    return []
                return self._apply_scan(pool, saved, items)

    def _apply_scan(self, pool, saved, items):
        actor, errors = getpass.getuser(), []
        store = self.store
        with store.transaction(persist=False):
            document = store.documents[pool["id"]]
            if saved["stamp"] != document["stamp"]:
                store.tags[pool["id"]] = deepcopy(saved["data"]["tags"])
                for asset_id, source in store.sources.items():
                    row = store.assets[asset_id]
                    if row["pool_id"] != pool["id"]:
                        continue
                    annotation = saved["data"]["assets"].get(source["directory"], {})
                    row.update(
                        {
                            k: deepcopy(annotation[k])
                            for k in ANNOTATIONS
                            if k in annotation and (k != "host" or pool["kind"] == "task")
                        }
                    )
                store.documents[pool["id"]] = {**saved, "baseline": deepcopy(saved["data"])}
            known = {
                s["directory"]: s
                for i, s in store.sources.items()
                if store.assets[i]["pool_id"] == pool["id"]
            }
            seen = set()
            changes = []
            for item in items:
                directory = item["directory"]
                seen.add(directory)
                old = known.get(directory)
                if item.get("error"):
                    errors.append(f"{directory}: {item['error']}")
                    if old:
                        store.assets[old["asset_id"]].update(
                            state="missing", last_error=item["error"]
                        )
                    continue
                if old and old["stamp"] == item["stamp"] and old["present"]:
                    continue
                changes.append((item, old))
            errors.extend(self._index_changes(pool, changes, saved, actor))
            for directory, old in known.items():
                if directory not in seen and old["present"]:
                    store.sources[old["asset_id"]]["present"] = False
                    store.assets[old["asset_id"]]["deleted"] = True
                    store.touch(old["asset_id"], actor)
            for row in store.assets.values():
                if row["pool_id"] == pool["id"]:
                    store.set_tags(row["id"], metadata_tags(row["metadata"]))
            store.prune_references()
            # This is the in-memory baseline, not a write to the Pool.
            store.documents[pool["id"]]["baseline"] = store.document(pool["id"])
        return errors

    def _index_changes(self, pool, changes, saved, actor):
        if not changes:
            return []
        # One checkpoint for a valid batch; isolate malformed records only on failure.
        try:
            with self.store.transaction(persist=False):
                for item, old in changes:
                    self._index_item(pool, item, old, saved, actor)
            return []
        except (ValueError, KeyError, TypeError, Conflict):
            pass
        errors = []
        for item, old in changes:
            try:
                with self.store.transaction(persist=False):
                    self._index_item(pool, item, old, saved, actor)
            except (ValueError, KeyError, TypeError, Conflict) as error:
                errors.append(f"{item['directory']}: {error}")
                if old:
                    self.store.assets[old["asset_id"]].update(
                        state="missing", last_error=str(error)
                    )
        return errors

    def _index_item(self, pool, item, old, saved, actor):
        store = self.store
        directory = item["directory"]
        spec = item["spec"]
        if pool["kind"] == "task":
            if type(spec.get("version")) is not int or spec["version"] != 1:
                raise ValueError("Unsupported Task version")
            validate_name(spec["name"])
            validate_goal(spec["goal"])
            for key in ("task_runtime_ref", "verifier_runtime_ref"):
                spec[key] = normalize_ref(spec[key])
            name, metadata, host = spec["name"], spec.get("metadata", {}), None
            metadata_tags(metadata)
        elif pool["kind"] == "trajectory":
            validate_record(spec)
            if spec["id"] != directory:
                raise ValueError("Rollout id does not match its directory")
            name, metadata, host = spec["id"], spec["metadata"], None
        else:
            spec = normalize_ref(spec)
            metadata = spec["metadata"]
            location = item.get("metadata", {})
            host, name = location.get("host"), spec["name"]
        annotation = saved["data"]["assets"].get(directory, {})
        if old:
            asset_id = old["asset_id"]
            previous = store.record(asset_id, deleted=True)
            if pool["kind"] == "task":
                host = previous["host"]
            store.update(
                asset_id,
                name=name,
                spec=spec,
                metadata=metadata,
                host=host,
                deleted=False,
                state="missing"
                if previous["state"] == "missing"
                and (
                    same_environment(previous["spec"], spec)
                    if pool["kind"] == "runtime"
                    else previous["spec"] == spec
                )
                else "registered",
            )
            store.touch(asset_id, actor)
        else:
            asset_id = digest([pool["id"], directory])
            store.insert(
                kind=pool["kind"],
                name=name,
                spec=spec,
                host=host,
                actor=actor,
                pool_id=pool["id"],
                asset_id=asset_id,
            )
            timestamp = datetime.fromtimestamp(item["mtime"], timezone.utc).isoformat()
            store.assets[asset_id].update(created_at=timestamp, updated_at=timestamp)
            store.assets[asset_id].update(
                {
                    k: deepcopy(annotation[k])
                    for k in ANNOTATIONS
                    if k in annotation and (k != "host" or pool["kind"] == "task")
                }
            )
            if annotation and annotation.get("stamp") != item["stamp"]:
                store.assets[asset_id].update(deleted=False, state="registered")
                store.touch(asset_id, actor)
        store.remember_source(asset_id, directory, item["stamp"])
        if pool["kind"] == "trajectory":
            store.assets[asset_id]["token_usage"] = item["token_usage"]
        if pool["kind"] == "task":
            self.tasks.link(asset_id, spec, store.assets[asset_id]["host"], actor)
        elif pool["kind"] == "runtime":
            for link in store.relations(asset_id):
                task = link["asset"]
                self.tasks.link(task["id"], task["spec"], task["host"], actor)
            aliases = {
                r["id"]
                for r in store.assets.values()
                if r["kind"] == "runtime"
                and same_environment(r["spec"], spec)
                and r["host"] == host
                and (not store.assets[asset_id]["deleted"] or r["pool_id"] is None)
            }
            for refs in store.links.values():
                for role, ref in refs.items():
                    if ref in aliases:
                        refs[role] = asset_id
