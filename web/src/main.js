import { createSelection } from "./components/selection.js";
import { taskGroups } from "./components/task-groups.js";
import { readSession, writeSession } from "./components/session.js";
import { setupTableColumns } from "./components/table-columns.js";
import { renderMultiFilter, renderSingleFilter } from "./components/filters.js";
import { renderPagination } from "./components/pagination.js";
import { openMenu } from "./components/menu.js";
import {
  renderTags,
  tagChip,
  createTag,
  assignTags,
  bindRowTags,
} from "./components/tags.js";
import { api, setPool } from "./api/client.js";
import {
  $,
  esc,
  pretty,
  date,
  formatDuration,
  formatTokens,
  download,
  field,
  showModal,
  toast,
  bind,
} from "./components/ui.js";
import { openAsset, configure, restoreDetail } from "./pages/assets.js";
const state = {
  kind: ["runtime", "trajectory"].includes(readSession("activeTab")) ? readSession("activeTab") : "task",
  pools: { task: null, runtime: null, trajectory: null },
  page: 1,
  pageSize: 30,
  sort: "updated_at:desc",
  total: 0,
  tags: new Set(),
  backends: new Set(),
  agents: new Set(),
  models: new Set(),
  creators: new Set(),
  tagDefinitions: [],
  filters: { kind: "task" },
};
let rowSignature = "";
let requestNumber = 0;
let loadController;
let loading = false;
configure(() => load());
const selectTableColumns = setupTableColumns($(".table-wrap table"));
const selectedRows = createSelection();
const groupedRows = taskGroups($("#rows"), renderRow, bindRows, {
  count: group => selectedRows.groupCount(group),
  toggle: (group, checked) => { selectedRows.toggleGroup(group, checked); renderSelection(); },
});
function poolId() {
  return state.pools[state.kind]?.id || null;
}
function renderRow(r, group = "") {
  const title = state.kind === "trajectory" ? r.rollout_id.slice(0, 12) : r.name;
  const hint = state.kind === "trajectory" ? `${r.rollout_id} · ${date(r.started_at)}` : r.name;
  return `<tr><td class="check"><input type="checkbox" data-id="${esc(r.id)}" data-group="${esc(group)}" aria-label="选择 ${esc(title)}"></td><td><button class="asset-name" title="${esc(hint)}" data-open="${esc(r.id)}">${esc(title)}</button></td><td><div class="table-tags" tabindex="0" aria-label="${esc(title)} 的标签">${r.tags.map(t => tagChip(t, state.tagDefinitions, r.id)).join("") || '<span class="muted">—</span>'}</div></td>${rowFields(r)}</tr>`;
}
function bindRows() {
  bindRowTags(state.kind, async () => {
    clearSelection();
    await load();
  });
  $("#rows").querySelectorAll("[data-errors]").forEach(button => {
    button.onclick = async () => {
      button.disabled = true;
      try {
        const selectedPool = poolId();
        const row = await api(`/assets/${button.dataset.errors}?pool_id=${encodeURIComponent(selectedPool)}`);
        if (!button.isConnected || selectedPool !== poolId()) return;
        showModal("Errors", row.spec.errors.map(error => `<section class="trajectory-tool"><h4>${esc(error.stage)} · ${esc(error.type)}</h4><div class="trajectory-text">${esc(error.message)}</div></section>`).join("") || "暂无错误", {wide: true});
      } catch (error) { toast(error.message, true); }
      finally { button.disabled = false; }
    };
  });
  $("#rows").querySelectorAll("[data-open]").forEach(button => {
    button.onclick = () => openAsset(button.dataset.open).catch(e => toast(e.message, true));
  });
  $("#rows").querySelectorAll("input[data-id]").forEach(input => {
    input.onchange = () => {
      selectedRows.toggleRow(input.dataset.id, input.dataset.group, input.checked);
      renderSelection();
    };
  });
  renderSelection();
}
function rowFields(row) {
  const values = state.kind === "trajectory" ? {
    agent: row.agent || "—",
    model: row.model || "—",
    duration_seconds: formatDuration(row.duration_seconds),
    input_tokens: formatTokens(row.input_tokens),
    output_tokens: formatTokens(row.output_tokens),
    score: row.score ?? "—",
    agent_stop_reason: row.agent_stop_reason || "—",
    errors: row.error_count ? `${row.error_count} 条错误` : "—",
  } : {
    backend: row.backend,
    created_by: row.created_by,
    created_at: date(row.created_at),
    updated_by: row.updated_by,
    updated_at: date(row.updated_at),
  };
  return Object.entries(values).map(([key, value]) =>
    `<td data-column="${key}" title="${esc(value)}">${key === "errors" && row.error_count ? `<button class="text-button" data-errors="${esc(row.id)}">${esc(value)}</button>` : key === "backend" ? `<span class="backend">${esc(value)}</span>` : esc(value)}</td>`
  ).join("");
}
function dateBoundary(id, end = false) {
  const value = $("#" + id).value;
  if (!value) return "";
  const date = new Date(`${value}T00:00:00`);
  if (end) {
    date.setDate(date.getDate() + 1);
    date.setMilliseconds(-1);
  }
  return date.toISOString();
}
function selection() {
  let metadata = {};
  try {
    metadata = JSON.parse($("#metadata-filter").value || "{}");
  } catch {
    throw Error("metadata 筛选须为 JSON 对象");
  }
  return {
    kind: state.kind,
    pool_id: poolId(),
    query: $("#query").value.trim(),
    tags: [...state.tags],
    backends: [...state.backends],
    creators: [...state.creators],
    created_after: dateBoundary("created-after-filter"),
    created_before: dateBoundary("created-before-filter", true),
    updated_after: dateBoundary("updated-after-filter"),
    updated_before: dateBoundary("updated-before-filter", true),
    metadata,
    ...(state.kind === "trajectory" ? {
      agents: [...state.agents], models: [...state.models],
      score_min: $("#score-min").value === "" ? null : Number($("#score-min").value),
      score_max: $("#score-max").value === "" ? null : Number($("#score-max").value),
      completed: $("#completed-filter").value === "" ? null : $("#completed-filter").value === "true",
      stop_reasons: $("#stop-reason").value ? [$("#stop-reason").value] : [],
    } : {}),
  };
}
function chosen() {
  if (!selectionCount()) throw Error("请先选择记录");
  return {
    ...state.filters,
    ...selectedRows.query(),
  };
}
function selectionCount() {
  return selectedRows.count(state.total);
}
function clearSelection() {
  selectedRows.clear();
  renderSelection();
}
function renderSelection() {
  groupedRows.updateSelection();
  const count = selectionCount();
  $("#selected-label").textContent = count ? ` · 已选 ${count} 条` : "";
  $("#clear-selection").hidden = !count;
  $("#operations").disabled = !count;
  $("#check-all").checked = count > 0 && count === state.total;
  $("#check-all").indeterminate = count > 0 && count < state.total;
  $("#check-all").disabled = state.total === 0;
  $("#rows")
    .querySelectorAll("input[data-id]")
    .forEach((c) => (c.checked = selectedRows.has(c.dataset.id, c.dataset.group)));
}
function renderTagFilter() {
  renderTags(
    state.kind,
    state.tagDefinitions,
    state.tags,
    () => {
      state.page = 1;
      clearSelection();
      load();
    },
    load,
  );
}
async function load() {
  const request = ++requestNumber;
  loadController?.abort();
  const controller = new AbortController();
  loadController = controller;
  renderPoolState();
  if (!poolId()) {
    loading = false;
    return;
  }
  const [sort, direction] = state.sort.split(":");
  loading = true;
  $(".table-wrap").setAttribute("aria-busy", "true");
  try {
    const filters = selection();
    const data = await api("/assets/page", {
      method: "POST",
      signal: controller.signal,
      body: {
        selection: filters,
        page: state.page,
        page_size: state.pageSize,
        sort,
        descending: direction === "desc",
        group_by: state.kind === "trajectory" ? "task" : null,
      },
    });
    const { tags, backends, creators } = data;
    if (request !== requestNumber) return;
    const pages = Math.max(1, Math.ceil((data.group_total ?? data.total) / state.pageSize));
    if (state.page > pages) {
      state.page = pages;
      return load();
    }
    if (JSON.stringify(filters) !== JSON.stringify(state.filters))
      clearSelection();
    previousIndex = data.revision;
    state.filters = filters;
    state.total = data.total;
    $("#list-error").hidden = true;
    $("#count").textContent =
      `${data.total} 条${({task: "任务", runtime: "环境", trajectory: "轨迹"})[state.kind]}`;
    const filterChanged = () => {
      state.page = 1;
      clearSelection();
      load();
    };
    renderMultiFilter(
      $("#backend-filter"),
      ["docker", "qemu", ...backends],
      state.backends,
      filterChanged,
      "全部后端",
      (value) => ({ docker: "Docker", qemu: "QEMU" })[value] || value,
    );
    renderMultiFilter(
      $("#creator-filter"),
      creators,
      state.creators,
      filterChanged,
      "全部创建者",
    );
    renderMultiFilter($("#agent-filter"), data.agents, state.agents, filterChanged, "全部 Agent");
    renderMultiFilter($("#model-filter"), data.models, state.models, filterChanged, "全部模型");
    state.tagDefinitions = tags;
    renderTagFilter();
    if (state.kind === "trajectory") {
      groupedRows.render(data.groups, {selection: filters, sort, descending: direction === "desc"}, data.revision);
      rowSignature = "";
    } else {
      groupedRows.reset();
      const signature = JSON.stringify([data.items, tags]);
      if (signature !== rowSignature) {
        rowSignature = signature;
        $("#rows").innerHTML = data.items.map(row => renderRow(row)).join("");
        bindRows();
      }
    }
    $("#empty").hidden = state.kind === "trajectory" ? data.groups.length > 0 : data.items.length > 0;
    $("#empty h2").textContent = state.total ? "当前页没有记录" : "没有找到资产";
    $("#page-info").textContent =
      `第 ${state.page} / ${Math.max(1, Math.ceil((data.group_total ?? data.total) / state.pageSize))} 页`;
    renderPagination(state.page, pages, (page) => {
      state.page = page;
      load();
    });
    renderSelection();
  } catch (e) {
    if (request !== requestNumber) return;
    $("#list-error").textContent = e.message;
    $("#list-error").hidden = false;
  } finally {
    if (request === requestNumber) {
      loading = false;
      $(".table-wrap").setAttribute("aria-busy", "false");
    }
  }
}
async function submitJob(path, body) {
  await api(path, { method: "POST", body });
  toast("操作已提交，可在“操作记录”查看状态");
  await jobs();
}
function jobContent(records) {
  const labels = {
    pending: "等待中",
    running: "进行中",
    completed: "完成",
    failed: "失败",
    partial: "部分完成",
  };
  return (
    records
      .map(
        (j) =>
          `<article class="job"><div><strong>${esc(j.kind)}</strong><span class="status">${labels[j.status]}</span><small>${esc(j.actor)} · ${date(j.created_at)}</small></div>${j.error || j.result ? `<pre>${esc(pretty(j.error || j.result))}</pre>` : ""}</article>`,
      )
      .join("") || '<p class="muted">暂无操作</p>'
  );
}
function renderJobStatus(records) {
  $("#job-dot").textContent = records.some((j) =>
    ["pending", "running"].includes(j.status),
  )
    ? "●"
    : "";
}
async function jobs(open = false) {
  const records = await api("/jobs");
  renderJobStatus(records);
  if (open)
    showModal("操作记录", `<div id="job-list">${jobContent(records)}</div>`);
}
let previousJobs = "",
  previousIndex = "";
