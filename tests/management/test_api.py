"""Management acceptance: HTTP contracts, lifecycle, relationships and persistence."""

import base64
import getpass
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from tests.management.helpers import configured_app, seed_runtime

REF = {"name": "test:one", "backend": "docker", "image": "test:one", "config": {}, "connection": {}, "metadata": {}}


class FakeFiles:
    def __init__(self):
        self.deleted = []
        self.fail = False

    def inspect(self, row):
        ref = row["spec"]
        image = "test:one" if ref["image"] == "alias" else ref["image"]
        return {
            "key": [ref["backend"], "server", image],
            "image": image,
            "dependencies": [
                [ref["backend"], "server", x] for x in ref["config"].get("parents", [])
            ],
        }

    def delete(self, row, identity):
        if self.fail:
            raise OSError("injected permission failure")
        self.deleted.append(identity["key"])


@pytest.fixture
def managed(tmp_path):
    files = FakeFiles()
    app = configured_app(
        tmp_path / "managed",
        environment_files=files,
    )
    with TestClient(app) as client:
        yield client, app, files


def package(path, name="sample", ref=None):
    path.mkdir(parents=True)
    (path / "verifier").mkdir()
    (path / "verifier/run.py").write_text("print('{\"score\":1}')")
    (path / "context").mkdir()
    (path / "context/input.txt").write_text("material")
    (path / "goal.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="
        )
    )
    data = {
        "version": 1,
        "name": name,
        "goal": [
            {"type": "text", "text": "Complete this task"},
            {"type": "image", "path": "goal.png"},
        ],
        "metadata": {"benchmark": "sample", "rank": 2},
        "task_runtime_ref": ref or REF,
        "verifier_runtime_ref": ref or REF,
        "cwd": "/work",
        "context": [{"source": "context/input.txt", "target": "/work/input.txt"}],
        "setup": {"command": "true", "timeout": 10},
        "verifier": {"entry": "verifier/run.py", "timeout": 10},
    }
    (path / "task.json").write_text(json.dumps(data))
    return path


def wait(client, response):
    assert response.status_code == 200, response.text
    job = response.json()
    for _ in range(200):
        job = client.get("/api/jobs/" + job["id"]).json()
        if job["status"] not in ("pending", "running"):
            return job
        time.sleep(0.01)
    pytest.fail(f"Job did not finish: {job}")


def install_task(client, source, tags=None):
    # Prepare saved environment files in the chosen Runtime Pool.
    spec = json.loads((source / "task.json").read_text())
    refs = [spec["task_runtime_ref"], spec["verifier_runtime_ref"]]
    for ref in refs:
        if not any(
            all(row["spec"].get(k) == ref.get(k, {}) for k in ("backend", "image", "config", "connection", "metadata")) for row in listing(client, "runtime")["items"]
        ):
            response = seed_runtime(
                client, json={"name": Path(ref["image"]).name.replace(":", "-"), "ref": ref}
            )
            assert response.status_code == 200, response.text
    pool = next(p for p in client.get("/api/pools").json() if p["kind"] == "task" and p["selected"])
    target = Path(pool["address"]) / spec["name"]
    shutil.copytree(source, target)
    if tags:
        spec.setdefault("metadata", {})["tags"] = sorted(
            set([*spec["metadata"].get("tags", []), *tags])
        )
        (target / "task.json").write_text(json.dumps(spec))
    return next(row["id"] for row in listing(client)["items"] if row["name"] == spec["name"])


def listing(client, kind="task", **filters):
    response = client.post("/api/assets/page", json={"selection": {"kind": kind, **filters}})
    assert response.status_code == 200, response.text
    result = response.json()
    result["items"] = [client.get(f"/api/assets/{row['id']}").json() for row in result["items"]]
    return result


