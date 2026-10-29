"""Pool metadata is portable; service state and browser choices are temporary."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from envweaver.management.api import create_app
from envweaver.management.models import TagDefinition
from envweaver.management.pools import PoolFiles
from tests.management.test_api import listing, package, wait
from tests.management.test_pools import add_pool


def test_startup_has_no_directories_or_default_pools(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    with TestClient(create_app()) as client:
        assert client.get("/api/pools").json() == []
        assert client.get("/api/jobs").json() == []
        assert client.get("/").status_code == 200
        assert (
            client.post("/api/assets/page", json={"selection": {"kind": "task"}}).status_code == 422
        )
    assert list(tmp_path.iterdir()) == []


def test_reading_does_not_write_metadata_or_register_a_runtime_pool(tmp_path):
    package(tmp_path / "task")
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    with TestClient(create_app()) as client:
        pool_id = add_pool(client, tmp_path)
        task = listing(client, pool_id=pool_id)["items"][0]
        assert len(client.get("/api/assets/" + task["id"]).json()["relations"]) == 2
        assert [p["kind"] for p in client.get("/api/pools").json()] == ["task"]
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


def test_tags_move_with_pool_in_task_metadata(tmp_path):
    root = tmp_path / "pool"
    package(root / "sample")
    original = (root / "sample/task.json").read_bytes()
    with TestClient(create_app()) as client:
        pool_id = add_pool(client, root)
        assert (
            client.post(
                "/api/tags",
                params={"kind": "task", "pool_id": pool_id},
                json={"tag": "ready", "color": "#dc2626"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/assets/tags",
                json={"selection": {"kind": "task", "pool_id": pool_id}, "add": ["ready"]},
            ).status_code
            == 200
        )
    root.rename(tmp_path / "moved")
    root = tmp_path / "moved"
    with TestClient(create_app()) as client:
        assert client.get("/api/pools").json() == []
        pool_id = add_pool(client, root)
        assert listing(client, pool_id=pool_id)["items"][0]["tags"] == ["ready"]
        assert client.post(
            "/api/assets/page", json={"selection": {"kind": "task", "pool_id": pool_id}}
        ).json()["tags"] == [{"tag": "ready", "color": "#dc2626", "count": 1}]
    expected = json.loads(original)
    expected["metadata"]["tags"] = ["ready"]
    assert json.loads((root / "sample/task.json").read_text()) == expected
    metadata = json.loads((root / ".envweaver/task-pool.json").read_text())
    assert "present" not in metadata["assets"]["sample"]
    assert "tags" not in metadata["assets"]["sample"]
    assert not list(tmp_path.rglob("*.sqlite*"))


def test_metadata_atomic_writers_and_stale_service(tmp_path):
    root = tmp_path / "pool"
    root.mkdir()
    first, second = create_app(), create_app()
    with TestClient(first) as a, TestClient(second) as b:
        pool_id = add_pool(a, root)
        assert add_pool(b, root) == pool_id
        first.state.store.create_tag("task", TagDefinition(tag="first"), pool_id)
        with pytest.raises(FileExistsError):
            second.state.store.create_tag("task", TagDefinition(tag="stale"), pool_id)
        # HTTP refreshes the sidecar before applying this second service's edit.
        response = b.post(
            "/api/tags", params={"kind": "task", "pool_id": pool_id}, json={"tag": "second"}
        )
        assert response.status_code == 200, response.text
        assert {
            t["tag"]
            for t in a.post(
                "/api/assets/page", json={"selection": {"kind": "task", "pool_id": pool_id}}
            ).json()["tags"]
        } == {"first", "second"}
        files = PoolFiles(first.state.store.pools[pool_id])
        saved = files.call("metadata")

        def write(index):
            try:
                files.call(
                    "metadata",
                    data={"tags": {str(index): "#2563eb"}, "assets": {}},
                    expected=saved["stamp"],
                )
                return "saved"
            except FileExistsError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(write, range(2))) == ["conflict", "saved"]
        json.loads((root / ".envweaver/task-pool.json").read_text())
        assert not list((root / ".envweaver").glob(".metadata-*"))


def test_task_runtime_tags_share_a_directory_but_not_a_namespace(tmp_path):
    package(tmp_path / "task")
    record = tmp_path / "runtimes/snapshot"
    record.mkdir(parents=True)
    task = json.loads((tmp_path / "task/task.json").read_text())
    (record / "runtime.json").write_text(json.dumps(task["task_runtime_ref"]))
    with TestClient(create_app()) as client:
        for kind in ("task", "runtime"):
            pool_id = add_pool(client, tmp_path, kind)
            assert (
                client.post(
                    "/api/tags", params={"kind": kind, "pool_id": pool_id}, json={"tag": kind}
                ).status_code
                == 200
            )
    with TestClient(create_app()) as client:
        for kind in ("task", "runtime"):
            pool_id = add_pool(client, tmp_path, kind)
            tags = client.post(
                "/api/assets/page", json={"selection": {"kind": kind, "pool_id": pool_id}}
            ).json()["tags"]
            assert [t["tag"] for t in tags] == [kind]


def test_hidden_record_stays_hidden_after_reselection(tmp_path):
    package(tmp_path / "task")
    with TestClient(create_app()) as client:
        pool_id = add_pool(client, tmp_path)
        plan = client.post(
            "/api/delete/plan", json={"selection": {"kind": "task", "pool_id": pool_id}}
        ).json()
        assert (
            wait(client, client.post("/api/delete/confirm", json={"token": plan["token"]}))[
                "status"
            ]
            == "completed"
        )
    with TestClient(create_app()) as client:
        pool_id = add_pool(client, tmp_path)
        assert listing(client, pool_id=pool_id)["total"] == 0
        (tmp_path / "task/context/input.txt").write_text("changed")
        assert listing(client, pool_id=pool_id)["total"] == 1


def test_copying_pool_keeps_labels_and_has_independent_asset_ids(tmp_path):
    import shutil

    source, copied = tmp_path / "source", tmp_path / "copy"
    package(source / "sample")
    with TestClient(create_app()) as client:
        first = add_pool(client, source)
        client.post(
            "/api/assets/tags",
            json={"selection": {"kind": "task", "pool_id": first}, "add": ["saved"]},
        )
        shutil.copytree(source, copied)
        second = add_pool(client, copied)
        a = listing(client, pool_id=first)["items"][0]
        b = listing(client, pool_id=second)["items"][0]
        assert a["id"] != b["id"]
        assert a["tags"] == b["tags"] == ["saved"]


@pytest.mark.parametrize("runtime_first", [False, True])
def test_missing_runtime_dependency_survives_restart(tmp_path, runtime_first):
    from tests.management.test_api import REF, FakeFiles

    tasks, runtimes = tmp_path / "tasks", tmp_path / "runtime"
    package(tasks / "sample")
    directory = runtimes / "runtimes/record"
    directory.mkdir(parents=True)
    (directory / "runtime.json").write_text(json.dumps(REF))
    with TestClient(create_app(environment_files=FakeFiles())) as client:
        add_pool(client, tasks)
        pool_id = add_pool(client, runtimes, "runtime")
        preview = client.post(
            "/api/delete/plan",
            json={"selection": {"kind": "runtime", "pool_id": pool_id}, "physical": True},
        ).json()
        job = wait(
            client,
            client.post(
                "/api/delete/confirm",
                json={"token": preview["token"], "acknowledge_dependencies": True},
            ),
        )
        assert job["status"] == "completed", job
    with TestClient(create_app()) as client:
        for root, kind in (
            [(runtimes, "runtime"), (tasks, "task")]
            if runtime_first
            else [(tasks, "task"), (runtimes, "runtime")]
        ):
            pool_id = add_pool(client, root, kind)
            if kind == "task":
                task_pool_id = pool_id
        assert listing(client, pool_id=task_pool_id)["items"][0]["state"] == "missing"


def test_current_pool_selection_is_shared_until_service_restart(tmp_path):
    first, second, runtimes = tmp_path / "first", tmp_path / "second", tmp_path / "runtimes"
    for path in (first, second, runtimes):
        path.mkdir()
    app = create_app()
    with TestClient(app) as client:
        first_id = add_pool(client, first)
        runtime_id = add_pool(client, runtimes, "runtime")
        second_id = add_pool(client, second)
        selected = {p["kind"]: p["id"] for p in client.get("/api/pools").json() if p["selected"]}
        assert selected == {"task": second_id, "runtime": runtime_id}
        assert add_pool(client, first) == first_id
        selected = {p["kind"]: p["id"] for p in client.get("/api/pools").json() if p["selected"]}
        assert selected == {"task": first_id, "runtime": runtime_id}
        response = client.post(
            "/api/pools", json={"kind": "task", "address": str(tmp_path / "missing")}
        )
        assert response.status_code == 404
        assert {
            p["kind"]: p["id"] for p in client.get("/api/pools").json() if p["selected"]
        } == selected
    assert not list(tmp_path.rglob(".envweaver"))
    with TestClient(create_app()) as restarted:
        assert restarted.get("/api/pools").json() == []
