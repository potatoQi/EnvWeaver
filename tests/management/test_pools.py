"""SDK files remain the source of truth across selectable Pools."""

import base64
import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from envweaver import RuntimeRef, TaskPool, Verifier
from envweaver.management.api import create_app
from tests.management.helpers import seed_runtime
from tests.management.test_api import REF, FakeFiles, listing, package, wait


def add_pool(client, path, kind="task"):
    result = client.post(
        "/api/pools", json={"kind": kind, "name": Path(str(path)).name, "address": str(path)}
    )
    assert result.status_code == 200, result.text
    return result.json()["id"]


def test_runtime_names_come_from_sdk_references(tmp_path):
    root = tmp_path / "runtimes"
    for index in range(2):
        directory = root / "runtimes" / str(index)
        directory.mkdir(parents=True)
        ref = RuntimeRef(backend="docker", image=f"image:{index}", name="SDK 环境")
        (directory / "runtime.json").write_text(json.dumps(ref.to_dict()))
        (directory / "metadata.json").write_text(json.dumps({"name": "unused display name"}))
    with TestClient(create_app()) as client:
        pool_id = add_pool(client, root, kind="runtime")
        rows = listing(client, "runtime", pool_id=pool_id)["items"]
        assert len(rows) == 2
        assert all(row["name"] == "SDK 环境" for row in rows)
        assert len({row["id"] for row in rows}) == 2
        path = root / "runtimes/0/runtime.json"
        value = json.loads(path.read_text())
        value["name"] = "SDK 改名"
        path.write_text(json.dumps(value))
        rows = listing(client, "runtime", pool_id=pool_id)["items"]
        assert {row["name"] for row in rows} == {"SDK 环境", "SDK 改名"}


def test_sdk_save_appears_without_import_and_edits_same_files(tmp_path, runtime):
    root = tmp_path / "sdk-tasks"
    root.mkdir()
    original = tmp_path / "original"
    original.mkdir()
    entry = tmp_path / "verification/run.py"
    entry.parent.mkdir()
    entry.write_text("print('{\"score\": 1}')")
    app = create_app()
    with TestClient(app) as client:
        pool_id = add_pool(client, root)
        pool = TaskPool(root=root, runtime=runtime)
        ref = pool.save(
            task_runtime_ref=RuntimeRef("test", str(original)),
            name="sdk-save",
            goal=[{"type": "text", "text": "Solve"}],
            verifier=Verifier(entry=str(entry), runtime=runtime, timeout=30),
            verifier_runtime_ref=RuntimeRef("test", "verifier"),
            cwd="/",
        )
        rows = listing(client, pool_id=pool_id)["items"]
        assert len(rows) == 1 and rows[0]["name"] == "sdk-save"
        assert app.state.store.source_address(rows[0]["id"]) == Path(ref).parent
        assert not (tmp_path / "management").exists()
        result = client.patch(
            "/api/assets/" + rows[0]["id"],
            json={"revision": rows[0]["revision"], "metadata": {"edited": True}},
        )
        assert result.status_code == 200, result.text
        assert pool.load(task_ref=ref).metadata == {"edited": True}
        row = result.json()
        result = client.put(
            f"/api/tasks/{row['id']}/file",
            params={"path": "notes.txt"},
            json={
                "revision": row["revision"],
                "content_base64": base64.b64encode(b"notes").decode(),
            },
        )
        assert result.status_code == 200, result.text
        assert (root / "sdk-save/notes.txt").read_text() == "notes"
        # External material edits invalidate an open edit form as well.
        (root / "sdk-save/notes.txt").write_text("external")
        result = client.patch(
            "/api/assets/" + row["id"],
            json={"revision": result.json()["revision"], "name": "stale"},
        )
        assert result.status_code == 409