def plan(client, ids, kind="runtime", physical=False):
    response = client.post(
        "/api/delete/plan", json={"selection": {"kind": kind, "ids": ids}, "physical": physical}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_open_access_and_schema(managed):
    client, app, files = managed
    assert listing(client)["total"] == 0
    schema = client.get("/openapi.json").json()
    assert "securitySchemes" not in schema.get("components", {})
    assert "/api/me" not in schema["paths"]
    for route in ("/api/assets/query", "/api/backends", "/api/creators"):
        assert route not in schema["paths"]
    assert "get" not in schema["paths"]["/api/tags"]
    assert client.get("/api/tags", params={"kind": "task"}).status_code == 404
    assert "QueryResult" not in schema["components"]["schemas"]
    assert "host" not in schema["components"]["schemas"]["Selection"]["properties"]
    assert (
        client.post(
            "/api/assets/page", json={"selection": {"kind": "task", "host": "local"}}
        ).status_code
        == 422
    )
    assert "/api/runtimes" not in schema["paths"]
    assert client.post("/api/runtimes", json={"name": "unused", "ref": REF}).status_code == 405
    assert client.post("/api/assets/page", json={"selection": {"kind": "other"}}).status_code == 422
    assert client.get("/").status_code == 200
    assert "/api/delete/confirm" in client.get("/openapi.json").json()["paths"]


def test_directory_detail_relations_and_materials(managed, tmp_path):
    client, app, _ = managed
    source = package(tmp_path / "source")
    asset_id = install_task(client, source, ["verified", "2026-10-28", "verified"])
    row = client.get("/api/assets/" + asset_id).json()
    assert row["tags"] == ["2026-10-28", "verified"]
    assert len(row["relations"]) == 2
    assert len(listing(client, "runtime")["items"]) == 1
    runtime_id = row["relations"][0]["asset"]["id"]
    assert {
        r["asset"]["id"] for r in client.get("/api/assets/" + runtime_id).json()["relations"]
    } == {asset_id}
    (source / "context/input.txt").write_text("changed at source")
    assert (
        client.get(f"/api/tasks/{asset_id}/file", params={"path": "context/input.txt"}).content
        == b"material"
    )
    assert (app.state.store.source_address(asset_id) / "task.json").is_file()


@pytest.mark.parametrize(
    "query,expected",
    [
        ({"tags": ["one", "two"]}, 1),
        ({"tags": ["three"]}, 0),
        ({"query": "甲"}, 1),
        ({"backends": ["qemu"]}, 0),
        ({"creators": [getpass.getuser()]}, 2),
        ({"metadata": {"rank": 2}}, 2),
        ({"metadata": {"rank": "2"}}, 0),
        ({"query": "' OR 1=1 --"}, 0),
    ],
)
def test_filters(managed, tmp_path, query, expected):
    client, _, _ = managed
    install_task(client, package(tmp_path / "a", "甲"), ["one", "two"])
    install_task(client, package(tmp_path / "b", "乙"), ["one"])
    assert listing(client, **query)["total"] == expected


def test_pagination_bulk_all_and_atomic_missing_selection(managed, tmp_path):
    client, _, _ = managed
    for i in range(3):
        install_task(client, package(tmp_path / str(i), str(i)), ["pool"])
    response = client.post(
        "/api/assets/page",
        json={
            "selection": {"kind": "task"},
            "page": 2,
            "page_size": 1,
            "sort": "name",
            "descending": False,
        },
    ).json()
    assert response["total"] == 3 and response["items"][0]["name"] == "1"
    assert client.post(
        "/api/assets/tags",
        json={"selection": {"kind": "task", "tags": ["pool"]}, "add": ["all"], "remove": ["pool"]},
    ).json() == {"updated": 3}
    row = listing(client)["items"][0]
    assert (
        client.post(
            "/api/assets/tags",
            json={"selection": {"kind": "task", "ids": [row["id"], "missing"]}, "add": ["bad"]},
        ).status_code
        == 409
    )
    assert listing(client, tags=["bad"])["total"] == 0
    assert (
        client.post(
            "/api/assets/tags",
            json={"selection": {"kind": "task"}, "add": ["same"], "remove": ["same"]},
        ).status_code
        == 422
    )


def test_edit_copy_and_stale_write(managed, tmp_path):
    client, app, _ = managed
    asset_id = install_task(client, package(tmp_path / "task"))
    row = client.get("/api/assets/" + asset_id).json()
    updated = client.patch(
        "/api/assets/" + asset_id,
        json={"revision": row["revision"], "name": "renamed", "tags": ["new"]},
    )
    assert updated.status_code == 200, updated.text
    assert (
        client.patch(
            "/api/assets/" + asset_id, json={"revision": row["revision"], "name": "overwrite"}
        ).status_code
        == 409
    )
    row = updated.json()
    copy = client.post(
        f"/api/assets/{asset_id}/copy", json={"revision": row["revision"], "name": "copy"}
    ).json()
    assert copy["id"] != asset_id
    assert (app.state.store.source_address(copy["id"]) / "task.json").is_file()
    assert copy["spec"]["name"] == "copy"
    body = {
        "revision": copy["revision"],
        "content_base64": base64.b64encode(b"edited copy").decode(),
    }
    assert (
        client.put(f"/api/tasks/{copy['id']}/file?path=context/input.txt", json=body).status_code
        == 200
    )
    assert client.get(f"/api/tasks/{asset_id}/file?path=context/input.txt").content == b"material"


def test_task_edit_updates_both_links_and_runtime_protection(managed, tmp_path):
    client, _, _ = managed
    asset_id = install_task(client, package(tmp_path / "task"))
    row = client.get("/api/assets/" + asset_id).json()
    runtime = row["relations"][0]["asset"]
    assert (
        client.patch(
            "/api/assets/" + runtime["id"],
            json={"revision": runtime["revision"], "ref": {**REF, "image": "changed"}},
        ).status_code
        == 409
    )
    manifest = row["spec"]
    manifest["task_runtime_ref"] = {**REF, "image": "changed"}
    assert (
        client.patch(
            "/api/assets/" + asset_id, json={"revision": row["revision"], "manifest": manifest}
        ).status_code
        == 200
    )
    detail = client.get("/api/assets/" + asset_id).json()
    assert len({r["asset"]["id"] for r in detail["relations"]}) == 2


@pytest.mark.parametrize("path", ["../escape", "/etc/passwd", "a/../../escape", "a\\escape"])
def test_file_path_escape(managed, tmp_path, path):
    client, _, _ = managed
    asset_id = install_task(client, package(tmp_path / "task"))
    assert client.get(f"/api/tasks/{asset_id}/file", params={"path": path}).status_code == 422
    assert (
        client.put(
            f"/api/tasks/{asset_id}/file",
            params={"path": path},
            json={"revision": 1, "content_base64": "eA=="},
        ).status_code
        == 422
    )


def test_file_guards_and_rollback(managed, tmp_path):
    client, app, _ = managed
    asset_id = install_task(client, package(tmp_path / "task"))
    assert (
        client.put(
            f"/api/tasks/{asset_id}/file?path=task.json",
            json={"revision": 1, "content_base64": "e30="},
        ).status_code
        == 422
    )
    assert (
        client.delete(f"/api/tasks/{asset_id}/file?path=context/input.txt&revision=1").status_code
        == 404
    )
    assert (
        app.state.store.source_address(asset_id) / "context/input.txt"
    ).read_text() == "material"
    assert client.get("/api/assets/" + asset_id).json()["revision"] == 1
    assert (
        client.put(
            f"/api/tasks/{asset_id}/file?path=notes.txt",
            json={"revision": 1, "content_base64": "bm90ZXM="},
        ).status_code
        == 200
    )
    assert client.delete(f"/api/tasks/{asset_id}/file?path=notes.txt&revision=2").status_code == 200


def test_delete_requires_ack_rechecks_and_is_one_use(managed, tmp_path):
    client, _, files = managed
    asset_id = install_task(client, package(tmp_path / "task"))
    runtime = listing(client, "runtime")["items"][0]
    preview = plan(client, [runtime["id"]], physical=True)
    assert preview["affected_tasks"] == [{"id": asset_id, "name": "sample"}]
    job = wait(client, client.post("/api/delete/confirm", json={"token": preview["token"]}))
    assert job["status"] == "failed" and files.deleted == []
    job = wait(
        client,
        client.post(
            "/api/delete/confirm",
            json={"token": preview["token"], "acknowledge_dependencies": True},
        ),
    )
    assert job["status"] == "completed" and len(files.deleted) == 1
    assert client.get("/api/assets/" + runtime["id"]).status_code == 404
    assert (
        client.get("/api/assets/" + asset_id).json()["relations"][0]["asset"]["state"] == "missing"
    )
    job = wait(
        client,
        client.post(
            "/api/delete/confirm",
            json={"token": preview["token"], "acknowledge_dependencies": True},
        ),
    )
    assert job["status"] == "failed" and len(files.deleted) == 1


def test_new_reference_invalidates_confirm(managed, tmp_path):
    client, _, files = managed
    install_task(client, package(tmp_path / "a", "a"))
    runtime = listing(client, "runtime")["items"][0]
    preview = plan(client, [runtime["id"]], physical=True)
    install_task(client, package(tmp_path / "b", "b"))
    job = wait(
        client,
        client.post(
            "/api/delete/confirm",
            json={"token": preview["token"], "acknowledge_dependencies": True},
        ),
    )
    assert job["status"] == "failed" and files.deleted == []
    assert "变化" in job["error"]


def test_aliases_and_qemu_dependencies_are_in_impact(managed, tmp_path):
    client, _, _ = managed
    first = seed_runtime(client, json={"name": "first", "ref": REF}).json()
    second = seed_runtime(client, json={"name": "alias", "ref": {**REF, "image": "alias"}}).json()
    preview = plan(client, [first["id"]], physical=True)
    assert second["id"] in {r["id"] for r in preview["affected_runtimes"]}
    base = {"backend": "qemu", "image": "/base.qcow2", "config": {}, "connection": {}}
    base_row = seed_runtime(client, json={"name": "base", "ref": base}).json()
    child = seed_runtime(
        client,
        json={
            "name": "child",
            "ref": {**base, "image": "/child.qcow2", "config": {"parents": ["/base.qcow2"]}},
        },
    ).json()
    preview = plan(client, [base_row["id"]], physical=True)
    assert child["id"] in {r["id"] for r in preview["affected_runtimes"]}


def test_delete_failure_retains_record_and_error(managed):
    client, _, files = managed
    row = seed_runtime(client, json={"name": "first", "ref": REF}).json()
    preview = plan(client, [row["id"]], physical=True)
    files.fail = True
    job = wait(client, client.post("/api/delete/confirm", json={"token": preview["token"]}))
    assert job["status"] == "partial"
    assert "permission" in client.get("/api/assets/" + row["id"]).json()["last_error"]


def test_task_delete_never_deletes_environment(managed, tmp_path):
    client, app, files = managed
    asset_id = install_task(client, package(tmp_path / "task"))
    preview = plan(client, [asset_id], kind="task", physical=True)
    job = wait(client, client.post("/api/delete/confirm", json={"token": preview["token"]}))
    assert job["status"] == "completed" and files.deleted == []
    assert not (app.state.store.source_address(asset_id)).exists()
    assert listing(client, "runtime")["total"] == 1


def test_concurrent_same_revision_only_one_writer(managed, tmp_path):
    client, _, _ = managed
    asset_id = install_task(client, package(tmp_path / "task"))

    def edit(i):
        return client.patch(
            "/api/assets/" + asset_id, json={"revision": 1, "name": f"edit-{i}"}
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(edit, range(2))) == [200, 409]
    row = client.get("/api/assets/" + asset_id).json()
    assert row["revision"] == 2


def test_annotations_survive_reselection_and_jobs_are_session_only(tmp_path):
    root = tmp_path / "managed"
    app = configured_app(root)
    with TestClient(app) as client:
        asset_id = install_task(client, package(tmp_path / "task"), ["persisted"])
        temporary = install_task(client, package(tmp_path / "temporary", "temporary"))
        preview = plan(client, [temporary], kind="task", physical=True)
        job = wait(client, client.post("/api/delete/confirm", json={"token": preview["token"]}))
        assert job["status"] == "completed"
        assert client.get("/api/jobs").json()
    with TestClient(configured_app(root)) as client:
        assert client.get("/api/assets/" + asset_id).json()["tags"] == ["persisted"]
        assert client.get("/api/jobs").json() == []


def test_missing_dependencies_remain_missing_when_editing_or_copying(managed, tmp_path):
    client, _, _ = managed
    task_id = install_task(client, package(tmp_path / "task"))
    runtime = listing(client, "runtime")["items"][0]
    preview = plan(client, [runtime["id"]], physical=True)
    assert (
        wait(
            client,
            client.post(
                "/api/delete/confirm",
                json={"token": preview["token"], "acknowledge_dependencies": True},
            ),
        )["status"]
        == "completed"
    )
    row = client.get("/api/assets/" + task_id).json()
    assert row["state"] == "missing"
    edited = client.patch(
        "/api/assets/" + task_id, json={"revision": row["revision"], "tags": ["changed"]}
    ).json()
    assert edited["state"] == "missing"
    copied = client.post(
        f"/api/assets/{task_id}/copy", json={"revision": edited["revision"], "name": "copy"}
    ).json()
    assert copied["state"] == "missing"
    assert listing(client, "runtime")["total"] == 0


@pytest.mark.parametrize("reason", ["expired", "identity"])
def test_confirmation_rejects_changed_identity_and_expiry(managed, reason):
    client, app, files = managed
    row = seed_runtime(client, json={"name": "runtime", "ref": REF}).json()
    preview = plan(client, [row["id"]], physical=True)
    if reason == "expired":
        with app.state.store.transaction() as db:
            for confirmation in db.plans.values():
                confirmation["created_at"] = "2020-01-01T00:00:00+00:00"
    else:
        original = files.inspect
        files.inspect = lambda row: {**original(row), "key": ["docker", "server", "changed"]}
    job = wait(
        client,
        client.post("/api/delete/confirm", json={"token": preview["token"]}),
    )
    assert job["status"] == "failed" and files.deleted == []


def test_bulk_aliases_delete_physical_image_once(managed):
    client, _, files = managed
    rows = [
        seed_runtime(client, json={"name": name, "ref": {**REF, "image": name}}).json()
        for name in ["test:one".replace(":", "-"), "alias"]
    ]
    # Both records refer to the same physical identity.
    files.inspect = lambda row: {
        "key": ["docker", "server", "same"],
        "image": "same",
        "dependencies": [],
    }
    preview = plan(client, [r["id"] for r in rows], physical=True)
    job = wait(client, client.post("/api/delete/confirm", json={"token": preview["token"]}))
    assert job["status"] == "completed" and len(files.deleted) == 1


def test_failed_publication_rolls_back_files_and_catalog(managed, tmp_path, monkeypatch):

    client, app, _ = managed
    task_id = install_task(client, package(tmp_path / "task"))
    rename = Path.rename

    def fail(path, target):
        if path.name == "new" and path.parent.name.startswith(".publish-"):
            raise OSError("injected publication failure")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail)
    response = client.patch("/api/assets/" + task_id, json={"revision": 1, "name": "changed"})
    assert response.status_code == 502
    assert client.get("/api/assets/" + task_id).json()["name"] == "sample"
    assert (
        json.loads((app.state.store.source_address(task_id) / "task.json").read_text())["name"]
        == "sample"
    )
    assert not list((tmp_path / "managed/task-pool").glob(".publish-*"))