function startPolling() {
  let timer,
    running = false;
  const poll = async () => {
    if (running) return;
    if (document.hidden || loading || groupedRows.busy()) {
      timer = setTimeout(poll, 3000);
      return;
    }
    running = true;
    const request = requestNumber;
    try {
      const [records, index] = await Promise.all([api("/jobs"), api("/index")]);
      if (request !== requestNumber) return;
      // A background refresh must not repeatedly cancel an expanding group.
      if (groupedRows.busy()) return;
      const changed = await restorePoolSelection(index.pools);
      const pool = index.pools.find((p) => p.id === poolId());
      if (pool?.error) {
        $("#list-error").textContent = pool.error;
        $("#list-error").hidden = false;
      }
      const key = JSON.stringify(records.map((j) => [j.id, j.status]));
      const list = $("#job-list");
      if (list && key !== previousJobs) list.innerHTML = jobContent(records);
      renderJobStatus(records);
      if (
        !changed &&
        ((previousJobs && key !== previousJobs) ||
          index.revision !== previousIndex)
      )
        await load();
      previousJobs = key;
      previousIndex = index.revision;
    } catch {
    } finally {
      running = false;
      timer = setTimeout(poll, 3000);
    }
  };
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      clearTimeout(timer);
      poll();
    }
  });
  timer = setTimeout(poll, 3000);
}
bind("#refresh", () => load());
bind("#jobs-open", () => jobs(true));
bind("#add-tag", () => createTag(state.kind, load));
bind("#operations", () =>
  openMenu(
    $("#operations"),
    '<button id="batch-tags">打标签</button>' + (state.kind === "trajectory" ? '<button id="export-traj">导出</button>' : '') + '<button id="delete" class="danger-text">删除</button>',
    (menu, close) => {
      const actions = {
        "batch-tags": () =>
          assignTags(chosen(), state.tagDefinitions, async () => {
            clearSelection();
            await load();
          }),
        "export-traj": async () => {
          try { const blob = await api("/trajectories/export", {method: "POST", body: chosen(), raw: true}); download(blob, "trajectories.zip"); }
          catch (error) {toast(error.message, true);}
        },
        delete: deleteDialog,
      };
      menu.querySelectorAll("button").forEach(
        (button) =>
          (button.onclick = () => {
            close();
            actions[button.id]();
          }),
      );
    },
  ),
);
function deleteDialog() {
  const selected = chosen();
  showModal(
    "删除资产",
    '<label class="check-label"><input type="checkbox" name="physical">同时删除实体' +
      (state.kind !== "runtime" ? "文件夹" : "镜像或磁盘") +
      "</label>",
    {
      submit: "查看影响",
      action: async (form) => {
        const plan = await api("/delete/plan", {
          method: "POST",
          body: { selection: selected, physical: !!form.get("physical") },
        });
        setTimeout(() => confirmDelete(plan), 0);
      },
    },
  );
}
function confirmDelete(plan) {
  const impacted = [...plan.affected_tasks, ...plan.affected_runtimes, ...plan.affected_trajectories];
  showModal(
    "确认删除",
    `<p>将删除 <strong>${plan.selected.length}</strong> 条记录${plan.physical ? "及其实体" : "，保留实体文件"}。</p><ul>${plan.selected.map((r) => `<li>${esc(r.name)}</li>`).join("")}</ul>${impacted.length ? `<div class="error"><strong>以下 ${impacted.length} 条关联资产将受影响</strong><ul>${impacted.map((r) => `<li>${esc(r.name)}</li>`).join("")}</ul></div><label class="check-label dependency-ack"><input type="checkbox" name="ack" required>我已了解关联影响，仍要删除</label>` : '<p class="muted">未发现有其他资产依赖它们。</p>'}`,
    {
      submit: "确认删除",
      action: async (form) => {
        await submitJob("/delete/confirm", {
          token: plan.token,
          acknowledge_dependencies: !!form.get("ack"),
        });
        clearSelection();
      },
    },
  );
}
$("#check-all").onchange = (e) => {
  selectedRows.clear(e.target.checked);
  renderSelection();
};
bind("#clear-selection", clearSelection);
$("#more-filters").onclick = () =>
  ($("#advanced").hidden = !$("#advanced").hidden);
