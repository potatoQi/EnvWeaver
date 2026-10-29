"""Two-step deletion with server-side dependency and stale-confirmation protection."""

import secrets
from datetime import datetime, timezone

from .store import Conflict, digest, now


class Deletion:
    def __init__(self, store, runtimes, pools):
        self.store, self.runtimes, self.pools = store, runtimes, pools

    def impact(self, ids, physical):
        selected = [self.store.record(i) for i in ids]
        runtime_ids = {r["id"] for r in selected if r["kind"] == "runtime"}
        identities = {}
        if physical and runtime_ids:
            if any(r["state"] != "registered" for r in selected):
                raise ValueError("所选环境实体已标记缺失；请仅删除记录")
            backends = {r["spec"]["backend"] for r in selected}
            candidates = [
                self.store.record(r["id"], deleted=True)
                for r in self.store.assets.values()
                if r["kind"] == "runtime" and r["state"] == "registered"
            ]
            for row in candidates:
                if row["spec"]["backend"] in backends:
                    identities[row["id"]] = self.runtimes.files.inspect(row)
            keys = [identities[i]["key"] for i in runtime_ids]
            runtime_ids |= {
                i
                for i, identity in identities.items()
                if identity["key"] in keys or any(k in identity["dependencies"] for k in keys)
            }
        affected = {}
        for runtime_id in runtime_ids:
            for link in self.store.relations(runtime_id):
                task = link["asset"]
                if task["id"] not in ids:
                    affected[task["id"]] = {"id": task["id"], "name": task["name"]}
        trajectories = {link["asset"]["id"]: {"id": link["asset"]["id"], "name": link["asset"]["name"]}
                        for row in selected if row["kind"] == "task"
                        for link in self.store.relations(row["id"])
                        if link["role"] == "trajectory" and link["asset"]["id"] not in ids}
        return {
            "affected_trajectories": sorted(trajectories.values(), key=lambda r: r["id"]),
            "selected": [{"id": r["id"], "kind": r["kind"], "name": r["name"]} for r in selected],
            "affected_tasks": sorted(affected.values(), key=lambda r: r["id"]),
            "affected_runtimes": [
                {"id": i, "name": self.store.record(i, deleted=True)["name"]}
                for i in sorted(runtime_ids - set(ids))
            ],
            "identities": identities,
        }

    def plan(self, request, actor):
        self.pools.sync(strict=True)
        with self.store.transaction() as state:
            ids = self.store.select(request.selection)
            if not ids:
                raise ValueError("请至少选择一条记录")
            impact = self.impact(ids, request.physical)
            body = {"ids": ids, "physical": request.physical, "impact": impact}
            token = secrets.token_urlsafe(32)
            state.plans = {
                key: plan
                for key, plan in state.plans.items()
                if (
                    datetime.now(timezone.utc) - datetime.fromisoformat(plan["created_at"])
                ).total_seconds()
                <= 600
            }
            state.plans[token] = dict(
                actor=actor,
                body=body,
                fingerprint=self.store.fingerprint(),
                created_at=now(),
            )
            return {
                "token": token,
                "physical": request.physical,
                "selected": impact["selected"],
                "affected_tasks": impact["affected_tasks"],
                "affected_runtimes": impact["affected_runtimes"],
                "affected_trajectories": impact["affected_trajectories"],
                "expires_in": 600,
            }

    def execute(self, request, actor):
        self.pools.sync(strict=True)
        with self.store.transaction() as state:
            plan = state.plans.get(request.token)
            if plan is None or plan["actor"] != actor:
                raise Conflict("删除确认不存在、已使用或不属于当前用户")
            age = (
                datetime.now(timezone.utc) - datetime.fromisoformat(plan["created_at"])
            ).total_seconds()
            if age > 600 or plan["fingerprint"] != self.store.fingerprint():
                raise Conflict("资产或关联关系已变化，请重新查看删除影响并确认")
            body = plan["body"]
            current = self.impact(body["ids"], body["physical"])
            if digest(current) != digest(body["impact"]):
                raise Conflict("实际环境或依赖已变化，请重新确认")
            if (
                current["affected_tasks"] or current["affected_runtimes"] or current["affected_trajectories"]
            ) and not request.acknowledge_dependencies:
                raise Conflict("存在关联资产，须明确确认影响")
            del state.plans[request.token]
            deleted, errors, removed_keys = [], [], []
            for asset_id in body["ids"]:
                row = self.store.record(asset_id)
                try:
                    if body["physical"]:
                        if row["kind"] in {"task", "trajectory"}:
                            pool, source = self.store.source(asset_id)
                            pool.call(
                                "delete", directory=source["directory"], expected=source["stamp"]
                            )
                            state.sources[asset_id]["present"] = False
                        else:
                            identity = current["identities"][asset_id]
                            if identity["key"] not in removed_keys:
                                self.runtimes.files.delete(row, identity)
                                removed_keys.append(identity["key"])
                                for other_id, other in current["identities"].items():
                                    if (
                                        other["key"] == identity["key"]
                                        or identity["key"] in other["dependencies"]
                                    ):
                                        state.assets[other_id]["state"] = "missing"
                                        self.store.touch(other_id, actor)
                    state.assets[asset_id]["deleted"] = True
                    self.store.touch(asset_id, actor)
                    deleted.append(asset_id)
                except Exception as error:
                    self.store.touch(asset_id, actor)
                    state.assets[asset_id]["last_error"] = str(error)
                    errors.append({"id": asset_id, "name": row["name"], "error": str(error)})
            return {"deleted": deleted, "errors": errors}