def test_missing_runtime_copy_and_explicit_new_reference(managed):
    client, app, _ = managed
    row = seed_runtime(client, json={"name": "runtime", "ref": REF}).json()
    with app.state.store.transaction() as db:
        db.assets[row["id"]]["state"] = "missing"
    copied = client.post(
        "/api/assets/" + row["id"] + "/copy", json={"name": "new", "revision": 1}
    ).json()
    assert copied["state"] == "missing"
    assert (
        client.post(
            "/api/delete/plan",
            json={"selection": {"kind": "runtime", "ids": [copied["id"]]}, "physical": True},
        ).status_code
        == 422
    )
    edited = client.patch(
        "/api/assets/" + copied["id"], json={"revision": 1, "ref": {**REF, "image": "replacement"}}
    ).json()
    assert edited["state"] == "registered"


@pytest.mark.parametrize(
    "field,value",
    [
        ("created_after", "2000-01-01T00:00:00Z"),
        ("created_before", "2099-01-01T00:00:00Z"),
    ],
)
def test_extra_filters(managed, tmp_path, field, value):
    client, _, _ = managed
    install_task(client, package(tmp_path / "task"))
    assert listing(client, **{field: value})["total"] == 1


def test_record_only_delete_preserves_files(managed, tmp_path):
    client, app, files = managed
    source = package(tmp_path / "a")
    task_id = install_task(client, source)
    preview = plan(client, [task_id], kind="task", physical=False)
    assert (
        wait(client, client.post("/api/delete/confirm", json={"token": preview["token"]}))["status"]
        == "completed"
    )
    assert (app.state.store.source_address(task_id) / "task.json").exists() and not files.deleted


