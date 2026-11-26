"""Session index; durable annotations belong to the selected Pool directories."""

import hashlib
import json
import uuid
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock

from .._io.paths import join_address, normalize_address
from .._util import clean_tags, metadata_tags
from ..trajectory.pool import completed


class Conflict(Exception):
    """The supplied revision or deletion confirmation is no longer current."""


class Missing(Exception):
    """A Pool or asset was not found."""


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


ANNOTATIONS = (
    "host",
    "created_by",
    "updated_by",
    "created_at",
    "updated_at",
    "revision",
    "deleted",
    "state",
    "last_error",
)


class Store:
    def __init__(self):
        self.lock = RLock()
        self.pools = {}
        self.assets = {}
        self.sources = {}
        self.tags = {}
        self.links = {}
        self.plans = {}
        self.documents = {}
        self.depth = 0

    @contextmanager
    def read(self):
        with self.lock:
            yield self

    @contextmanager
    def transaction(self, *, persist=True):
        with self.lock:
            fields = ("pools", "assets", "sources", "tags", "links", "plans", "documents")
            snapshot = {key: deepcopy(getattr(self, key)) for key in fields}
            self.depth += 1
            try:
                yield self
                if persist and self.depth == 1:
                    self.flush()
            except BaseException:
                for key, value in snapshot.items():
                    setattr(self, key, value)
                raise
            finally:
                self.depth -= 1

    def flush(self):
        from .pools import PoolFiles

        changes = {}
        for asset_id, source in self.sources.items():
            row = self.assets[asset_id]
            if source["present"] and row["metadata"] != source["metadata"]:
                changes.setdefault(row["pool_id"], []).append((asset_id, source))
        for pool_id, entries in changes.items():
            stamps = PoolFiles(self.pools[pool_id]).call(
                "asset_metadata_batch",
                updates=[
                    dict(
                        directory=source["directory"],
                        expected=source["stamp"],
                        metadata=self.assets[asset_id]["metadata"],
                    )
                    for asset_id, source in entries
                ],
            )
            for (asset_id, source), stamp in zip(entries, stamps, strict=True):
                self.remember_source(asset_id, source["directory"], stamp)

        for pool_id, pool in self.pools.items():
            document = self.document(pool_id)
            previous = self.documents[pool_id]
            # Reading or indexing a Pool never creates a sidecar. Only changed annotations do.
            if document == previous["baseline"]:
                continue
            saved = PoolFiles(pool).call("metadata", data=document, expected=previous["stamp"])
            self.documents[pool_id] = {**saved, "baseline": deepcopy(document)}

    def document(self, pool_id):
        document = {
            "tags": deepcopy(self.tags.get(pool_id, {})),
            "assets": deepcopy(self.documents[pool_id]["data"].get("assets", {})),
        }
        for asset_id, source in self.sources.items():
            row = self.assets[asset_id]
            if row["pool_id"] == pool_id:
                document["assets"][source["directory"]] = {
                    **{
                        key: deepcopy(row[key])
                        for key in ANNOTATIONS
                        if (key != "host" or row["kind"] == "task")
                    },
                    "stamp": source["stamp"],
                }
        return document

    def fingerprint(self):
        with self.read():
            return digest(
                [
                    [r["id"], r["revision"], r["deleted"], r["state"]]
                    for r in sorted(self.assets.values(), key=lambda r: r["id"])
                ]
            )

    def resolve_pool(self, kind, pool_id):
        if pool_id is None:
            choices = [p["id"] for p in self.pools.values() if p["kind"] == kind]
            if len(choices) != 1:
                raise ValueError(f"请指定 {kind} Pool")
            return choices[0]
        self.pool_record(pool_id, kind)
        return pool_id

    def pool_record(self, pool_id, kind=None):
        row = self.pools.get(pool_id)
        if row is None or (kind is not None and row["kind"] != kind):
            raise Missing("Pool 不存在或类型不符，请先选择目录")
        return deepcopy(row)

    def source(self, asset_id):
        from .pools import PoolFiles

        source = self.sources.get(asset_id)
        if source is None:
            raise Missing("资产没有对应的 Pool 文件")
        return PoolFiles(self.pool_record(self.assets[asset_id]["pool_id"])), deepcopy(source)

    def source_address(self, asset_id):
        with self.read():
            files, row = self.source(asset_id)
            return files.address(row["directory"])

    def remember_source(self, asset_id, directory, stamp):
        self.sources[asset_id] = dict(
            asset_id=asset_id,
            directory=directory,
            stamp=stamp,
            present=True,
            metadata=deepcopy(self.assets[asset_id]["metadata"]),
        )

    def record(self, asset_id, *, deleted=False):
        row = self.assets.get(asset_id)
        if row is None or (row["deleted"] and not deleted):
            raise Missing("资产不存在")
        item = deepcopy(row)
        if item["kind"] == "task" and any(
            self.assets[ref]["deleted"] or self.assets[ref]["state"] == "missing"
            for ref in self.links.get(asset_id, {}).values()
        ):
            item["state"] = "missing"
        return item

    def get(self, asset_id):
        with self.read():
            return self.record(asset_id)

    def update(self, asset_id, **values):
        row = self.assets[asset_id]
        name = values.get("name", row["name"])
        if row["kind"] != "runtime" and row["pool_id"] and any(
            other["id"] != asset_id
            and other["pool_id"] == row["pool_id"]
            and other["name"] == name
            and not other["deleted"]
            for other in self.assets.values()
        ):
            raise Conflict("Pool 中已有同名资产")
        row.update(deepcopy(values))

    def insert(
        self,
        *,
        kind,
        name,
        spec,
        actor,
        pool_id,
        host=None,
        asset_id=None,
    ):
        asset_id = asset_id or uuid.uuid4().hex
        if pool_id is not None:
            self.pool_record(pool_id, kind)
        if asset_id in self.assets and not self.assets[asset_id]["deleted"]:
            raise Conflict("资产 ID 已存在")
        timestamp = now()
        metadata = spec.get("metadata", {})
        self.assets[asset_id] = dict(
            id=asset_id,
            kind=kind,
            name=name,
            spec=deepcopy(spec),
            metadata=deepcopy(metadata),
            host=host,
            created_by=actor,
            updated_by=actor,
            created_at=timestamp,
            updated_at=timestamp,
            pool_id=pool_id,
            revision=1,
            deleted=False,
            state="registered",
            last_error=None,
            tags=[],
        )
        self.update(asset_id, name=name)
        self.set_tags(asset_id, metadata_tags(metadata))
        return asset_id

    def set_tags(self, asset_id, tags):
        row = self.assets[asset_id]
        tags = clean_tags(tags)
        if row["pool_id"] is not None:
            for tag in tags:
                self.tags.setdefault(row["pool_id"], {}).setdefault(tag, "#2563eb")
        row["tags"] = tags
        if tags != metadata_tags(row["metadata"]):
            row["metadata"]["tags"] = tags
            row["spec"]["metadata"] = deepcopy(row["metadata"])

    def check_revision(self, row, revision):
        if row["revision"] != revision:
            raise Conflict("记录已被修改，请刷新后重试")

    def touch(self, asset_id, actor):
        row = self.assets[asset_id]
        row.update(
            revision=row["revision"] + 1, updated_by=actor, updated_at=now(), last_error=None
        )

    @staticmethod
    def backend(row):
        spec = row["spec"]
        return spec.get("backend", "") if row["kind"] == "runtime" else spec.get("task_runtime_ref", {}).get("backend", "")

    @staticmethod
    def task_group(row):
        ref = row["spec"].get("task_ref")
        return "ref:" + ref if ref else "rollout:" + row["id"]

    @staticmethod
    def rollout(row):
        spec = row["spec"]
        info = (spec.get("agent_result") or {}).get("metadata", {})
        config = spec.get("configuration", {}).get("agent", {})
        return dict(
            agent=info.get("agent") or config.get("config", {}).get("name") or config.get("class", "").rsplit(".", 1)[-1] or None, model=info.get("model") or config.get("config", {}).get("model"),
            score=(spec.get("verification_result") or {}).get("score") if spec.get("score_valid") else None,
            duration_seconds=spec.get("duration_seconds"),
            **row.get("trajectory_stats", {}),
            error_count=len(spec["errors"]),
            agent_stop_reason=spec.get("agent_stop_reason"), completed=completed(spec),
            rollout_id=spec["id"], started_at=spec.get("started_at"),
        )

    def _pool_rows(self, kind, pool_id):
        pool_id = self.resolve_pool(kind, pool_id)
        return (
            row for row in self.assets.values()
            if row["pool_id"] == pool_id and row["kind"] == kind and not row["deleted"]
        )

    def matching(self, selection):
        rows = self._pool_rows(selection.kind, selection.pool_id)
        if selection.score_min is not None and selection.score_max is not None and selection.score_min > selection.score_max:
            raise ValueError("分数下限不能大于上限")
        ranges = {}
        for field in ("created", "updated"):
            bounds = []
            for suffix in ("after", "before"):
                value = getattr(selection, f"{field}_{suffix}")
                parsed = datetime.fromisoformat(value) if value else None
                if parsed is not None and parsed.tzinfo is None:
                    raise ValueError("筛选时间须包含时区")
                bounds.append(parsed)
            lower, upper = bounds
            if lower is not None and upper is not None and lower > upper:
                raise ValueError("时间范围起不能晚于时间范围终")
            if lower is not None or upper is not None:
                ranges[f"{field}_at"] = (lower, upper)
        if any(isinstance(value, (dict, list)) for value in selection.metadata.values()):
            raise ValueError("metadata 筛选值须为文字、数字、布尔值或 null")
        if selection.kind != "trajectory" and (selection.task_groups or selection.exclude_task_groups):
            raise ValueError("只有 Traj Pool 支持按 Task 分组选择")
        ids, excluded = set(selection.ids), set(selection.exclude_ids)
        groups, excluded_groups = set(selection.task_groups), set(selection.exclude_task_groups)
        for row in rows:
            group = self.task_group(row) if selection.kind == "trajectory" else None
            if not selection.include_all and (ids or groups) and row["id"] not in ids and group not in groups:
                continue
            if row["id"] in excluded or (group in excluded_groups and row["id"] not in ids):
                continue
            searchable = row["name"] + (" " + row["spec"]["name"] if selection.kind == "trajectory" else "")
            if selection.query.lower() not in searchable.lower():
                continue
            if not set(selection.tags) <= set(row["tags"]):
                continue
            if selection.creators and row["created_by"] not in selection.creators:
                continue
            if selection.kind == "trajectory":
                if selection.task_group is not None and group != selection.task_group:
                    continue
                info = self.rollout(row)
                if selection.agents and info["agent"] not in selection.agents:
                    continue
                if selection.models and info["model"] not in selection.models:
                    continue
                if selection.stop_reasons and info["agent_stop_reason"] not in selection.stop_reasons:
                    continue
                if selection.completed is not None and info["completed"] != selection.completed:
                    continue
                if selection.score_min is not None and (info["score"] is None or info["score"] < selection.score_min):
                    continue
                if selection.score_max is not None and (info["score"] is None or info["score"] > selection.score_max):
                    continue
            if selection.backends and self.backend(row) not in selection.backends:
                continue
            if any(
                (lower is not None and datetime.fromisoformat(row[field]) < lower)
                or (upper is not None and datetime.fromisoformat(row[field]) > upper)
                for field, (lower, upper) in ranges.items()
            ):
                continue
            if any(
                key not in row["metadata"]
                or type(row["metadata"][key]) is not type(value)
                or row["metadata"][key] != value
                for key, value in selection.metadata.items()
            ):
                continue
            yield row

    def select(self, selection):
        ids = [
            r["id"] for r in sorted(self.matching(selection), key=lambda r: (r["name"], r["id"]))
        ]
        if not (set(selection.ids) - set(selection.exclude_ids)) <= set(ids):
            raise Conflict("部分选中记录已删除或不再符合筛选条件")
        return ids

    def listing(self, selection, *, page=1, page_size=30, sort="updated_at", descending=True, group_by=None):
        if sort not in {"name", "created_at", "updated_at"}:
            raise ValueError("不支持的排序字段")
        with self.read():
            rows = sorted(self.matching(selection), key=lambda r: r["id"])
            rows.sort(key=lambda r: r["spec"]["name"] if sort == "name" and r["kind"] == "trajectory" else r[sort], reverse=descending)
            if group_by is not None:
                if group_by != "task" or selection.kind != "trajectory":
                    raise ValueError("只有 Traj Pool 支持按 Task 分组")
                groups = {}
                # Rows are already sorted: each group follows its first matching rollout.
                for row in rows:
                    key = self.task_group(row)
                    if key not in groups:
                        groups[key] = dict(key=key, name=row["spec"]["name"], count=0)
                    groups[key]["count"] += 1
                return dict(
                    items=[], groups=list(groups.values())[(page - 1) * page_size:page * page_size],
                    group_total=len(groups), total=len(rows), page=page, page_size=page_size,
                )
            return dict(
                items=[
                    dict(
                        id=r["id"],
                        name=r["spec"]["name"] if r["kind"] == "trajectory" else r["name"],
                        **(self.rollout(r) if r["kind"] == "trajectory" else {}),
                        tags=list(r["tags"]),
                        created_by=r["created_by"],
                        created_at=r["created_at"],
                        updated_by=r["updated_by"],
                        updated_at=r["updated_at"],
                        backend=self.backend(r),
                    )
                    for r in rows[(page - 1) * page_size : page * page_size]
                ],
                total=len(rows),
                page=page,
                page_size=page_size,
            )

    def batch_tags(self, selection, add, remove, actor):
        add, remove = set(clean_tags(add)), set(clean_tags(remove))
        if add & remove:
            raise ValueError("同一个标签不能同时添加和移除")
        with self.transaction():
            ids = self.select(selection)
            for asset_id in ids:
                tags = self.assets[asset_id]["tags"]
                self.set_tags(asset_id, (set(tags) | add) - remove)
                self.touch(asset_id, actor)
        return {"updated": len(ids)}

    def filter_options(self, kind, pool_id):
        options = {key: set() for key in ("backends", "creators", "agents", "models")}
        with self.read():
            for row in self._pool_rows(kind, pool_id):
                options["backends"].add(self.backend(row))
                options["creators"].add(row["created_by"])
                if kind == "trajectory":
                    rollout = self.rollout(row)
                    if rollout["agent"]:
                        options["agents"].add(rollout["agent"])
                    if rollout["model"]:
                        options["models"].add(rollout["model"])
        return {key: sorted(values) for key, values in options.items()}

    def tag_counts(self, kind, pool_id=None):
        with self.read():
            pool_id = self.resolve_pool(kind, pool_id)
            tags = self.tags.get(pool_id)
            if not tags:
                return []
            counts = Counter(
                tag
                for row in self.assets.values()
                if row["pool_id"] == pool_id and not row["deleted"]
                for tag in row["tags"]
            )
            return [
                dict(tag=tag, color=color, count=counts[tag])
                for tag, color in sorted(tags.items())
            ]

    def create_tag(self, kind, value, pool_id=None):
        with self.transaction():
            pool_id = self.resolve_pool(kind, pool_id)
            tags = self.tags.setdefault(pool_id, {})
            if value.tag in tags:
                raise Conflict("标签已存在")
            tags[value.tag] = value.color
        return value

    def update_tag(self, kind, tag, value, actor, pool_id=None):
        with self.transaction():
            pool_id = self.resolve_pool(kind, pool_id)
            tags = self.tags.setdefault(pool_id, {})
            if tag not in tags:
                raise Missing("标签不存在")
            if value.tag != tag and value.tag in tags:
                raise Conflict("标签已存在")
            del tags[tag]
            tags[value.tag] = value.color
            for row in self.assets.values():
                if row["pool_id"] == pool_id and tag in row["tags"]:
                    self.set_tags(row["id"], [value.tag if t == tag else t for t in row["tags"]])
                    self.touch(row["id"], actor)
        return value

    def delete_tag(self, kind, tag, actor, pool_id=None):
        with self.transaction():
            pool_id = self.resolve_pool(kind, pool_id)
            if tag not in self.tags.get(pool_id, {}):
                raise Missing("标签不存在")
            del self.tags[pool_id][tag]
            count = 0
            for row in self.assets.values():
                if row["pool_id"] == pool_id and tag in row["tags"]:
                    self.set_tags(row["id"], [t for t in row["tags"] if t != tag])
                    self.touch(row["id"], actor)
                    count += 1
        return {"updated": count}

    def prune_references(self):
        referenced = {ref for refs in self.links.values() for ref in refs.values()}
        self.assets = {
            key: row
            for key, row in self.assets.items()
            if row["pool_id"] is not None or key in referenced
        }

    def relations(self, asset_id):
        asset = self.record(asset_id, deleted=True)
        if asset["kind"] == "task":
            pairs = [(role, ref) for role, ref in sorted(self.links.get(asset_id, {}).items())]
        elif asset["kind"] == "trajectory":
            address = asset["spec"].get("task_ref")
            pairs = [("task", row["id"]) for row in self.assets.values()
                     if address and row["kind"] == "task" and not row["deleted"] and row["id"] in self.sources
                     and str(normalize_address(address)) == str(join_address(self.source_address(row["id"]), "task.json"))]
        else:
            pairs = [
                (role, task_id)
                for task_id, refs in self.links.items()
                if not self.assets[task_id]["deleted"]
                for role, ref in refs.items()
                if ref == asset_id
            ]
            pairs.sort(key=lambda p: self.assets[p[1]]["name"])
        if asset["kind"] == "task" and asset_id in self.sources:
            address = str(join_address(self.source_address(asset_id), "task.json"))
            pairs += [("trajectory", row["id"]) for row in self.assets.values()
                      if row["kind"] == "trajectory" and not row["deleted"]
                      and row["spec"].get("task_ref") and str(normalize_address(row["spec"]["task_ref"])) == address]
        return [{"role": role, "asset": self.record(ref, deleted=True)} for role, ref in pairs]
