"""RuntimeRef.metadata is shared by saved references and management tags."""

import json

import pytest
from fastapi.testclient import TestClient

from envweaver import RuntimeRef
from envweaver._io.storage import write_records
from envweaver.management.api import create_app
from envweaver.management.models import Selection
from tests.management.test_api import REF, listing, package
from tests.management.test_pools import add_pool


def test_runtime_tags_roundtrip_and_relationships(tmp_path):
    root = tmp_path / "environments"
    ref = RuntimeRef.from_dict({**REF, "metadata": {"tags": ["sdk"], "custom": {"v": 1}}})
    write_records(root, "runtimes/base", {"runtime.json": ref.to_dict()})
    record = root / "runtimes/base/runtime.json"
    tasks = tmp_path / "tasks"
    package(tasks / "sample", ref=REF)
    with TestClient(create_app()) as client:
        pool = add_pool(client, root, kind="runtime")
        add_pool(client, tasks)
        row = listing(client, "runtime")["items"][0]
        task = listing(client)["items"][0]
        assert row["tags"] == ["sdk"]
        assert client.get(f"/api/assets/{row['id']}").json()["relations"]
        changed = client.post(
            "/api/assets/tags",
            json={
                "selection": {"kind": "runtime", "pool_id": pool},
                "add": ["ready"],
                "remove": ["sdk"],
            },
        )
        assert changed.status_code == 200, changed.text
        saved = RuntimeRef.from_dict(json.loads(record.read_text()))
        assert saved.metadata == {"tags": ["ready"], "custom": {"v": 1}}
        assert not (record.parent / "metadata.json").exists()
        sidebar = json.loads((root / ".envweaver/runtime-pool.json").read_text())
        assert "tags" not in sidebar["assets"]["base"]
        assert (
            client.patch(
                "/api/tags",
                params={"kind": "runtime", "pool_id": pool, "tag": "ready"},
                json={"tag": "approved"},
            ).status_code
            == 200
        )
        row = listing(client, "runtime")["items"][0]
        assert row["metadata"]["tags"] == ["approved"]
        relations = client.get(f"/api/assets/{row['id']}").json()["relations"]
        assert {r["asset"]["id"] for r in relations} == {task["id"]}
        assert (
            client.patch(
                f"/api/assets/{row['id']}",
                json={
                    "revision": row["revision"],
                    "ref": {**row["spec"], "metadata": {"tags": ["json"], "n": 2}},
                },
            ).status_code
            == 200
        )
        row = client.get(f"/api/assets/{row['id']}").json()
        assert row["tags"] == ["json"]
        copied = client.post(
            f"/api/assets/{row['id']}/copy", json={"name": "python/work:v2", "revision": row["revision"]}
        )
        assert copied.status_code == 200, copied.text
        assert copied.json()["name"] == copied.json()["spec"]["name"] == "python/work:v2"
        assert copied.json()["spec"]["metadata"] == row["metadata"]
        assert (
            client.delete(
                "/api/tags", params={"kind": "runtime", "pool_id": pool, "tag": "json"}
            ).status_code
            == 200
        )
        assert json.loads(record.read_text())["metadata"] == {"tags": [], "n": 2}
    with TestClient(create_app()) as client:
        add_pool(client, root, kind="runtime")
        assert all(r["tags"] == [] for r in listing(client, "runtime")["items"])


def test_runtime_external_metadata_and_stale_write(tmp_path):
    write_records(tmp_path, "runtimes/base", {"runtime.json": REF})
    record = tmp_path / "runtimes/base/runtime.json"
    app = create_app()
    with TestClient(app) as client:
        pool = add_pool(client, tmp_path, kind="runtime")
        external = {**REF, "metadata": {"tags": ["external"]}}
        record.write_text(json.dumps(external))
        with pytest.raises(ValueError, match="changed"):
            app.state.store.batch_tags(
                Selection(kind="runtime", pool_id=pool), ["stale"], [], "test"
            )
        assert json.loads(record.read_text()) == external
        row = listing(client, "runtime", tags=["external"])["items"][0]
        result = client.patch(
            f"/api/assets/{row['id']}",
            json={"revision": row["revision"], "metadata": {"nested": [1, True, None]}},
        )
        assert result.status_code == 200 and result.json()["tags"] == []
        assert json.loads(record.read_text())["metadata"] == {"nested": [1, True, None]}


@pytest.mark.parametrize(
    "metadata", [[], {"tags": "bad"}, {"tags": [2]}, {"n": float("nan")}, {"n": object()}]
)
def test_runtime_metadata_rejects_invalid_json(metadata):
    with pytest.raises((TypeError, ValueError)):
        RuntimeRef("docker", "image", metadata=metadata)


def test_external_metadata_edit_does_not_restore_missing_environment(tmp_path):
    write_records(tmp_path, "runtimes/base", {"runtime.json": REF})
    app = create_app()
    with TestClient(app) as client:
        add_pool(client, tmp_path, kind="runtime")
        row = listing(client, "runtime")["items"][0]
        with app.state.store.transaction():
            app.state.store.assets[row["id"]]["state"] = "missing"
        record = tmp_path / "runtimes/base/runtime.json"
        record.write_text(json.dumps({**REF, "metadata": {"tags": ["updated"]}}))
        assert listing(client, "runtime")["items"][0]["state"] == "missing"