def test_tag_catalog_lifecycle(managed, tmp_path):
    client, app, _ = managed
    assert client.post("/api/tags", json={"tag": "missing-pool"}).status_code == 422
    for kind, color in [("task", "#dc2626"), ("runtime", "#2563eb")]:
        assert (
            client.post(
                "/api/tags", params={"kind": kind}, json={"tag": " unused ", "color": color}
            ).status_code
            == 200
        )
        assert client.post("/api/assets/page", json={"selection": {"kind": kind}}).json()[
            "tags"
        ] == [{"tag": "unused", "color": color, "count": 0}]
        assert (
            client.post("/api/tags", params={"kind": kind}, json={"tag": "unused"}).status_code
            == 409
        )
    assert client.post("/api/tags", params={"kind": "task"}, json={"tag": "   "}).status_code == 422
    assert (
        client.post(
            "/api/tags", params={"kind": "task"}, json={"tag": "bad", "color": "red;display:none"}
        ).status_code
        == 422
    )
    install_task(client, package(tmp_path / "tagged"), ["unused"])
    task = listing(client)["items"][0]
    runtime = seed_runtime(
        client, json={"name": "tagged-runtime", "ref": REF, "tags": ["unused"]}
    ).json()
    name = '路径/quoted "标签"'
    changed = client.patch(
        "/api/tags",
        params={"kind": "task", "tag": "unused"},
        json={"tag": name, "color": "#DC2626"},
    )
    assert changed.status_code == 200
    assert changed.json()["color"] == "#dc2626"
    current = client.get("/api/assets/" + task["id"]).json()
    assert current["tags"] == [name]
    assert current["revision"] == task["revision"] + 1
    assert current["spec"] == {
        **task["spec"],
        "metadata": {**task["metadata"], "tags": [name]},
    }
    assert client.get("/api/assets/" + runtime["id"]).json()["revision"] == runtime["revision"]
    assert client.get("/api/assets/" + runtime["id"]).json()["tags"] == ["unused"]
    client.post("/api/tags", params={"kind": "task"}, json={"tag": "conflict"})
    assert (
        client.patch(
            "/api/tags", params={"kind": "task", "tag": name}, json={"tag": "conflict"}
        ).status_code
        == 409
    )
    assert client.delete("/api/tags", params={"kind": "runtime", "tag": name}).status_code == 404
    assert (
        client.post(
            "/api/assets/tags",
            json={"selection": {"kind": "task", "ids": [task["id"]]}, "remove": [name]},
        ).status_code
        == 200
    )
    tags = client.post("/api/assets/page", json={"selection": {"kind": "task"}}).json()["tags"]
    assert next(t for t in tags if t["tag"] == name)["count"] == 0
    with TestClient(
        configured_app(tmp_path / "managed", environment_files=FakeFiles())
    ) as reopened:
        assert (
            reopened.post("/api/assets/page", json={"selection": {"kind": "task"}}).json()["tags"]
            == tags
        )
        assert reopened.post("/api/assets/page", json={"selection": {"kind": "runtime"}}).json()[
            "tags"
        ] == [{"tag": "unused", "color": "#2563eb", "count": 1}]
    assert client.delete("/api/tags", params={"kind": "task", "tag": name}).status_code == 200
    assert client.get("/api/assets/" + runtime["id"]).json()["tags"] == ["unused"]
    assert client.get("/api/assets/" + task["id"]).status_code == 200
    # Deleting a tag with the same name must only remove associations in that Pool.
    client.post(
        "/api/assets/tags",
        json={"selection": {"kind": "task", "ids": [task["id"]]}, "add": ["unused"]},
    )
    assert (
        client.delete("/api/tags", params={"kind": "runtime", "tag": "unused"}).status_code == 200
    )
    assert client.get("/api/assets/" + task["id"]).json()["tags"] == ["unused"]
    assert client.get("/api/assets/" + runtime["id"]).json()["tags"] == []


