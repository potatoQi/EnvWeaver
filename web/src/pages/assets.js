import { trajectorySummary, loadTrajectory, openTrajectoryFile } from "./trajectories.js";
import { readSession, writeSession } from "../components/session.js";
import { renderJson } from "../components/json.js";
import { setupDetailPanel } from "../components/detail-panel.js";
import { showImage } from "../components/image-viewer.js";
import { tagChip } from "../components/tags.js";
import { api } from "../api/client.js";
import {
  $,
  esc,
  pretty,
  formatSize,
  date,
  download,
  field,
  tagList,
  showModal,
  toast,
} from "../components/ui.js";
let refresh = () => {};
let blobUrls = [];
let detailHistory = [];
let currentAsset = null;
let detailController;
function detailView() {
  const dialog = $("#detail");
  return {
    id: currentAsset,
    scrollTop: dialog.scrollTop,
    jsonOpen: !!dialog.querySelector("#asset-json[open]"),
  };
}
function rememberDetail() {
  writeSession(
    "detail",
    $("#detail").open && currentAsset
      ? { ...detailView(), history: detailHistory }
      : null,
  );
}
export function configure(callback) {
  refresh = callback;
  setupDetailPanel($("#detail"));
  window.addEventListener("pagehide", rememberDetail);
}
export async function restoreDetail() {
  const saved = readSession("detail");
  const validView = (view) =>
    view &&
    typeof view.id === "string" &&
    Number.isFinite(view.scrollTop) &&
    view.scrollTop >= 0;
  if (
    !validView(saved) ||
    !Array.isArray(saved.history) ||
    !saved.history.every(validView)
  ) {
    writeSession("detail", null);
    return;
  }
  try {
    await showAsset(saved.id, saved.history, saved);
  } catch (error) {
    writeSession("detail", null);
    toast(`无法恢复上次详情：${error.message}`, true);
  }
}
export async function openAsset(id, navigation = "root") {
  const history = navigation === "root" ? [] : [...detailHistory];
  let view = {};
  if (navigation === "forward" && currentAsset) {
    history.push(detailView());
  } else if (navigation === "back") {
    view = history.pop();
    if (!view) return;
    id = view.id;
  } else if (navigation === "refresh") {
    view = detailView();
  }
  return showAsset(id, history, view);
}
async function showAsset(id, history, view) {
  const dialog = $("#detail");
  detailController?.abort();
  const controller = new AbortController();
  detailController = controller;
  const active = () => !controller.signal.aborted;
  const scrollTop = view.scrollTop || 0;
  let row;
  try {
    row = await api(`/assets/${id}`, { signal: controller.signal });
  } catch (error) {
    if (!active()) return;
    throw error;
  }
  if (!active()) return;
  const tags = row.tag_definitions;
  detailHistory = history;
  currentAsset = id;
  dialog.onclose = () => {
    detailController?.abort();
    detailHistory = [];
    currentAsset = null;
    writeSession("detail", null);
    blobUrls.forEach(URL.revokeObjectURL);
    blobUrls = [];
  };
  blobUrls.forEach(URL.revokeObjectURL);
  blobUrls = [];
  const task = row.kind === "task";
  const trajectory = row.kind === "trajectory";
  const visibleRelations = trajectory ? [] : row.relations.filter(link => link.role !== "trajectory");
  const closeButton =
    '<button class="quiet" id="detail-close" aria-label="关闭详情"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6"/></svg></button>';
  const relations = trajectory ? "" : `<section class="asset-relations"><h3>${task ? "关联资产" : "引用该环境的 Task"}</h3><div class="relations">${visibleRelations.map((link) => `<button data-related="${esc(link.asset.id)}" ${link.asset.deleted ? "disabled" : ""}><span>${esc(link.asset.name)}${link.asset.deleted ? " · 记录已删除" : link.asset.state === "missing" ? " · 实体缺失" : ""}</span><small>${esc(({task_runtime_ref: "做题环境", verifier_runtime_ref: "验证环境"})[link.role])} ↗</small></button>`).join("") || '<p class="muted">暂无关联</p>'}</div></section>`;
  const overview = trajectory ? "" : `<section><h3>${task ? "任务说明" : "环境配置"}</h3><div id="goal">${task ? row.spec.goal.map((p) => (p.type === "text" ? `<p class="goal-text">${esc(p.text)}</p>` : `<img class="goal-image" data-path="${esc(p.path)}" alt="任务图片">`)).join("") : renderJson(row.spec)}</div></section>`;
  dialog.innerHTML = `<button class="detail-resize-handle" aria-label="调整详情面板宽度" title="拖动调整宽度"></button>${history.length ? `<div class="detail-nav"><button id="detail-back" class="quiet" aria-label="返回上一条详情"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 5-7 7 7 7M5 12h14"/></svg><span>返回</span></button>${closeButton}</div>` : ""}<div class="dialog-heading"><div class="detail-title"><h2>${esc(trajectory ? row.spec.name : row.name)}</h2>${!trajectory && row.pool_id ? '<button id="edit-asset" class="primary">编辑</button>' : ""}</div>${history.length ? "" : closeButton}</div><div class="detail-content"><div class="chips">${row.tags.map((t) => tagChip(t, tags)).join("") || '<span class="muted">暂无标签</span>'}</div><dl class="facts${trajectory ? " trajectory-facts" : ""}"><dt>记录 ID</dt><dd>${esc(row.id)}</dd><dt>创建者</dt><dd>${esc(row.created_by)}</dd><dt>创建时间</dt><dd>${date(row.created_at)}</dd><dt>最后修改者</dt><dd>${esc(row.updated_by)}</dd><dt>最后修改时间</dt><dd>${date(row.updated_at)}</dd></dl>${row.last_error ? `<p class="error">${esc(row.last_error)}</p>` : ""}${trajectory ? trajectorySummary(row) : ""}${row.kind === "runtime" ? relations : ""}${overview}${task ? relations : ""}${task || trajectory ? `<section><h3>${task ? "Task文件" : "产物文件"}</h3><div id="files"${trajectory ? ' class="artifact-files" tabindex="0" aria-label="产物文件"' : ""}>加载中…</div></section>` : ""}${task ? `<details id="asset-json"><summary>task.json</summary></details>` : ""}</div>`;
  const back = dialog.querySelector("#detail-back");
  if (back)
    back.onclick = () =>
      openAsset(null, "back").catch((e) => toast(e.message, true));
  dialog.querySelector("#detail-close").onclick = () => dialog.close();
  dialog
    .querySelectorAll("[data-related]")
    .forEach(
      (b) =>
        (b.onclick = () =>
          openAsset(b.dataset.related, "forward").catch((e) =>
            toast(e.message, true),
          )),
    );
  const edit = dialog.querySelector("#edit-asset");
  if (edit) edit.onclick = () => editAsset(row);
  const json = dialog.querySelector("#asset-json");
  if (json) {
    const render = () => {
      if (json.open && !json.querySelector(".json-view"))
        json.insertAdjacentHTML("beforeend", renderJson(row.spec));
    };
    json.ontoggle = render;
    json.querySelector("summary").onclick = (event) => {
      event.preventDefault();
      json.open = !json.open;
      render();
    };
    json.open = !!view.jsonOpen;
    render();
  }
  if (!dialog.open) dialog.showModal();
  dialog.scrollTop = scrollTop;
  rememberDetail();
  if (trajectory) {
    await Promise.all([loadTrajectory(row, dialog, controller.signal), loadFiles(row, dialog, controller.signal, active, scrollTop)]);
  }
  if (task) {
    await Promise.all([
      loadFiles(row, dialog, controller.signal, active, scrollTop),
      loadImages(row, dialog, controller.signal, active, scrollTop),
    ]);
  }
}

