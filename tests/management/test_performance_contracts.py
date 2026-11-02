"""Fast listing and batched writes preserve file discovery and conflict checks."""

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from fastapi.testclient import TestClient

from envweaver.management._pool_files import index_digest
from envweaver.management.api import create_app
from envweaver.management.models import Edit
from envweaver.management.pools import PoolFiles
from tests.management.test_api import package
from tests.management.test_pools import add_pool


def test_file_previews_read_current_bytes_without_scanning(tmp_path, monkeypatch):
    folder = package(tmp_path / "sample", "sample")
    app = create_app()
    with TestClient(app) as client:
        pool = add_pool(client, tmp_path)
        row = client.post("/api/assets/page", json={"selection": {"kind": "task", "pool_id": pool}}).json()["items"][0]
        original = PoolFiles.call

        def no_scan(self, action, **kwargs):
            assert action not in {"index", "scan", "metadata"}
            return original(self, action, **kwargs)

        monkeypatch.setattr(PoolFiles, "call", no_scan)
        for kind in ("tasks", "trajectories"):
            path = folder / "new.bin"
            path.write_bytes(b"new bytes\x00\xff" + kind.encode())
            files = client.get(f"/api/{kind}/{row['id']}/files").json()
            assert any(f["path"] == "new.bin" for f in files)
            response = client.get(f"/api/{kind}/{row['id']}/file", params={"path": "new.bin"})
            assert response.content == path.read_bytes()
            path.unlink()
            assert client.get(f"/api/{kind}/{row['id']}/file", params={"path": "new.bin"}).status_code == 404


def test_slow_scan_does_not_block_reads_or_overwrite_concurrent_edit(tmp_path, monkeypatch):
    package(tmp_path / "sample", "sample")
    app = create_app()
    with TestClient(app) as client:
        pool = add_pool(client, tmp_path)
        row = client.post("/api/assets/page", json={"selection": {"kind": "task", "pool_id": pool}}).json()["items"][0]
        row = app.state.store.get(row["id"])
        scanned, release = Event(), Event()
        original = PoolFiles.call
        scans = 0

        def delayed(self, action, **kwargs):
            nonlocal scans
            result = original(self, action, **kwargs)
            if action == "index":
                scans += 1
                if scans == 1:
                    scanned.set()
                    assert release.wait(10)
            return result

        monkeypatch.setattr(PoolFiles, "call", delayed)
        with ThreadPoolExecutor(max_workers=2) as executor:
            future = executor.submit(app.state.pools.sync, pool)
            try:
                assert scanned.wait(5)

                def read_and_edit():
                    assert client.get("/api/pools").status_code == 200
                    assert client.get(f"/api/tasks/{row['id']}/file", params={"path": "task.json"}).status_code == 200
                    return app.state.tasks.edit(row["id"], Edit(revision=row["revision"], metadata={"edited": True}), "test")

                edited = executor.submit(read_and_edit).result(timeout=5)
            finally:
                release.set()
            future.result(timeout=5)
        assert scans == 2
        result = app.state.store.get(row["id"])
        assert result["metadata"] == {"edited": True}
        assert result["revision"] == edited["revision"]


def test_summary_scans_once_and_returns_pool_wide_options(tmp_path, monkeypatch):
    for name in ("alpha", "beta"):
        folder = package(tmp_path / name, name)
        path = folder / "task.json"
        data = json.loads(path.read_text())
        data["goal"][0]["text"] = "long instruction " * 10000
        data["metadata"]["tags"] = [name]
        path.write_text(json.dumps(data))
    with TestClient(create_app()) as client:
        pool = add_pool(client, tmp_path)
        calls = Counter()
        original = PoolFiles.call

        def counted(self, action, **kwargs):
            calls[action] += 1
            return original(self, action, **kwargs)

        monkeypatch.setattr(PoolFiles, "call", counted)
        response = client.post(
            "/api/assets/page",
            json={
                "selection": {"kind": "task", "pool_id": pool, "query": "alpha"},
            },
        )
        assert response.status_code == 200, response.text
        page = response.json()
        assert calls["index"] == 1
        assert calls["metadata"] == calls["scan"] == 0
        assert page["total"] == 1
        assert page["items"][0]["name"] == "alpha"
        assert page["items"][0]["backend"] == "docker"
        assert {tag["tag"] for tag in page["tags"]} == {"alpha", "beta"}
        assert page["backends"] == ["docker"] and page["creators"]
        assert "spec" not in page["items"][0]
        assert len(response.content) < 3000
        assert page["revision"] == client.get("/api/index").json()["revision"]


