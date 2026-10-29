"""Environment records and physical Docker/QEMU operations."""

import json
import subprocess
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

from .._io.ssh_transport import _validate_host, command_args
from .._io.storage import local_source
from .._util import metadata_tags
from ..models import RuntimeRef
from ..runtime._connection import fixed_connection
from ..runtime.docker import DockerBackend
from .store import Conflict, digest


def normalize_ref(value):
    ref = RuntimeRef.from_dict(value)
    if (
        not isinstance(ref.backend, str)
        or not ref.backend
        or not isinstance(ref.image, str)
        or not ref.image.strip()
    ):
        raise ValueError("环境须包含 backend 和 image")
    if not isinstance(ref.config, dict):
        raise ValueError("config 须为字典")
    return replace(ref, connection=fixed_connection(ref.backend, ref.connection)).to_dict()


def same_environment(left, right):
    return all(
        left.get(key) == right.get(key) for key in ("backend", "image", "config", "connection")
    )


def command(host, args):
    argv = command_args(host, args) if host else args
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800)
    if result.returncode:
        raise OSError(result.stderr.decode(errors="replace"))
    return result.stdout.decode()


class EnvironmentFiles:
    """Inspect identities before destructive operations; never force-remove Docker images."""

    def inspect(self, asset):
        ref, host = asset["spec"], asset["host"]
        if ref["backend"] == "docker":
            backend = DockerBackend()
            daemon = backend._docker("info", "--format", "{{.ID}}", host=host)
            image = backend._docker(
                "image", "inspect", "--format", "{{.Id}}", ref["image"], host=host
            )
            return {"key": ["docker", daemon, image], "dependencies": [], "image": image}
        if ref["backend"] == "qemu":
            code = "import json,pathlib,sys; p=pathlib.Path(sys.argv[1]).resolve(strict=True); print(json.dumps([pathlib.Path('/etc/machine-id').read_text().strip(),str(p)]))"
            machine, image = json.loads(command(host, ["python3", "-c", code, ref["image"]]))
            chain = json.loads(
                command(host, ["qemu-img", "info", "--backing-chain", "--output=json", image])
            )
            paths = [
                row.get("full-backing-filename")
                for row in chain
                if row.get("full-backing-filename")
            ]
            if paths:
                paths = json.loads(
                    command(
                        host,
                        [
                            "python3",
                            "-c",
                            "import json,pathlib,sys; print(json.dumps([str(pathlib.Path(p).resolve(strict=True)) for p in sys.argv[1:]]))",
                            *paths,
                        ],
                    )
                )
            return {
                "key": ["qemu", machine, image],
                "dependencies": [["qemu", machine, p] for p in paths],
                "image": image,
            }
        raise ValueError("实体删除目前支持 Docker、QEMU")

    def delete(self, asset, identity):
        if asset["spec"]["backend"] == "docker":
            DockerBackend()._docker("image", "rm", identity["image"], host=asset["host"])
        else:
            command(
                asset["host"],
                [
                    "python3",
                    "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).unlink()",
                    identity["image"],
                ],
            )


class Runtimes:
    def __init__(self, store, files=None):
        self.store = store
        self.files = files or EnvironmentFiles()

    def ensure(self, ref, host, actor):
        ref, host = normalize_ref(ref), _validate_host(host)
        matches = [
            r
            for r in self.store.assets.values()
            if r["kind"] == "runtime" and r["host"] == host and same_environment(r["spec"], ref)
        ]
        matches.sort(
            key=lambda r: (
                r["id"] not in self.store.sources,
                r["deleted"],
                r["state"] != "registered",
            )
        )
        if matches:
            return self.store.record(matches[0]["id"], deleted=True)
        return self.store.insert(
            kind="runtime",
            name=ref["name"],
            spec=ref,
            host=host,
            actor=actor,
            reference=True,
        )

    def persist(self, row, *, directory=None):
        from .._util import write_json
        from .pools import PoolFiles

        source = self.store.sources.get(row["id"])
        files = PoolFiles(self.store.pool_record(row["pool_id"], "runtime"))
        directory = source["directory"] if source else directory
        if directory is None:
            raise ValueError("请在 Task 中编辑此环境引用，或保存到 Runtime Pool")
        with tempfile.TemporaryDirectory(prefix="envweaver-runtime-") as temporary:
            stage = Path(temporary) / "record"
            if source:
                from .tasks import copy_materials

                with local_source(files.address(directory)) as original:
                    copy_materials(original, stage)
            else:
                stage.mkdir()
            write_json(stage / "runtime.json", row["spec"])
            location = stage / "metadata.json"
            details = json.loads(location.read_text()) if location.exists() else {}
            write_json(location, {**details, "host": row["host"]})
            stamp = files.publish(directory, stage, source["stamp"] if source else None)
        self.store.remember_source(row["id"], directory, stamp)

    def edit(self, asset_id, value, actor):
        with self.store.transaction():
            row = self.store.record(asset_id)
            self.store.check_revision(row, value.revision)
            if value.manifest is not None:
                raise ValueError("环境不包含 manifest")
            data = dict(value.ref if value.ref is not None else row["spec"])
            if value.name is not None:
                data["name"] = value.name
            if value.metadata is not None:
                data["metadata"] = value.metadata
            ref = normalize_ref(data)
            host = _validate_host(value.host) if "host" in value.model_fields_set else row["host"]
            environment_changed = not same_environment(ref, row["spec"]) or host != row["host"]
            if environment_changed and self.store.relations(asset_id):
                raise Conflict("该环境被 Task 引用；请复制环境后修改，再调整 Task 的环境引用")
            tags = metadata_tags(ref["metadata"])
            self.store.update(
                asset_id,
                name=ref["name"],
                spec=ref,
                host=host,
                metadata=ref["metadata"],
            )
            self.store.set_tags(
                asset_id,
                value.tags if value.tags is not None else tags,
            )
            if environment_changed:
                self.store.assets[asset_id]["state"] = "registered"
            self.store.touch(asset_id, actor)
            result = self.store.record(asset_id)
            self.persist(result)
            return result

    def copy(self, asset_id, value, actor):
        with self.store.transaction():
            row = self.store.record(asset_id)
            self.store.check_revision(row, value.revision)
            if row["pool_id"] is None:
                raise ValueError("请先将环境引用保存到 Runtime Pool")
            ref = normalize_ref({**row["spec"], "name": value.name})
            directory = uuid.uuid4().hex
            copied = self.store.insert(
                kind="runtime",
                name=ref["name"],
                spec=ref,
                host=row["host"],
                pool_id=row["pool_id"],
                actor=actor,
                asset_id=digest([row["pool_id"], directory]),
            )
            self.persist(copied, directory=directory)
            self.store.assets[copied["id"]]["state"] = row["state"]
            return self.store.record(copied["id"])