async function loadFiles(row, dialog, signal, active, scrollTop) {
  try {
    const files = await api(`/${row.kind === "trajectory" ? "trajectories" : "tasks"}/${row.id}/files`, { signal });
    if (!active()) return;
    const container = dialog.querySelector("#files");
    container.innerHTML = files
      .map(
        (f) =>
          `<button class="file-row" data-file="${esc(f.path)}"><span>${esc(f.path)}</span><small>${formatSize(f.size)}</small></button>`,
      )
      .join("");
    container.querySelectorAll("[data-file]").forEach((button) => {
      button.onclick = () =>
        openFile(row, button.dataset.file).catch((e) => toast(e.message, true));
    });
    if (scrollTop) dialog.scrollTop = scrollTop;
  } catch (error) {
    if (!active()) return;
    dialog.querySelector("#files").textContent = "文件列表加载失败";
    toast(error.message, true);
  }
}

async function loadImages(row, dialog, signal, active, scrollTop) {
  const images = [...dialog.querySelectorAll("[data-path]")];
  const worker = async () => {
    while (images.length && active()) {
      const img = images.shift();
      if (!/\.(png|jpe?g|webp|gif)$/i.test(img.dataset.path)) continue;
      try {
        const blob = await api(
          `/tasks/${row.id}/file?path=${encodeURIComponent(img.dataset.path)}`,
          { raw: true, signal },
        );
        if (!active()) return;
        const url = URL.createObjectURL(blob);
        blobUrls.push(url);
        img.onload = () => {
          if (!active()) return;
          if (scrollTop) dialog.scrollTop = scrollTop;
          img.tabIndex = 0;
          img.setAttribute("role", "button");
          img.setAttribute("aria-label", "放大任务图片");
          img.onclick = () => showImage(img);
          img.onkeydown = (event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              showImage(img);
            }
          };
        };
        img.src = url;
      } catch {
        if (active()) img.alt = "图片暂时无法读取";
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(4, images.length) }, worker));
}