bind("#apply-filters", () => {
  state.page = 1;
  clearSelection();
  return load();
});
let debounce;
$("#query").oninput = () => {
  clearTimeout(debounce);
  debounce = setTimeout(() => {
    state.page = 1;
    clearSelection();
    load();
  }, 250);
};
$("#tag-search").oninput = renderTagFilter;
renderSingleFilter(
  $("#sort"),
  [
    ["updated_at:desc", "最近修改"],
    ["created_at:desc", "最近创建"],
    ["name:asc", "名称 A → Z"],
  ],
  state.sort,
  (value) => {
    state.sort = value;
    state.page = 1;
    clearSelection();
    load();
  },
);
$("#page-size").onchange = () => {
  state.page = 1;
  state.pageSize = Number($("#page-size").value);
  clearSelection();
  load();
};
function renderActiveTab() {
  document
    .querySelectorAll("[data-kind]")
    .forEach((button) =>
      button.classList.toggle("active", button.dataset.kind === state.kind),
    );
  $("#page-title").textContent =
    ({task: "Task Pool", runtime: "Runtime Pool", trajectory: "Traj Pool"})[state.kind];
  $("#query").placeholder =
    state.kind === "runtime" ? "搜索环境名称…" : "搜索任务名称…";
}
document.querySelectorAll("[data-kind]").forEach((button) => {
  button.onclick = () => {
    state.kind = button.dataset.kind;
    writeSession("activeTab", state.kind);
    renderActiveTab();
    $("#query").value = "";
    switchPool(state.pools[state.kind]);
  };
});
function renderPoolPath() {
  const path = state.pools[state.kind]?.address || "";
  $("#pool-path").textContent = path;
  $("#pool-path").title = path;
  $("#pool-path").hidden = !path;
}
async function switchPool(pool) {
  const id = pool?.id || null;
  state.pools[state.kind] = pool;
  ++requestNumber;
  groupedRows.reset();
  setPool(id);
  state.tags.clear();
  state.backends.clear();
  state.agents.clear();
  state.models.clear();
  for (const id of ["score-min", "score-max", "completed-filter", "stop-reason"]) $("#" + id).value = "";
  state.creators.clear();
  state.tagDefinitions = [];
  $("#tag-list").replaceChildren();
  state.page = 1;
  state.total = 0;
  rowSignature = "";
  $("#rows").replaceChildren();
  $("#count").textContent = id ? "加载中…" : "";
  clearSelection();
  previousIndex = "";
  renderPoolPath();
  await load();
}
bind("#choose-pool", () =>
  showModal(
    "切换 Pool 目录",
    field("目录", "address", state.pools[state.kind]?.address || "", {
      required: true,
      placeholder: "/data/task-pool 或 ssh://user@server/data/task-pool",
    }),
    {
      submit: "打开",
      action: async (form) => {
        const pool = await api("/pools", {
          method: "POST",
          body: {
            kind: state.kind,
            address: form.get("address"),
          },
        });
        await switchPool(pool);
      },
    },
  ),
);
function renderPoolState() {
  selectTableColumns(state.kind);
  const traj = state.kind === "trajectory";
  $("#backend-filter").hidden = traj;
  $("#page-size").setAttribute("aria-label", traj ? "每页 Task 数" : "每页数量");
  for (const node of document.querySelectorAll("#agent-filter, #model-filter, .traj-filter")) node.hidden = !traj;
  const selected = !!poolId();
  $("#pool-empty").hidden = selected;
  $("#workspace > aside").hidden = !selected;
  $("#pool-empty-title").textContent =
    `选择 ${({task: "Task", runtime: "Runtime", trajectory: "Traj"})[state.kind]} Pool 目录`;
  for (const selector of [
    ".filterbar",
    ".selectionbar",
    ".table-wrap",
    ".pagination",
    "#refresh",
  ])
    $(selector).hidden = !selected;
  for (const selector of ["#add-tag", "#tag-search"])
    $(selector).disabled = !selected;
  if (!selected) {
    $("#advanced").hidden = true;
    $("#list-error").hidden = true;
    $("#tag-list").replaceChildren();
    $("#rows").replaceChildren();
    rowSignature = "";
    state.total = 0;
    clearSelection();
  }
}
bind("#open-pool", () => $("#choose-pool").click());
async function restorePoolSelection(pools) {
  const current = poolId();
  for (const kind of ["task", "runtime", "trajectory"]) {
    const pool = pools.find((p) => p.kind === kind && p.selected) || null;
    state.pools[kind] = pool;
  }
  if (current !== poolId()) {
    await switchPool(state.pools[state.kind]);
    return true;
  }
  return false;
}
async function initialize() {
  renderActiveTab();
  renderPoolState();
  await restorePoolSelection(await api("/pools"));
  renderPoolPath();
  if (poolId()) await restoreDetail();
  else writeSession("detail", null);
  startPolling();
}
initialize().catch((error) => toast(error.message, true));
