# 资产管理

Task Pool 和 Runtime Pool 各自管理标签，支持筛选、详情、编辑和批量操作。页面只调用 HTTP API。

## 启动

```bash
pip install -e '.[management]'
python -m envweaver.management --port 8788
```

打开 `http://127.0.0.1:8788` 即可使用，无需登录或 Token。

API 以服务进程的系统权限访问文件、Docker 和 SSH；操作记录使用服务进程的系统用户名。默认只监听本机。

接口说明位于 `/docs`，OpenAPI 位于 `/openapi.json`；HTTP 请求不需要认证头。无需修改核心接口文档的导航。

## 选择 Pool

在 Task Pool 或 Runtime Pool 顶部点击“切换目录”，填写目录。支持本机路径及 `ssh://user@server/path`；标题右侧显示当前路径，再次点击“切换目录”可更换 Pool。刷新或重新打开页面会继续使用当前目录；只有服务重启后才需重新选择。服务没有默认 Pool。

- Task Pool：填写 `TaskPool(root=...)` 的目录，直接读取其中的 `*/task.json`。
- Runtime Pool：填写 `Runtime(root=...)` 的目录，直接读取其中的 `runtimes/*/runtime.json`。

选定目录后，SDK 的 `save()` 无需增加任何调用；页面在上次检查完成三秒后再次检查变化，隐藏页面时暂停检查；也可以点击刷新。编辑写回原目录。远端目录使用已有 SSH 配置和免密认证，服务器需要 Python 3。

同名资产可以存在于不同 Pool。标签、筛选和批量选择仅作用于当前 Pool；删除环境时检查所有已选择目录中的 Task 依赖。无法读取某个 Pool 时保留已有索引并显示错误，删除操作须等关联检查完成。

`POST /api/pools` 接收 `kind`（`task` 或 `runtime`）、`address` 和可选的 `name`，返回 Pool ID 并设为该类型的当前目录；`GET /api/pools` 列出本次服务连接的目录，`selected` 标识当前目录。列表和批量接口的 `Selection.pool_id` 指定 Pool；标签与后端菜单接口使用 `pool_id` 查询参数。仅连接一个同类型 Pool 时可省略；连接多个时必须填写。

```python
pool = TaskPool(root="/data/tasks", runtime=runtime)
# 页面选择 /data/tasks 后，照常调用 pool.save(...) 即可自动显示。
```

## 标签

Task 标签保存在 `task.json` 的 `metadata.tags` 中，例如 `{"tags": ["Terminal-Bench 2.1", "已适配"]}`。SDK 保存时传入，页面自动读取；页面打标签、移除、重命名和删除标签直接修改该字段。标签名称去除首尾空白并去重，每项为 1–100 个字符。

标签颜色和未关联任务的标签保存在 `.envweaver/task-pool.json`；Runtime 标签保存在每条环境的 `runtime.json` 的 `metadata.tags` 中，颜色和未关联环境的标签保存在 `.envweaver/runtime-pool.json`。复制或移动整个 Pool 时一并保留 `.envweaver/`。不同 Pool 的标签相互独立，同名标签可以使用不同颜色；可以先创建标签，再关联当前 Pool 的记录。

- 左侧“＋”创建标签；点击标签图标重命名、选择 24 种颜色或删除。删除需确认，只移除当前 Pool 中的关联，保留资产。
- 选中记录，点击“操作 → 打标签”选择已有标签；同一菜单提供删除。
- 表格标签列限宽，支持触控板和 Shift＋滚轮横向滚动；悬停时“×”覆盖标签右端，确认后只移除当前记录的标签。

列表响应的 `tags` 和详情响应的 `tag_definitions` 提供标签、颜色及关联数量。标签写入接口须指定 `kind=task` 或 `kind=runtime`：`POST /api/tags?kind=task` 创建；`PATCH /api/tags?kind=task&tag=原名称` 重命名或换色；`DELETE /api/tags?kind=task&tag=名称` 删除。创建和修改的 JSON 使用 `{"tag":"已适配","color":"#2563eb"}`，颜色省略时为蓝色；名称在各自 Pool 内唯一。批量关联使用 `POST /api/assets/tags`，由 `selection.kind` 指定 Pool。

## 资产操作

- **筛选**：多个标签同时匹配；支持名称、后端、创建者、创建时间范围、最后修改时间范围和 metadata 顶层字段的标量精确匹配。Task 的后端筛选使用做题环境。
- **编辑**：详情标题右侧进入编辑。Task 可编辑 JSON 及已有文本文件；被 Task 使用的 Runtime 不能直接更换环境引用。页面不提供复制操作。
- **批量操作**：可逐条勾选；表头复选框选择当前筛选条件下的全部记录，跨页有效。全选后可取消个别记录，打标签和删除均按该范围执行；点击“取消选择”清空勾选。
- **关联查看**：Task 显示做题、验证环境；Runtime 显示引用它的 Task。环境记录删除或实体缺失后，关联 Task 会提示异常。
- **删除**：先查看影响清单，再确认。默认从当前列表移除、保留实体；文件未变化时自动扫描不会重新显示，外部修改或重新保存后会再次显示。勾选后才删除任务文件夹或环境镜像、磁盘。有关联时必须再次勾选确认。确认有效期十分钟；资产或关联变化后须重新确认。Docker 被容器占用时不强制删除。

