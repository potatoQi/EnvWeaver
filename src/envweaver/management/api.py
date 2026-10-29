"""HTTP routes. Asset rules live in the management services."""

import getpass
import logging
import shutil
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.middleware.gzip import GZipMiddleware

from ..errors import RuntimeError as RuntimeFailure
from .deletion import Deletion
from .jobs import Jobs
from .models import (
    Asset,
    AssetDetail,
    AssetPage,
    Copy,
    DeleteConfirm,
    DeletePlan,
    DeletePreview,
    Edit,
    FileEdit,
    Kind,
    Material,
    Operation,
    PoolInput,
    Query,
    Selection,
    TagDefinition,
    Tags,
)
from .pools import Pools
from .runtimes import Runtimes
from .store import Conflict, Missing, Store
from .tasks import Tasks
from .trajectories import Trajectories


class ApiCompression:
    """Compress API JSON; leave file downloads and static assets unchanged."""

    def __init__(self, app):
        self.app = app
        self.compressed = GZipMiddleware(app, minimum_size=1024, compresslevel=3)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        handler = (
            self.compressed
            if scope["type"] == "http" and path.startswith("/api/") and not path.endswith("/file")
            else self.app
        )
        await handler(scope, receive, send)


def create_app(*, environment_files=None) -> FastAPI:
    """创建资产管理 HTTP 服务。

    Args:
        environment_files: 测试时替换环境文件操作；不填使用 Docker、QEMU。

    Returns:
        FastAPI 应用；接口说明位于 /docs，前端位于 /。

    Example:
        app = create_app()
    """
    store = Store()
    runtimes = Runtimes(store, environment_files)
    tasks = Tasks(store, runtimes)
    trajectories = Trajectories(store)
    pools = Pools(store, tasks)
    deletion = Deletion(store, runtimes, pools)
    jobs = Jobs()

    @asynccontextmanager
    async def lifespan(app):
        yield
        jobs.close()

    app = FastAPI(title="EnvWeaver Management API", version=version("envweaver"), lifespan=lifespan)
    app.add_middleware(ApiCompression)
    app.state.store, app.state.jobs = store, jobs
    app.state.tasks, app.state.runtimes = tasks, runtimes
    app.state.deletion = deletion
    app.state.pools = pools
    actor = getpass.getuser()

    @app.exception_handler(Exception)
    async def server_error(request, error):
        logging.getLogger(__name__).error("Management request failed", exc_info=error)
        return JSONResponse(status_code=500, content={"detail": "操作失败；请检查服务日志"})

    async def expected_error(request, error):
        code = (
            409
            if isinstance(error, (Conflict, FileExistsError))
            else 404
            if isinstance(error, (Missing, FileNotFoundError))
            else 422
            if isinstance(error, (ValueError, KeyError, TypeError))
            else 502
        )
        return JSONResponse(status_code=code, content={"detail": str(error)})

    for cls in (
        Conflict,
        Missing,
        ValueError,
        KeyError,
        TypeError,
        OSError,
        RuntimeFailure,
    ):
        app.add_exception_handler(cls, expected_error)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if (
            request.url.path != "/api/assets/page"
            and not (request.method == "GET" and request.url.path.startswith("/api/assets/"))
            and request.url.path.startswith("/api/")
            and not request.url.path.startswith(("/api/jobs", "/api/pools"))
        ):
            await run_in_threadpool(pools.sync, request.query_params.get("pool_id"))
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/pools", summary="本次服务连接的 Pool；selected 表示当前目录")
    def pool_list(kind: Kind | None = None):
        return pools.list(kind)

    @app.post("/api/pools", summary="切换当前 Pool 目录；服务重启前保留选择，不复制资产")
    def add_pool(value: PoolInput):
        return pools.add(value)

    @app.get("/api/index", summary="同步文件并返回索引状态")
    def index():
        with store.read():
            return {"revision": store.fingerprint(), "pools": pools.list()}

    @app.post("/api/assets/page", response_model=AssetPage, summary="列表摘要、筛选选项和索引版本")
    def page(value: Query):
        pools.sync(value.selection.pool_id)
        with store.read():
            return {
                **store.listing(
                    value.selection,
                    page=value.page,
                    page_size=value.page_size,
                    sort=value.sort,
                    descending=value.descending,
                    group_by=value.group_by,
                ),
                "tags": store.tag_counts(value.selection.kind, value.selection.pool_id),
                "backends": store.backends(value.selection.kind, value.selection.pool_id),
                "creators": store.creators(value.selection.kind, value.selection.pool_id),
                "agents": store.rollout_values("agent", value.selection.kind, value.selection.pool_id),
                "models": store.rollout_values("model", value.selection.kind, value.selection.pool_id),
                "revision": store.fingerprint(),
            }

    @app.post("/api/tags", response_model=TagDefinition, summary="创建标签；允许尚未关联资产")
    def create_tag(kind: Kind, value: TagDefinition, pool_id: str | None = None):
        return store.create_tag(kind, value, pool_id)

    @app.patch("/api/tags", response_model=TagDefinition, summary="重命名或换色；仅更新指定 Pool")
    def update_tag(kind: Kind, tag: str, value: TagDefinition, pool_id: str | None = None):
        return store.update_tag(kind, tag, value, actor, pool_id)

    @app.delete("/api/tags", summary="删除指定 Pool 的标签及关联；保留资产")
    def delete_tag(kind: Kind, tag: str, pool_id: str | None = None):
        return store.delete_tag(kind, tag, actor, pool_id)

    @app.post("/api/assets/tags", summary="批量增删所选资产的标签")
    def batch_tags(value: Tags):
        return store.batch_tags(value.selection, value.add, value.remove, actor)

    @app.get(
        "/api/assets/{asset_id}", response_model=AssetDetail, summary="查看资产、关联关系和标签定义"
    )
    def detail(asset_id: str, pool_id: str | None = None):
        pools.sync(pool_id)
        with store.read():
            target_pool = store.record(asset_id, deleted=True)["pool_id"]
        if pool_id is not None and target_pool is not None and target_pool != pool_id:
            pools.sync(target_pool)
        with store.read():
            row = store.record(asset_id)
            row["relations"] = store.relations(asset_id)
            row["tag_definitions"] = (
                store.tag_counts(row["kind"], row["pool_id"]) if row["pool_id"] else []
            )
            return row

    @app.patch(
        "/api/assets/{asset_id}",
        response_model=Asset,
        summary="编辑资产；revision 防止覆盖他人修改",
    )
    def edit(asset_id: str, value: Edit):
        service = {"task": tasks, "runtime": runtimes, "trajectory": trajectories}[store.get(asset_id)["kind"]]
        return service.edit(asset_id, value, actor)

    @app.post("/api/trajectories/export", summary="导出选中轨迹及全部附件")
    def export_trajectories(value: Selection):
        path = trajectories.export(value)
        return FileResponse(path, filename="trajectories.zip", background=BackgroundTask(shutil.rmtree, path.parent))

    @app.get("/api/trajectories/{asset_id}/files", response_model=list[Material])
    def trajectory_files(asset_id: str):
        return tasks.files(asset_id)

    @app.get("/api/trajectories/{asset_id}/file")
    def trajectory_file(asset_id: str, path: str):
        with tasks.file_path(asset_id, path) as local:
            return Response(local.read_bytes(), media_type="application/octet-stream")

    @app.post(
        "/api/assets/{asset_id}/copy",
        response_model=Asset,
        summary="复制资产；环境复制仅新建引用记录",
    )
    def copy(asset_id: str, value: Copy):
        if store.get(asset_id)["kind"] == "trajectory":
            raise ValueError("轨迹不支持复制")
        service = tasks if store.get(asset_id)["kind"] == "task" else runtimes
        return service.copy(asset_id, value, actor)

    @app.get("/api/tasks/{asset_id}/files", response_model=list[Material], summary="列出任务材料")
    def files(asset_id: str):
        return tasks.files(asset_id)

    @app.get("/api/tasks/{asset_id}/file", summary="下载任务材料；path 相对于任务目录")
    def file(asset_id: str, path: str):
        with tasks.file_path(asset_id, path) as local:
            return Response(local.read_bytes(), media_type="application/octet-stream")

    @app.put(
        "/api/tasks/{asset_id}/file", response_model=Asset, summary="新增或覆盖材料；最多 16 MiB"
    )
    def put_file(asset_id: str, path: str, value: FileEdit):
        return tasks.write_file(asset_id, path, value, actor)

    @app.delete(
        "/api/tasks/{asset_id}/file", response_model=Asset, summary="删除未被 Task 字段引用的材料"
    )
    def delete_file(asset_id: str, path: str, revision: int):
        return tasks.write_file(
            asset_id, path, FileEdit(revision=revision, content_base64=""), actor, delete=True
        )

    @app.post(
        "/api/delete/plan", response_model=DeletePreview, summary="预览删除影响；确认有效期十分钟"
    )
    def plan(value: DeletePlan):
        return deletion.plan(value, actor)

    @app.post(
        "/api/delete/confirm", response_model=Operation, summary="确认删除；执行前重新检查全部关联"
    )
    def confirm(value: DeleteConfirm):
        return jobs.submit("delete", actor, lambda: deletion.execute(value, actor))

    @app.get("/api/jobs", response_model=list[Operation], summary="最近五十次后台操作")
    def job_list():
        return jobs.list()

    @app.get("/api/jobs/{job_id}", response_model=Operation, summary="后台操作状态和逐项结果")
    def job(job_id: str):
        return jobs.get(job_id)

    bundled = Path(__file__).parent / "web"
    web = bundled if bundled.exists() else Path(__file__).parents[3] / "web"
    if web.is_dir():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app
