"""SDK, file edits and management operations share Task metadata.tags."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from envweaver import Runtime, RuntimeRef, TaskPool, Verifier
from envweaver.management.api import create_app
from envweaver.management.models import Selection
from envweaver.management.pools import PoolFiles
from tests.management.test_api import install_task, listing, package
from tests.management.test_api import managed as managed
from tests.management.test_pools import add_pool


def test_sdk_save_ui_tags_and_sdk_load_share_metadata(tmp_path, runtime):
    image = tmp_path / "image"
    image.mkdir()
    verifier = tmp_path / "verify/run.py"
    verifier.parent.mkdir()
    verifier.write_text('print("{\\"score\\": 1}")')
    instance = runtime.start(ref=RuntimeRef("test", str(image)))
    pool = TaskPool(root=tmp_path / "pool", runtime=runtime)
    try:
        ref = pool.save(
            instance=instance,
            name="sdk",
            goal=[{"type": "text", "text": "task"}],
            verifier=Verifier(entry=verifier, timeout=30, runtime=runtime),
            verifier_runtime_ref=RuntimeRef("test", str(image)),
            cwd="/",
            metadata={"source": "sdk", "tags": ["initial"]},
        )
    finally:
        runtime.close(instance=instance)
    material = Path(ref).parent / "verifier/run.py"
    inode = material.stat().st_ino
    before = material.read_bytes()
    with TestClient(create_app()) as client:
        pool_id = add_pool(client, pool.root)
        row = listing(client, pool_id=pool_id)["items"][0]
        assert row["tags"] == row["metadata"]["tags"] == ["initial"]
        assert (
            client.post(
                "/api/assets/tags",
                json={
                    "selection": {"kind": "task", "pool_id": pool_id},
                    "add": ["reviewed"],
                    "remove": ["initial"],
                },
            ).status_code
            == 200
        )
        assert pool.load(task_ref=ref).metadata == {"source": "sdk", "tags": ["reviewed"]}
        assert material.stat().st_ino == inode and material.read_bytes() == before
        assert (
            client.patch(
                "/api/tags",
                params={"kind": "task", "pool_id": pool_id, "tag": "reviewed"},
                json={"tag": "approved", "color": "#dc2626"},
            ).status_code
            == 200
        )
        assert pool.load(task_ref=ref).metadata["tags"] == ["approved"]
        assert (
            client.delete(
                "/api/tags", params={"kind": "task", "pool_id": pool_id, "tag": "approved"}
            ).status_code
            == 200
        )
        assert pool.load(task_ref=ref).metadata["tags"] == []
        document = json.loads((pool.root / ".envweaver/task-pool.json").read_text())
        assert "tags" not in document["assets"]["sdk"]


def test_external_and_api_metadata_edits_replace_tags(tmp_path):
    root = tmp_path / "pool"
    package(root / "task")
    manifest = root / "task/task.json"
    with TestClient(create_app()) as client:
        pool_id = add_pool(client, root)
        data = json.loads(manifest.read_text())
        data["metadata"]["tags"] = ["from-file"]
        manifest.write_text(json.dumps(data))
        row = listing(client, pool_id=pool_id, tags=["from-file"])["items"][0]
        edited = client.patch(
            "/api/assets/" + row["id"],
            json={
                "revision": row["revision"],
                "metadata": {"source": "api", "tags": ["from-api"]},
            },
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["tags"] == ["from-api"]
        assert json.loads(manifest.read_text())["metadata"] == {
            "source": "api",
            "tags": ["from-api"],
        }
        edited = client.patch(
            "/api/assets/" + row["id"],
            json={
                "revision": edited.json()["revision"],
                "metadata": {"source": "no-tags"},
            },
        )
        assert edited.status_code == 200 and edited.json()["tags"] == []
        assert "tags" not in json.loads(manifest.read_text())["metadata"]


def test_copy_preserves_manifest_tags(managed, tmp_path):
    client, app, _ = managed
    source = package(tmp_path / "source")
    manifest = source / "task.json"
    data = json.loads(manifest.read_text())
    data["metadata"]["tags"] = ["sdk"]
    manifest.write_text(json.dumps(data))
    asset_id = install_task(client, source, tags=["imported"])
    row = client.get("/api/assets/" + asset_id).json()
    assert row["tags"] == ["imported", "sdk"]
    copy = client.post(
        f"/api/assets/{asset_id}/copy", json={"name": "copy", "revision": row["revision"]}
    )
    assert copy.status_code == 200 and copy.json()["metadata"]["tags"] == row["tags"]
    copied = app.state.store.source_address(copy.json()["id"]) / "task.json"
    assert json.loads(copied.read_text())["metadata"]["tags"] == row["tags"]
    assert json.loads(manifest.read_text())["metadata"]["tags"] == ["sdk"]


def test_task_tag_write_rejects_external_change(tmp_path):
    root = tmp_path / "pool"
    package(root / "task")
    app = create_app()
    with TestClient(app) as client:
        pool_id = add_pool(client, root)
        manifest = root / "task/task.json"
        external = json.loads(manifest.read_text())
        external["metadata"]["source"] = "changed concurrently"
        manifest.write_text(json.dumps(external))
        with pytest.raises(ValueError, match="changed"):
            app.state.store.batch_tags(Selection(kind="task", pool_id=pool_id), ["new"], [], "test")
        assert json.loads(manifest.read_text()) == external
        assert not list(root.glob(".*.lock"))
        assert listing(client, pool_id=pool_id)["items"][0]["tags"] == []


def test_task_sidecar_cannot_override_manifest_tags(tmp_path):
    root = tmp_path / "pool"
    package(root / "task")
    files = PoolFiles({"kind": "task", "address": str(root)})
    saved = files.call("metadata")
    files.call(
        "metadata",
        expected=saved["stamp"],
        data={
            "tags": {"unassigned": "#dc2626"},
            "assets": {"task": {"tags": ["obsolete"]}},
        },
    )
    with TestClient(create_app()) as client:
        pool_id = add_pool(client, root)
        assert listing(client, pool_id=pool_id)["items"][0]["tags"] == []
        assert client.post(
            "/api/assets/page", json={"selection": {"kind": "task", "pool_id": pool_id}}
        ).json()["tags"] == [
            {"tag": "unassigned", "color": "#dc2626", "count": 0},
        ]


@pytest.mark.parametrize("tags", ["one", None, [1], [""], ["x" * 101]])
def test_invalid_task_tags_fail_sdk_load_and_management(tmp_path, tags):
    root = tmp_path / "pool"
    package(root / "task")
    path = root / "task/task.json"
    data = json.loads(path.read_text())
    data["metadata"]["tags"] = tags
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        TaskPool(root=root, runtime=Runtime()).load(task_ref=path)
    with TestClient(create_app()) as client:
        pool_id = add_pool(client, root)
        assert listing(client, pool_id=pool_id)["total"] == 0
        assert client.get("/api/pools").json()[0]["error"]