function editAsset(row) {
  const task = row.kind === "task";
  showModal(
    "编辑资产",
    field("名称", "name", row.name, { required: true }) +
      field("标签（逗号分隔）", "tags", row.tags.join(", ")) +
      field(task ? "Task JSON" : "RuntimeRef JSON", "spec", pretty(row.spec), {
        rows: 16,
      }),
    {
      wide: true,
      action: async (form) => {
        const body = {
          revision: row.revision,
          name: form.get("name"),
        };
        if (!task && body.name === row.name) delete body.name;
        const tags = tagList(form.get("tags"));
        if (JSON.stringify(tags) !== JSON.stringify(row.tags)) body.tags = tags;
        body[task ? "manifest" : "ref"] = JSON.parse(form.get("spec"));
        await api(`/assets/${row.id}`, { method: "PATCH", body });
        toast("已保存");
        await refresh();
        await openAsset(row.id, "refresh");
      },
    },
  );
}
async function openFile(row, path) {
  if (row.kind === "trajectory") return openTrajectoryFile(row, path);
  const blob = await api(
    `/tasks/${row.id}/file?path=${encodeURIComponent(path)}`,
    { raw: true },
  );
  if (
    blob.size < 1024 * 1024 &&
    /\.(py|json|txt|md|sh|yaml|yml|toml|csv|html|js|css|xml|ini)$/i.test(path)
  ) {
    fileEditor(row, path, await blob.text());
  } else {
    download(blob, path.split("/").pop());
  }
}
function fileEditor(row, path, content) {
  const readonly = path === "task.json";
  const dialog = showModal(
    readonly ? "task.json · 通过“编辑资产”修改" : "编辑文件",
    `<p>${esc(path)}</p>` +
      field("文字内容", "content", content, {
        rows: 15,
        readonly,
      }) +
      (!readonly
        ? '<button type="button" id="remove-file" class="danger-text">删除此文件</button>'
        : ""),
    {
      wide: true,
      submit: "保存文件",
      action: readonly
        ? undefined
        : async (form) => {
            const bytes = new TextEncoder().encode(form.get("content"));
            let binary = "";
            for (let i = 0; i < bytes.length; i += 8192)
              binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
            await api(
              `/tasks/${row.id}/file?path=${encodeURIComponent(path)}`,
              {
                method: "PUT",
                body: { revision: row.revision, content_base64: btoa(binary) },
              },
            );
            toast("文件已保存");
            await refresh();
            await openAsset(row.id, "refresh");
          },
    },
  );
  const remove = dialog.querySelector("#remove-file");
  if (remove)
    remove.onclick = async () => {
      if (!confirm(`删除材料 ${path}？`)) return;
      try {
        await api(
          `/tasks/${row.id}/file?path=${encodeURIComponent(path)}&revision=${row.revision}`,
          { method: "DELETE" },
        );
        dialog.close();
        await openAsset(row.id, "refresh");
        await refresh();
      } catch (e) {
        toast(e.message, true);
      }
    };
}
