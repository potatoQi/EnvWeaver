import io
import json
import shutil
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from envweaver import TrajPool
from envweaver._util import read_json
from envweaver.management.api import create_app
from tests.management.test_api import package, wait
from tests.trajectory.test_pool import rollout


def test_task_groups_paginate_tasks_and_filter_rollouts(tmp_path):
    pool = TrajPool(tmp_path / "pool")
    for index, ref in enumerate(["/a/task.json"] * 3 + ["/b/task.json", None, None]):
        source = tmp_path / f"source-{index}"
        rollout(source, task_ref=ref, score=index % 2)
        pool.save(output_dir=source)
    with TestClient(create_app()) as client:
        pool_id = choose(client, pool.root)
        query = dict(
            selection={"kind": "trajectory", "pool_id": pool_id}, group_by="task", page_size=2
        )
        first = client.post("/api/assets/page", json=query).json()
        second = client.post("/api/assets/page", json={**query, "page": 2}).json()
        assert first["total"] == 6 and first["group_total"] == 4
        assert first["items"] == []
        groups = first["groups"] + second["groups"]
        assert len({g["key"] for g in groups}) == 4
        assert {g["key"]: g["count"] for g in groups}["ref:/a/task.json"] == 3
        assert all(g["name"] == "example" for g in groups)
        # Same display name never merges distinct source addresses or unknown sources.
        children = listing(client, pool_id, task_group="ref:/a/task.json")
        assert children["total"] == 3
        assert len({r["rollout_id"] for r in children["items"]}) == 3
        assert listing(client, pool_id, task_group="ref:/a/task.json", score_min=1)["total"] == 1
        filtered = client.post(
            "/api/assets/page",
            json={
                **query,
                "selection": {**query["selection"], "score_min": 1},
                "page_size": 30,
            },
        ).json()
        assert filtered["total"] == filtered["group_total"] == 3
        assert all(g["count"] == 1 for g in filtered["groups"])
        assert listing(client, pool_id, task_group="ref:/missing/task.json")["total"] == 0


def choose(client, root, kind="trajectory"):
    response = client.post("/api/pools", json={"kind": kind, "address": str(root)})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_group_selection_and_bulk_operations(tmp_path):
    pool = TrajPool(tmp_path / "pool")
    for index, ref in enumerate(["/a/task.json"] * 3 + ["/b/task.json"]):
        source = tmp_path / f"source-{index}"
        rollout(source, task_ref=ref, score=index % 2)
        pool.save(output_dir=source)
    with TestClient(create_app()) as client:
        pool_id = choose(client, pool.root)
        group = "ref:/a/task.json"
        members = listing(client, pool_id, task_group=group)["items"]
        first = members[0]["id"]
        other = listing(client, pool_id, task_group="ref:/b/task.json")["items"][0]["id"]
        assert listing(client, pool_id, task_groups=[group])["total"] == 3
        assert listing(client, pool_id, task_groups=[group], ids=[other])["total"] == 4
        assert listing(client, pool_id, task_groups=[group], exclude_ids=[first])["total"] == 2
        assert listing(client, pool_id, task_groups=[group], score_min=1)["total"] == 1
        selection = dict(
            kind="trajectory", pool_id=pool_id, include_all=True,
            exclude_task_groups=[group], ids=[first],
        )
        assert {r["id"] for r in listing(client, pool_id, **{
            k: v for k, v in selection.items() if k not in {"kind", "pool_id"}
        })["items"]} == {first, other}
        assert listing(
            client, pool_id, include_all=True, exclude_task_groups=[group],
            ids=[first], exclude_ids=[first],
        )["total"] == 1
        response = client.post("/api/assets/tags", json={"selection": selection, "add": ["chosen"]})
        assert response.status_code == 200, response.text
        assert {r["id"] for r in listing(client, pool_id, tags=["chosen"])["items"]} == {first, other}
        response = client.post("/api/assets/tags", json={
            "selection": {"kind": "trajectory", "pool_id": pool_id, "task_groups": [group]},
            "add": ["whole-group"],
        })
        assert response.status_code == 200, response.text
        assert listing(client, pool_id, tags=["whole-group"])["total"] == 3
        preview = client.post("/api/delete/plan", json={"selection": selection}).json()
        assert {row["id"] for row in preview["selected"]} == {first, other}
        exported = client.post("/api/trajectories/export", json=selection)
        assert exported.status_code == 200, exported.text
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            expected = {row["rollout_id"] for row in listing(client, pool_id, tags=["chosen"])["items"]}
            assert {path.split("/")[0] for path in archive.namelist()} == expected
        response = client.post("/api/assets/tags", json={
            "selection": {**selection, "ids": ["missing"]}, "add": ["invalid"],
        })
        assert response.status_code == 409
        assert listing(client, pool_id, tags=["invalid"])["total"] == 0