def test_scan_skips_unchanged_json_but_detects_nested_edits_and_symlinks(tmp_path):
    package(tmp_path / "alpha", "alpha")
    files = PoolFiles({"address": str(tmp_path), "kind": "task"})
    original = files.call("scan")[0]
    known = {"alpha": original["stamp"]}
    assert files.call("scan", known=known) == [{"directory": "alpha", "stamp": original["stamp"]}]
    (tmp_path / "alpha/context/input.txt").write_text("Changed outside the UI")
    changed = files.call("scan", known=known)[0]
    assert changed["stamp"] != original["stamp"] and "spec" in changed
    (tmp_path / "alpha/context/link").symlink_to(tmp_path)
    assert "symlink" in files.call("scan", known={"alpha": changed["stamp"]})[0]["error"]


def test_remote_index_digest_detects_changes_without_transferring_all_stamps(tmp_path):
    package(tmp_path / "alpha", "alpha")
    files = PoolFiles({"address": str(tmp_path), "kind": "task"})
    item = files.call("index", known={})["items"][0]
    known_digest = index_digest({"alpha": item["stamp"]})
    response = files.call("index", known_digest=known_digest)
    assert response == {"metadata": files.call("metadata"), "unchanged": True}
    # Pool annotations are returned even when the asset tree is unchanged.
    files.call("metadata", data={"tags": {"new": {}}, "assets": {}}, expected=response["metadata"]["stamp"])
    assert files.call("index", known_digest=known_digest)["metadata"]["data"]["tags"] == {"new": {}}
    (tmp_path / "alpha/context/input.txt").write_text("external change")
    assert files.call("index", known_digest=known_digest)["unchanged"] is False
    item = files.call("index", known={})["items"][0]
    known_digest = index_digest({"alpha": item["stamp"]})
    (tmp_path / "alpha/context/escape").symlink_to(tmp_path)
    assert files.call("index", known_digest=known_digest)["unchanged"] is False
    (tmp_path / "alpha/context/escape").unlink()
    (tmp_path / "alpha/task.json").unlink()
    assert files.call("index", known_digest=known_digest)["unchanged"] is False


def test_invalid_batch_record_does_not_discard_valid_records(tmp_path):
    for name in ("alpha", "broken", "gamma"):
        package(tmp_path / name, name)
    path = tmp_path / "broken/task.json"
    data = json.loads(path.read_text())
    data["metadata"]["tags"] = [42]  # Fails indexing after filesystem scan succeeds.
    path.write_text(json.dumps(data))
    app = create_app()
    with TestClient(app) as client:
        pool = add_pool(client, tmp_path)
        query = {"selection": {"kind": "task", "pool_id": pool}}
        rows = client.post("/api/assets/page", json=query).json()
        assert {r["name"] for r in rows["items"]} == {"alpha", "gamma"}
        assert client.get("/api/pools").json()[0]["error"]
        data["metadata"]["tags"] = ["fixed"]
        path.write_text(json.dumps(data))
        rows = client.post("/api/assets/page", json=query).json()
        assert rows["total"] == 3
        assert client.get("/api/pools").json()[0]["error"] is None
        assert len(app.state.store.sources) == 3


def test_batch_tag_uses_one_call_and_recovers_after_later_file_conflict(tmp_path, monkeypatch):
    for name in ("alpha", "beta", "gamma"):
        package(tmp_path / name, name)
    app = create_app()
    with TestClient(app) as client:
        pool = add_pool(client, tmp_path)
        selection = {"kind": "task", "pool_id": pool}
        original = PoolFiles.call
        batches = []
        inject = True

        def conflict(self, action, **kwargs):
            nonlocal inject
            if action == "asset_metadata_batch":
                batches.append(kwargs["updates"])
                if inject:
                    inject = False
                    path = tmp_path / kwargs["updates"][1]["directory"] / "task.json"
                    data = json.loads(path.read_text())
                    data["metadata"]["external"] = "preserved"
                    path.write_text(json.dumps(data))
            return original(self, action, **kwargs)

        monkeypatch.setattr(PoolFiles, "call", conflict)
        response = client.post("/api/assets/tags", json={"selection": selection, "add": ["new"]})
        assert response.status_code == 422, response.text
        assert "Pool files changed" in response.json()["detail"]
        assert len(batches) == 1 and len(batches[0]) == 3
        rows = client.post("/api/assets/page", json={"selection": selection}).json()["items"]
        rows = [client.get(f"/api/assets/{row['id']}").json() for row in rows]
        assert sum("new" in row["tags"] for row in rows) == 1
        assert sum(row["metadata"].get("external") == "preserved" for row in rows) == 1
        response = client.post("/api/assets/tags", json={"selection": selection, "add": ["new"]})
        assert response.status_code == 200, response.text
        assert len(batches) == 2 and len(batches[-1]) == 2
        assert all(
            "new" in row["tags"]
            for row in client.post("/api/assets/page", json={"selection": selection}).json()[
                "items"
            ]
        )