def test_selection_exclusions_apply_to_all_operations(managed, tmp_path):
    client, app, _ = managed
    install_task(client, package(tmp_path / "one", "one"), ["pool"])
    install_task(client, package(tmp_path / "two", "two"), ["pool"])
    rows = listing(client)["items"]
    excluded, included = rows[0]["id"], rows[1]["id"]
    selection = {"kind": "task", "tags": ["pool"], "exclude_ids": [excluded]}
    response = client.post("/api/assets/tags", json={"selection": selection, "add": ["selected"]})
    assert response.json() == {"updated": 1}
    assert "selected" not in app.state.store.get(excluded)["tags"]
    assert "selected" in app.state.store.get(included)["tags"]
    preview = client.post("/api/delete/plan", json={"selection": selection}).json()
    assert [r["id"] for r in preview["selected"]] == [included]
    result = client.post(
        "/api/assets/page", json={"selection": {**selection, "ids": [excluded, included]}}
    ).json()
    assert [r["id"] for r in result["items"]] == [included]


def test_backend_multiselect_and_discovery(managed, tmp_path):
    client, _, _ = managed
    for backend in ["docker", "qemu", "kube"]:
        install_task(
            client,
            package(
                tmp_path / backend,
                backend,
                ref={**REF, "backend": backend, "image": backend + "-image"},
            ),
        )
    for kind in ["task", "runtime"]:
        assert client.post("/api/assets/page", json={"selection": {"kind": kind}}).json()[
            "backends"
        ] == [
            "docker",
            "kube",
            "qemu",
        ]
        selection = {"kind": kind, "backends": ["docker", "kube"]}
        rows = client.post("/api/assets/page", json={"selection": selection}).json()["items"]
        assert len(rows) == 2
        assert {r["backend"] for r in rows} == {"docker", "kube"}
        assert client.post(
            "/api/assets/tags", json={"selection": selection, "add": ["chosen"]}
        ).json() == {"updated": 2}
        preview = client.post("/api/delete/plan", json={"selection": selection}).json()
        assert len(preview["selected"]) == 2
        assert (
            client.post(
                "/api/assets/page", json={"selection": {"kind": kind, "backends": []}}
            ).json()["total"]
            == 3
        )
        assert (
            client.post(
                "/api/assets/page", json={"selection": {"kind": kind, "backends": ["unknown"]}}
            ).json()["total"]
            == 0
        )
        assert (
            client.post(
                "/api/assets/page",
                json={"selection": {"kind": kind, "backends": ["docker", "kube"], "query": "qemu"}},
            ).json()["total"]
            == 0
        )


