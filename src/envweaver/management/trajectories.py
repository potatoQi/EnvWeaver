"""Read-only rollout contents with editable Pool metadata."""

import shutil
import tempfile
import zipfile
from pathlib import Path

from .._io.storage import local_source
from .._util import metadata_tags
from .store import encode


class Trajectories:
    def __init__(self, store):
        self.store = store

    def edit(self, asset_id, value, actor):
        if value.model_fields_set - {"revision", "metadata", "tags"}:
            raise ValueError("轨迹只允许编辑 metadata 和标签")
        with self.store.transaction():
            row = self.store.record(asset_id)
            if row["kind"] != "trajectory":
                raise ValueError("不是轨迹记录")
            self.store.check_revision(row, value.revision)
            metadata = row["metadata"] if value.metadata is None else value.metadata
            encode(metadata)
            tags = value.tags if value.tags is not None else metadata_tags(metadata)
            row["spec"]["metadata"] = metadata
            self.store.update(asset_id, metadata=metadata, spec=row["spec"])
            self.store.set_tags(asset_id, tags)
            self.store.touch(asset_id, actor)
            return self.store.record(asset_id)

    def export(self, selection):
        if selection.kind != "trajectory":
            raise ValueError("请选择 Traj Pool")
        with self.store.read():
            addresses = [self.store.source_address(i) for i in self.store.select(selection)]
        if not addresses:
            raise ValueError("请至少选择一条轨迹")
        directory = Path(tempfile.mkdtemp(prefix="envweaver-traj-export-"))
        target = directory / "trajectories.zip"
        try:
            with zipfile.ZipFile(
                target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1
            ) as archive:
                for address in addresses:
                    with local_source(address) as source:
                        for path in sorted(source.rglob("*")):
                            if path.is_symlink():
                                raise ValueError("轨迹产物不能包含符号链接")
                            if path.is_file():
                                archive.write(
                                    path, str(Path(source.name) / path.relative_to(source))
                                )
            return target
        except BaseException:
            shutil.rmtree(directory)
            raise