def listing(client, pool, **filters):
    response = client.post(
        "/api/assets/page", json={"selection": {"kind": "trajectory", "pool_id": pool, **filters}}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_listing_tags_filters_details_export_and_delete(tmp_path):
    pool = TrajPool(tmp_path / "pool", completed_only=False)
    for number, score in enumerate([0, 1, None]):
        source = tmp_path / f"source-{number}"
        rollout(source, score=score, reason="error" if score is None else "completed")
        pool.save(output_dir=source)
        shutil.rmtree(source)
    with TestClient(create_app()) as client:
        pool_id = choose(client, pool.root)
        data = listing(client, pool_id)
        assert data["total"] == 3
        assert data["models"] == ["test-model"] and data["agents"] == ["fixture"]
        assert listing(client, pool_id, completed=True)["total"] == 2
        assert listing(client, pool_id, score_min=0, score_max=0)["total"] == 1
        assert listing(client, pool_id, score_min=1)["total"] == 1
        assert listing(client, pool_id, stop_reasons=["error"])["total"] == 1
        assert listing(client, pool_id, agents=["missing"])["total"] == 0
        assert listing(client, pool_id, models=["missing"])["total"] == 0
        row = data["items"][0]
        asset_id = row["id"]
        detail = client.get(f"/api/assets/{asset_id}").json()
        response = client.patch(
            f"/api/assets/{asset_id}",
            json={"revision": detail["revision"], "metadata": {"tags": ["review"], "note": "kept"}},
        )
        assert response.status_code == 200, response.text
        ref = pool.root / detail["spec"]["id"] / "result.json"
        assert read_json(ref)["metadata"] == {"tags": ["review"], "note": "kept"}
        assert listing(client, pool_id, tags=["review"])["total"] == 1
        assert (
            client.patch(
                f"/api/assets/{asset_id}",
                json={"revision": response.json()["revision"], "manifest": {}},
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/api/assets/{asset_id}/copy", json={"revision": 1, "name": "copy"}
            ).status_code
            == 422
        )
        assert (
            client.get(
                f"/api/trajectories/{asset_id}/file", params={"path": "../../outside"}
            ).status_code
            == 422
        )
        files = client.get(f"/api/trajectories/{asset_id}/files").json()
        assert any(f["path"] == "agent/trajectory.child.json" for f in files)
        response = client.post(
            "/api/trajectories/export",
            json={"kind": "trajectory", "pool_id": pool_id, "ids": [asset_id]},
        )
        assert response.status_code == 200, response.text
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert any(p.endswith("/agent/trajectory.json") for p in archive.namelist())
            saved = json.loads(archive.read(detail["spec"]["id"] + "/result.json"))
            assert saved["metadata"]["tags"] == ["review"]
        plan = client.post(
            "/api/delete/plan",
            json={
                "selection": {"kind": "trajectory", "pool_id": pool_id, "ids": [asset_id]},
                "physical": True,
            },
        ).json()
        result = wait(client, client.post("/api/delete/confirm", json={"token": plan["token"]}))
        assert not result["result"]["errors"]
        assert not Path(ref).exists()
        assert listing(client, pool_id)["total"] == 2


def test_task_relationship_and_delete_confirmation(tmp_path):
    task_dir = package(tmp_path / "tasks/example", "example")
    source = tmp_path / "source"
    rollout(source, task_ref=str(task_dir / "task.json"))
    pool = TrajPool(tmp_path / "pool")
    ref = pool.save(output_dir=source)
    with TestClient(create_app()) as client:
        task_pool = choose(client, task_dir.parent, "task")
        traj_pool = choose(client, pool.root)
        row = listing(client, traj_pool)["items"][0]
        detail = client.get(f"/api/assets/{row['id']}").json()
        assert detail["relations"][0]["role"] == "task"
        task_id = detail["relations"][0]["asset"]["id"]
        task_detail = client.get(f"/api/assets/{task_id}").json()
        assert any(link["role"] == "trajectory" for link in task_detail["relations"])
        plan = client.post(
            "/api/delete/plan",
            json={
                "selection": {"kind": "task", "pool_id": task_pool, "ids": [task_id]},
                "physical": True,
            },
        ).json()
        assert len(plan["affected_trajectories"]) == 1
        denied = wait(client, client.post("/api/delete/confirm", json={"token": plan["token"]}))
        assert denied["status"] == "failed"
        done = wait(
            client,
            client.post(
                "/api/delete/confirm",
                json={"token": plan["token"], "acknowledge_dependencies": True},
            ),
        )
        assert done["status"] == "completed"
        assert Path(ref).is_file()
        assert not task_dir.exists()
        assert client.get(f"/api/assets/{row['id']}").json()["relations"] == []


def test_empty_pool_live_refresh_and_independent_tags(tmp_path):
    pool = TrajPool(tmp_path / "pool")
    pool.root.mkdir()
    task_dir = package(tmp_path / "tasks/example")
    with TestClient(create_app()) as client:
        pool_id = choose(client, pool.root)
        task_id = choose(client, task_dir.parent, "task")
        assert listing(client, pool_id)["total"] == 0
        source = tmp_path / "source"
        rollout(source)
        pool.save(output_dir=source)
        assert listing(client, pool_id)["total"] == 1
        response = client.post(
            "/api/tags",
            params={"kind": "trajectory", "pool_id": pool_id},
            json={"tag": "new", "color": "#ff0000"},
        )
        assert response.status_code == 200
        data = client.post(
            "/api/assets/page", json={"selection": {"kind": "task", "pool_id": task_id}}
        ).json()
        assert not any(t["tag"] == "new" for t in data["tags"])


def test_usage_and_errors_are_indexed_once_per_file_change(tmp_path, monkeypatch):
    from envweaver.management import _pool_files

    source = tmp_path / "source"
    result = rollout(
        source,
        errors=[{"stage": "agent", "type": "timeout", "message": "time limit"}],
        reason="timeout",
    )
    root = source / "agent/trajectory.json"
    value = read_json(root)
    value["steps"][-1]["metrics"] = {"prompt_tokens": 1234, "completion_tokens": 0}
    root.write_text(json.dumps(value))
    # The continuation contains only a user message, hence no model usage.
    pool = TrajPool(tmp_path / "pool")
    archived = Path(pool.save(output_dir=source))
    calls = []
    original = _pool_files.token_usage

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(_pool_files, "token_usage", counted)
    with TestClient(create_app()) as client:
        pool_id = choose(client, pool.root)
        for _ in range(2):
            row = listing(client, pool_id)["items"][0]
            assert row["input_tokens"] == 1234
            assert row["output_tokens"] == 0
            assert row["error_count"] == 1
            assert row["score"] == 0
            assert row["agent_stop_reason"] == "timeout"
        assert len(calls) == 1
        details = client.get(f"/api/assets/{row['id']}?pool_id={pool_id}").json()
        assert details["spec"]["errors"] == result["errors"]
        changed = archived.parent / "agent/trajectory.json"
        value["steps"][-1]["metrics"]["prompt_tokens"] = 5678
        changed.write_text(json.dumps(value))
        assert listing(client, pool_id)["items"][0]["input_tokens"] == 5678
        assert len(calls) == 2
        # Derived statistics do not mutate the original or archived Eval result.
        assert "token_usage" not in read_json(archived)