def test_import_export_routes_removed(managed):
    client, _, _ = managed
    paths = client.get("/openapi.json").json()["paths"]
    for route in ("/api/import/tasks", "/api/import/upload", "/api/import/bundle", "/api/export"):
        assert route not in paths
        assert client.post(route, json={}).status_code in (404, 405)


@pytest.mark.parametrize("kind", ["task", "runtime"])
def test_created_and_updated_ranges_are_independent(managed, tmp_path, kind):
    client, app, _ = managed
    if kind == "task":
        ids = [install_task(client, package(tmp_path / name, name)) for name in ("first", "second")]
    else:
        ids = [
            seed_runtime(client, json={"name": name, "ref": REF}).json()["id"]
            for name in ("first", "second")
        ]
    with app.state.store.transaction():
        app.state.store.assets[ids[0]].update(
            created_at="2026-01-01T00:00:00+00:00", updated_at="2026-03-01T00:00:00+00:00"
        )
        app.state.store.assets[ids[1]].update(
            created_at="2026-02-01T00:00:00+00:00", updated_at="2026-02-02T00:00:00+00:00"
        )
    cases = [
        ({"created_before": "2026-01-01T00:00:00Z"}, [ids[0]]),
        ({"created_after": "2026-02-01T00:00:00Z"}, [ids[1]]),
        ({"updated_before": "2026-02-02T00:00:00Z"}, [ids[1]]),
        ({"updated_after": "2026-03-01T00:00:00Z"}, [ids[0]]),
        (
            {
                "updated_after": "2026-03-01T08:00:00+08:00",
                "updated_before": "2026-03-01T00:00:00Z",
            },
            [ids[0]],
        ),
        ({"created_before": "2026-01-31T23:59:59Z", "updated_before": "2026-02-28T23:59:59Z"}, []),
    ]
    for filters, expected in cases:
        assert [r["id"] for r in listing(client, kind, **filters)["items"]] == expected
    for filters in (
        {"updated_after": "2026-03-01"},
        {"updated_after": "invalid"},
        {"created_after": "2026-03-01T00:00:00Z", "created_before": "2026-01-01T00:00:00Z"},
        {"updated_after": "2026-03-01T00:00:00Z", "updated_before": "2026-01-01T00:00:00Z"},
    ):
        assert (
            client.post(
                "/api/assets/page", json={"selection": {"kind": kind, **filters}}
            ).status_code
            == 422
        )
    result = client.post(
        "/api/assets/tags",
        json={
            "selection": {"kind": kind, "updated_before": "2026-02-02T00:00:00Z"},
            "add": ["filtered"],
        },
    )
    assert result.status_code == 200, result.text
    assert client.get(f"/api/assets/{ids[0]}").json()["tags"] == []
    assert client.get(f"/api/assets/{ids[1]}").json()["tags"] == ["filtered"]