def test_fast_stamp_preserves_fingerprint_and_detects_file_changes(tmp_path):
    import hashlib
    import os

    from envweaver.management._pool_files import stamp

    for name in ("a/z", "a.txt", "a-b", ".hidden", "中文/文件", "empty-dir/child"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)

    def expected():
        digest = hashlib.sha256()
        for path in [tmp_path, *sorted(tmp_path.rglob("*"))]:
            info = path.stat()
            digest.update(
                json.dumps(
                    [
                        str(path.relative_to(tmp_path)),
                        info.st_mode,
                        info.st_ino,
                        info.st_size,
                        info.st_mtime_ns,
                    ]
                ).encode()
            )
        return digest.hexdigest()

    previous = stamp(tmp_path)
    assert previous == expected()
    target = tmp_path / "a/z"
    target.write_text("changed")
    assert stamp(tmp_path) == expected() != previous
    previous = stamp(tmp_path)
    target.chmod(0o600)
    assert stamp(tmp_path) == expected() != previous
    previous = stamp(tmp_path)
    replacement = tmp_path / "new"
    replacement.write_bytes(target.read_bytes())
    os.utime(replacement, ns=(target.stat().st_atime_ns, target.stat().st_mtime_ns))
    os.replace(replacement, target)
    assert stamp(tmp_path) == expected() != previous


def test_detail_tag_definitions_and_compression_preserve_full_data(tmp_path):
    folder = package(tmp_path / "alpha", "alpha")
    path = folder / "task.json"
    manifest = json.loads(path.read_text())
    manifest["goal"][0]["text"] = "long task instructions " * 1000
    manifest["metadata"]["tags"] = ["review"]
    path.write_text(json.dumps(manifest))
    binary = folder / "data.bin"
    binary.write_bytes(b"file body" * 1000)
    with TestClient(create_app()) as client:
        pool = add_pool(client, tmp_path)
        row = client.post(
            "/api/assets/page", json={"selection": {"kind": "task", "pool_id": pool}}
        ).json()["items"][0]
        url = f"/api/assets/{row['id']}"
        plain = client.get(url, headers={"Accept-Encoding": "identity"})
        zipped = client.get(url, headers={"Accept-Encoding": "gzip"})
        assert zipped.headers["content-encoding"] == "gzip"
        assert plain.json() == zipped.json()
        assert int(zipped.headers["content-length"]) < len(plain.content) / 4
        assert zipped.json()["spec"] == manifest
        assert (
            zipped.json()["tag_definitions"]
            == client.post(
                "/api/assets/page", json={"selection": {"kind": "task", "pool_id": pool}}
            ).json()["tags"]
        )
        assert all("spec" in relation["asset"] for relation in zipped.json()["relations"])
        file = client.get(
            f"/api/tasks/{row['id']}/file",
            params={"path": "data.bin"},
            headers={"Accept-Encoding": "gzip"},
        )
        assert "content-encoding" not in file.headers
        assert file.content == binary.read_bytes()
        manifest["goal"][0]["text"] = "External edit"
        path.write_text(json.dumps(manifest))
        assert client.get(url).json()["spec"]["goal"][0]["text"] == "External edit"


def test_detail_synchronizes_its_own_pool_when_following_a_relation(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    package(first / "first", "first")
    package(second / "second", "second")
    with TestClient(create_app()) as client:
        a, b = add_pool(client, first), add_pool(client, second)
        row = client.post(
            "/api/assets/page", json={"selection": {"kind": "task", "pool_id": b}}
        ).json()["items"][0]
        path = second / "second/task.json"
        data = json.loads(path.read_text())
        data["goal"][0]["text"] = "Updated in another Pool"
        data["metadata"]["tags"] = ["changed"]
        path.write_text(json.dumps(data))
        response = client.get(f"/api/assets/{row['id']}", params={"pool_id": a})
        assert response.status_code == 200, response.text
        assert response.json()["spec"]["goal"][0]["text"] == "Updated in another Pool"
        assert response.json()["tag_definitions"][0]["tag"] == "changed"