def test_pools_isolate_names_and_tags_survive_rescan_and_restart(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    package(first / "sample")
    package(second / "sample")
    with TestClient(create_app()) as client:
        a, b = add_pool(client, first), add_pool(client, second)
        ra, rb = listing(client, pool_id=a)["items"][0], listing(client, pool_id=b)["items"][0]
        assert ra["id"] != rb["id"]
        for pool_id, color in [(a, "#ff0000"), (b, "#0000ff")]:
            assert (
                client.post(
                    "/api/tags",
                    params={"kind": "task", "pool_id": pool_id},
                    json={"tag": "ready", "color": color},
                ).status_code
                == 200
            )
        assert (
            client.post(
                "/api/assets/tags",
                json={"selection": {"kind": "task", "pool_id": a}, "add": ["ready"]},
            ).status_code
            == 200
        )
        manifest = json.loads((first / "sample/task.json").read_text())
        manifest["goal"][0]["text"] = "updated from SDK files"
        (first / "sample/task.json").write_text(json.dumps(manifest))
        row = listing(client, pool_id=a)["items"][0]
        assert row["tags"] == ["ready"]
        assert row["spec"]["goal"][0]["text"] == "updated from SDK files"
        assert listing(client, pool_id=b)["items"][0]["tags"] == []
    with TestClient(create_app()) as client:
        assert client.get("/api/pools").json() == []
        assert add_pool(client, first) == a
        assert add_pool(client, second) == b
        assert listing(client, pool_id=a)["items"][0]["tags"] == ["ready"]
        assert (
            client.post(
                "/api/assets/page", json={"selection": {"kind": "task", "pool_id": b}}
            ).json()["tags"][0]["color"]
            == "#0000ff"
        )


def test_external_remove_hide_recreate_and_bad_pool(tmp_path):
    root = tmp_path / "tasks"
    package(root / "sample")
    with TestClient(create_app()) as client:
        pool_id = add_pool(client, root)
        row = listing(client, pool_id=pool_id)["items"][0]
        selection = {"kind": "task", "pool_id": pool_id, "ids": [row["id"]]}
        preview = client.post("/api/delete/plan", json={"selection": selection}).json()
        assert (
            wait(client, client.post("/api/delete/confirm", json={"token": preview["token"]}))[
                "status"
            ]
            == "completed"
        )
        assert listing(client, pool_id=pool_id)["total"] == 0  # rescan must not undo explicit hide
        assert (root / "sample/task.json").exists()
        shutil.rmtree(root / "sample")
        assert listing(client, pool_id=pool_id)["total"] == 0
        package(root / "sample")
        assert listing(client, pool_id=pool_id)["total"] == 1
        shutil.rmtree(root)
        response = client.get("/api/index")
        assert response.status_code == 200
        assert next(p for p in response.json()["pools"] if p["id"] == pool_id)["error"]
        root.mkdir()
        assert listing(client, pool_id=pool_id)["total"] == 0


def test_runtime_records_and_cross_pool_dependencies(tmp_path):
    runtimes = tmp_path / "runtime-root"
    directory = runtimes / "runtimes/snapshot"
    directory.mkdir(parents=True)
    (directory / "runtime.json").write_text(json.dumps(REF))
    (directory / "metadata.json").write_text(json.dumps({"host": None, "tag": "saved"}))
    tasks = tmp_path / "tasks"
    package(tasks / "sample")
    with TestClient(create_app(environment_files=FakeFiles())) as client:
        task_pool = add_pool(client, tasks)
        runtime_pool = add_pool(client, runtimes, kind="runtime")
        row = listing(client, "runtime", pool_id=runtime_pool)["items"][0]
        assert row["spec"] == REF
        detail = client.get("/api/assets/" + row["id"]).json()
        assert len(detail["relations"]) == 2
        preview = client.post(
            "/api/delete/plan",
            json={"selection": {"kind": "runtime", "pool_id": runtime_pool}, "physical": True},
        ).json()
        assert len(preview["affected_tasks"]) == 1
        assert listing(client, pool_id=task_pool)["total"] == 1
        assert (
            client.patch(
                "/api/assets/" + row["id"],
                json={"revision": row["revision"], "ref": {**REF, "image": "changed"}},
            ).status_code
            == 409
        )
        changed = client.patch(
            "/api/assets/" + row["id"], json={"revision": row["revision"], "name": "named-snapshot"}
        )
        assert changed.status_code == 200, changed.text
        assert json.loads((directory / "runtime.json").read_text())["name"] == "named-snapshot"
        assert "name" not in json.loads((directory / "metadata.json").read_text())
        assert changed.json()["name"] == changed.json()["spec"]["name"] == "named-snapshot"
        # Editing the reference JSON and SDK writes use the same name field.
        changed = client.patch(
            "/api/assets/" + row["id"],
            json={"revision": changed.json()["revision"], "ref": {**REF, "name": "SDK name"}},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["name"] == "SDK name"
        assert len(client.get("/api/assets/" + row["id"]).json()["relations"]) == 2
        assert (
            seed_runtime(
                client,
                params={"pool_id": runtime_pool},
                json={"name": "another", "ref": {**REF, "image": "another"}},
            ).status_code
            == 200
        )
        assert len(list((runtimes / "runtimes").glob("*/runtime.json"))) == 2


def test_stale_delete_confirmation_after_external_edit(tmp_path):
    root = tmp_path / "tasks"
    package(root / "sample")
    with TestClient(create_app()) as client:
        pool_id = add_pool(client, root)
        preview = client.post(
            "/api/delete/plan",
            json={"selection": {"kind": "task", "pool_id": pool_id}, "physical": True},
        ).json()
        (root / "sample/context/input.txt").write_text("external change")
        job = wait(client, client.post("/api/delete/confirm", json={"token": preview["token"]}))
        assert job["status"] == "failed"
        assert (root / "sample/task.json").exists()


def test_publish_recovery_and_symlink_guards(tmp_path):
    import subprocess

    from envweaver.management._pool_files import pool_operation

    root = tmp_path / "pool"
    package(root / "sample")
    dead = subprocess.Popen(["true"])
    dead.wait()
    temporary = root / ".publish-interrupted"
    temporary.mkdir()
    (root / "sample").rename(temporary / "old")
    (temporary / "intent.json").write_text(json.dumps({"pid": dead.pid, "directory": "sample"}))
    (root / ".sample.lock").touch()
    found = pool_operation({"root": str(root), "kind": "task", "action": "scan"})
    assert len(found) == 1 and found[0]["spec"]["name"] == "sample"
    assert not temporary.exists() and not (root / ".sample.lock").exists()
    (root / "sample/escape").symlink_to(tmp_path)
    with __import__("pytest").raises(ValueError):
        pool_operation(
            {
                "root": str(root),
                "kind": "task",
                "action": "resolve",
                "directory": "sample",
                "relative": "escape/pool/sample/task.json",
            }
        )


def test_bad_asset_does_not_hide_good_assets_or_allow_deletion(tmp_path):
    root = tmp_path / "pool"
    package(root / "good", name="good")
    package(root / "bad", name="bad")
    (root / "bad/task.json").write_text("{")
    with TestClient(create_app()) as client:
        pool_id = add_pool(client, root)
        assert listing(client, pool_id=pool_id)["total"] == 1
        error = next(p for p in client.get("/api/pools").json() if p["id"] == pool_id)["error"]
        assert error
        assert (
            client.post(
                "/api/delete/plan", json={"selection": {"kind": "task", "pool_id": pool_id}}
            ).status_code
            == 409
        )


def test_external_runtime_change_preserves_task_reference(tmp_path):
    runtime_root = tmp_path / "runtimes"
    record = runtime_root / "runtimes/snapshot"
    record.mkdir(parents=True)
    (record / "runtime.json").write_text(json.dumps(REF))
    tasks = tmp_path / "tasks"
    package(tasks / "sample")
    with TestClient(create_app()) as client:
        runtime_pool = add_pool(client, runtime_root, "runtime")
        task_pool = add_pool(client, tasks)
        task = listing(client, pool_id=task_pool)["items"][0]
        runtime = listing(client, "runtime", pool_id=runtime_pool)["items"][0]
        (record / "runtime.json").write_text(json.dumps({**REF, "image": "other"}))
        listing(client, "runtime", pool_id=runtime_pool)
        detail = client.get("/api/assets/" + task["id"]).json()
        assert all(link["asset"]["spec"]["image"] == REF["image"] for link in detail["relations"])
        assert client.get("/api/assets/" + runtime["id"]).json()["relations"] == []
