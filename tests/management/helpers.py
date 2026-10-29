"""Explicit Pool setup shared by management workflow tests."""

from envweaver.management.api import create_app
from envweaver.management.models import PoolInput


def configured_app(root, **kwargs):
    app = create_app(**kwargs)
    for kind in ("task", "runtime"):
        directory = root / (kind + "-pool")
        directory.mkdir(parents=True, exist_ok=True)
        app.state.pools.add(PoolInput(kind=kind, address=str(directory)))
    return app


def seed_runtime(client, *, json, params=None):
    """Write SDK-format records, then let the management index discover them."""
    import uuid

    from envweaver._io.storage import write_records

    pool_id = (params or {}).get("pool_id")
    pool = next(
        p
        for p in client.get("/api/pools").json()
        if p["kind"] == "runtime" and (p["id"] == pool_id if pool_id else p["selected"])
    )
    directory = uuid.uuid4().hex
    write_records(
        pool["address"],
        f"runtimes/{directory}",
        {
            "runtime.json": {**json["ref"], "name": json["name"]},
            "metadata.json": {
                **json.get("metadata", {}),
                "host": json.get("host"),
            },
        },
    )
    rows = client.post(
        "/api/assets/page",
        json={"selection": {"kind": "runtime", "pool_id": pool["id"], "query": json["name"]}},
    ).json()["items"]
    row = next(r for r in rows if r["name"] == json["name"])
    row = client.get(f"/api/assets/{row['id']}").json()
    if json.get("tags"):
        response = client.patch(
            f"/api/assets/{row['id']}", json={"revision": row["revision"], "tags": json["tags"]}
        )
        assert response.status_code == 200, response.text
    return client.get(f"/api/assets/{row['id']}")