单次材料编辑上限为 16 MiB。服务器 SSH 使用已有 OpenSSH 配置和免密认证。任务材料不接受符号链接。

长操作在“操作记录”查看状态、成功项和失败项。批量操作可能部分成功；重试前查看结果。操作记录只保留在本次服务中，关闭服务会等待正在执行的操作结束。删除关联检查覆盖本次服务已连接的 Pool；未选择的目录不在检查范围内。

## 代码目录

```text
src/envweaver/management/
├── api.py                # HTTP 路由
├── models.py             # 请求字段
├── store.py              # 内存索引、Pool 标签与并发写保护
├── pools.py              # Pool 位置选择与文件索引同步
├── _pool_files.py        # 本机与 SSH 共用的文件操作
├── tasks.py              # Task 文件和引用维护
├── runtimes.py           # Runtime 引用及环境实体操作
├── deletion.py           # 删除影响预览与确认
├── jobs.py               # 后台操作及状态
└── __main__.py           # 启动入口
web/
├── index.html
└── src/
    ├── api/client.js
    ├── components/       # 弹窗、菜单和标签交互
    ├── pages/assets.js
    ├── main.js
    └── style.css
```

前端使用浏览器原生模块，不需要 Node.js 或构建步骤；安装包包含页面资源。管理依赖通过 `management` extra 安装，不影响只使用 Runtime、Task、Agent、Eval 的用户。

验收项目与执行结果见 [验收清单](../tests/management/ACCEPTANCE.md)，加载与批量修改的实测见 [性能检查](../tests/management/PERFORMANCE.md)。

批量接口使用 `Selection`：`ids` 为空表示全部匹配记录，`exclude_ids` 排除指定记录；执行时按筛选条件匹配，删除仍须预览并确认。

后端筛选支持多选，选择多个后端时匹配其中任一个，未选表示全部。菜单保留 Docker、QEMU，并从当前 Pool 的资产中自动发现其他后端。列表响应的 `backends` 提供已登记后端；`Selection.backends` 使用字符串列表，例如 `["docker", "kube"]`，与其他筛选条件同时生效。

时间筛选使用 `Selection.created_after`、`created_before`、`updated_after`、`updated_before`，填写带时区的 ISO 8601 时间，空字符串不限制，包含起终边界。两个时间范围同时生效；页面日期按浏览器本地时区的全天计算。

任务说明按 goal 顺序显示文字和图片；图片预览最高 330px，点击后在居中弹窗内等比例显示，弹窗占据屏幕大部分区域，按 Esc 或点击关闭退出。

点击详情中的关联资产可继续查看；左上角“返回”逐层回到上一条详情，并恢复滚动位置。关闭详情后清空返回路径。

拖动详情面板左边缘可调整宽度，当前浏览器会记住设置。点击面板外的遮罩区域关闭详情。

创建者支持多选，选项来自当前 Pool。列表响应的 `creators` 提供创建者名称；`Selection.creators` 填写名称列表，如 `["alice", "bob"]`，匹配任一名称，空列表不限制。

Task、Runtime 的 JSON 详情使用等宽字体、四空格缩进、层级参考线及语法高亮；长行自动换行。

列表显示名称、标签、后端、创建者、创建时间、最后修改者和最后修改时间。按住 Shift 滚动鼠标滚轮可横向查看；拖动表头右边缘调整列宽，Task Pool 和 Runtime Pool 的列宽分别保存在当前浏览器。`POST /api/assets/page` 的列表摘要包含上述字段。


## 列表接口

`POST /api/assets/page` 返回分页摘要、当前 Pool 的标签/后端/创建者选项和索引版本。请求字段：`selection` 必填，`page` 默认 1，`page_size` 默认 30（最多 200），`sort` 默认 `updated_at`，`descending` 默认 `true`。

```json
{"selection": {"kind": "task", "pool_id": "POOL_ID"}, "page": 1, "page_size": 30}
```

返回 `items`、`total`、`page`、`page_size`、`tags`、`backends`、`creators`、`revision`。筛选选项覆盖当前 Pool，不受当前搜索和分页限制；完整 Task、RuntimeRef 从 `/api/assets/{id}` 读取。请求字段无效返回 422；Pool 无法读取时保留已有索引，错误由 `/api/pools` 和 `/api/index` 的 Pool 信息返回。

刷新页面会恢复当前 Pool 页签、打开的资产详情、返回路径和详情滚动位置；这些状态保存在当前浏览器标签页的会话存储中。主动关闭详情后，刷新不再打开它。


详情接口 `GET /api/assets/{id}` 一次返回完整资产、`relations` 及 `tag_definitions`（所属 Pool 的标签、颜色和数量；无所属 Pool 时为空列表）。标签定义与资产使用同一次同步结果。`pool_id` 可省略；传入时同步当前 Pool，目标条目属于其他 Pool 时也同步目标 Pool。支持 gzip 的客户端会收到压缩后的大 JSON 响应；文件下载保持原内容。