@pytest.mark.parametrize("kind", ["task", "runtime"])
def test_creator_multiselect_and_bulk_operations(managed, tmp_path, kind):
    client, app, _ = managed
    for name in ("alice", "bob", "carol"):
        install_task(client, package(tmp_path / name, name, ref={**REF, "image": name}))
    with app.state.store.transaction():
        for row in app.state.store.assets.values():
            row["created_by"] = row["spec"].get("name", row["spec"].get("image"))
    assert client.post("/api/assets/page", json={"selection": {"kind": kind}}).json()[
        "creators"
    ] == ["alice", "bob", "carol"]
    for names, count in [([], 3), (["alice"], 1), (["alice", "bob"], 2), (["missing"], 0)]:
        response = client.post(
            "/api/assets/page", json={"selection": {"kind": kind, "creators": names}}
        )
        assert response.status_code == 200
        assert response.json()["total"] == count
    selection = {"kind": kind, "creators": ["alice", "bob"]}
    assert client.post(
        "/api/assets/tags", json={"selection": selection, "add": ["chosen"]}
    ).json() == {"updated": 2}
    plan = client.post("/api/delete/plan", json={"selection": selection}).json()
    assert {app.state.store.get(r["id"])["created_by"] for r in plan["selected"]} == {
        "alice",
        "bob",
    }
    assert (
        client.post("/api/assets/page", json={"selection": {**selection, "query": "carol"}}).json()[
            "total"
        ]
        == 0
    )


@pytest.mark.parametrize("kind", ["task", "runtime"])
def test_page_includes_creation_and_modification_fields(managed, tmp_path, kind):
    client, app, _ = managed
    if kind == "task":
        root = next(p["address"] for p in client.get("/api/pools").json() if p["kind"] == kind)
        package(Path(root) / "timestamps")
    else:
        seed_runtime(client, json={"name": "timestamps", "ref": REF})
    response = client.post("/api/assets/page", json={"selection": {"kind": kind}})
    assert response.status_code == 200, response.text
    row = response.json()["items"][0]
    detail = client.get(f"/api/assets/{row['id']}").json()
    for key in ("created_by", "created_at", "updated_by", "updated_at"):
        assert row[key] == detail[key]
    assert "spec" not in row
