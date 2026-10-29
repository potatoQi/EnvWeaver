"""HTTP contracts shared by asset operations."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .._util import clean_tags

Kind = Literal["task", "runtime", "trajectory"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Selection(Input):
    """筛选资产；多个标签须同时满足，backends 和 creators 分别匹配其中任一后端、创建者。"""

    kind: Kind
    pool_id: str | None = None
    ids: list[str] = Field(default_factory=list, max_length=10000)
    exclude_ids: list[str] = Field(default_factory=list, max_length=10000)
    include_all: bool = Field(default=False, description="选择全部匹配记录；否则 ids 与 task_groups 取并集，两者均空时也不限制。")
    task_groups: list[str] = Field(default_factory=list, description="选中的 Traj Pool 分组键，与 ids 取并集。")
    exclude_task_groups: list[str] = Field(default_factory=list, description="排除的 Traj Pool 分组键；ids 可重新选入组内个别记录，exclude_ids 始终优先排除。")
    query: str = ""
    task_group: str | None = Field(default=None, description="Traj Pool 的来源 Task 分组键；不填则不限分组。")
    agents: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    stop_reasons: list[str] = Field(default_factory=list)
    completed: bool | None = None
    score_min: float | None = Field(default=None, ge=0, le=1)
    score_max: float | None = Field(default=None, ge=0, le=1)
    tags: list[str] = Field(default_factory=list)
    backends: list[str] = Field(default_factory=list, max_length=100)
    creators: list[str] = Field(
        default_factory=list, description="创建者名称列表；匹配任一名称，空列表不限制。"
    )
    created_after: str = Field(
        default="", description="创建时间下限，含边界；填写带时区的 ISO 8601 时间，空字符串不限制。"
    )
    created_before: str = Field(
        default="", description="创建时间上限，含边界；填写带时区的 ISO 8601 时间，空字符串不限制。"
    )
    updated_after: str = Field(
        default="",
        description="最后修改时间下限，含边界；填写带时区的 ISO 8601 时间，空字符串不限制。",
    )
    updated_before: str = Field(
        default="",
        description="最后修改时间上限，含边界；填写带时区的 ISO 8601 时间，空字符串不限制。",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def json_values(cls, value):
        import json

        json.dumps(value, allow_nan=False)
        return value


class PoolInput(Input):
    """选择 SDK 保存资产的根目录；省略名称时使用目录名。"""

    name: str | None = Field(default=None, min_length=1, max_length=249)
    kind: Kind
    address: str = Field(min_length=1)


class Edit(Input):
    """只修改提供的字段；revision 使用最近读取的值。manifest 属于 Task，ref 属于 Runtime。name 写入对应 Task 或 RuntimeRef 的 name。"""

    revision: int = Field(ge=1)
    name: str | None = None
    tags: list[str] | None = Field(
        default=None,
        description="写入资产的 metadata.tags；显式填写时覆盖 manifest 或 metadata 中的 tags。",
    )
    metadata: dict | None = None
    manifest: dict | None = None
    ref: dict | None = None
    host: str | None = None


class Copy(Input):
    name: str = Field(min_length=1, max_length=249)
    revision: int = Field(ge=1)


class Tags(Input):
    selection: Selection
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class DeletePlan(Input):
    selection: Selection
    physical: bool = False


class DeleteConfirm(Input):
    """使用删除预览返回的 token；存在关联资产时须明确确认影响。"""

    token: str
    acknowledge_dependencies: bool = False


class FileEdit(Input):
    revision: int = Field(ge=1)
    content_base64: str


class Query(Input):
    selection: Selection
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=30, ge=1, le=200)
    sort: Literal["name", "created_at", "updated_at"] = "updated_at"
    descending: bool = True
    group_by: Literal["task"] | None = Field(default=None, description="Traj Pool 按来源 Task 分组分页；默认逐条记录分页。")


class Asset(BaseModel):
    """资产记录；pool_id 为 None 时表示 Task 中尚未保存到 Runtime Pool 的环境引用。"""

    pool_id: str | None

    id: str
    kind: Kind
    name: str
    spec: dict
    metadata: dict
    tags: list[str]
    host: str | None
    created_by: str
    updated_by: str
    created_at: str
    updated_at: str
    revision: int
    deleted: bool
    state: Literal["registered", "missing"]
    last_error: str | None


class Relation(BaseModel):
    role: Literal["task_runtime_ref", "verifier_runtime_ref", "task", "trajectory"]
    asset: Asset


class AssetDetail(Asset):
    """完整资产、关联关系和所属 Pool 的标签定义。"""

    relations: list[Relation]
    tag_definitions: list["TagCount"]


class Operation(BaseModel):
    """后台操作；result 包含成功项和逐项错误，error 表示整次操作未完成的原因。"""

    id: str
    kind: str
    actor: str
    status: Literal["pending", "running", "completed", "partial", "failed"]
    result: dict | None
    error: str | None
    created_at: str
    updated_at: str


class AssetSummary(BaseModel):
    id: str
    name: str


class SelectedAsset(AssetSummary):
    kind: Kind


class DeletePreview(BaseModel):
    token: str
    physical: bool
    selected: list[SelectedAsset]
    affected_tasks: list[AssetSummary]
    affected_runtimes: list[AssetSummary]
    affected_trajectories: list[AssetSummary]
    expires_in: int = Field(description="确认有效秒数。")


class TagDefinition(Input):
    """当前 Pool 的标签；color 为十六进制 RGB，默认蓝色。"""

    tag: str
    color: str = Field(default="#2563eb", pattern=r"^#[0-9a-fA-F]{6}$")

    @field_validator("tag")
    @classmethod
    def clean_name(cls, value):
        return clean_tags([value])[0]

    @field_validator("color")
    @classmethod
    def normalize_color(cls, value):
        return value.lower()


class TagCount(TagDefinition):
    count: int


class Material(BaseModel):
    path: str = Field(description="相对于任务文件夹的路径。")
    size: int = Field(description="文件大小，单位字节。")


class AssetRow(BaseModel):
    """列表显示字段；完整内容由资产详情接口返回。"""

    id: str
    name: str
    tags: list[str]
    backend: str
    agent: str | None = None
    model: str | None = None
    score: float | None = None
    duration_seconds: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error_count: int = 0
    agent_stop_reason: str | None = None
    completed: bool | None = None
    rollout_id: str | None = None
    started_at: str | None = None
    created_by: str
    created_at: str
    updated_by: str
    updated_at: str


class TaskGroup(BaseModel):
    key: str
    name: str
    task_ref: str | None
    count: int


class AssetPage(BaseModel):
    """一次返回分页列表及当前 Pool 的筛选选项。"""

    items: list[AssetRow]
    groups: list[TaskGroup] = Field(default_factory=list)
    group_total: int | None = None
    total: int
    page: int
    page_size: int
    tags: list[TagCount]
    backends: list[str]
    creators: list[str]
    agents: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    revision: str
