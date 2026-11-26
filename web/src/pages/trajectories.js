import { api } from "../api/client.js";
import { esc, date, formatDuration, download, showModal, toast } from "../components/ui.js";
import { renderTrajectory } from "../components/trajectory-view.js";
import { showImage } from "../components/image-viewer.js";

export function trajectorySummary(row) {
  const result = row.spec;
  const info = result.agent_result?.metadata || {};
  const config = result.configuration?.agent || {};
  const agent = info.agent || config.config?.name || config.class?.split(".").pop() || "—";
  const model = info.model || config.config?.model || "—";
  const task = row.relations.find(link => link.role === "task")?.asset;
  const taskName = task ? task.name + (task.deleted ? " · 记录已删除" : task.state === "missing" ? " · 实体缺失" : "") : result.name;
  const taskLink = task
    ? `<button class="task-reference" data-related="${esc(task.id)}" title="${esc(result.task_ref || "")}" ${task.deleted ? "disabled" : ""}>${esc(taskName)} ↗</button>`
    : `<span title="${esc(result.task_ref || "")}">${esc(taskName || "—")}</span>`;
  return `<dl class="facts trajectory-facts"><dt>关联 Task</dt><dd>${taskLink}</dd><dt>Agent</dt><dd>${esc(agent)}</dd><dt>模型</dt><dd>${esc(model)}</dd><dt>得分</dt><dd>${esc(result.score_valid ? result.verification_result?.score : "—")}</dd><dt>退出原因</dt><dd>${esc(result.agent_stop_reason || "—")}</dd><dt>开始时间</dt><dd>${date(result.started_at)}</dd><dt>结束时间</dt><dd>${result.ended_at ? date(result.ended_at) : "—"}</dd><dt>耗时</dt><dd>${formatDuration(result.duration_seconds)}</dd><dt>轨迹统计</dt><dd id="trajectory-metrics" class="trajectory-metrics">加载中…</dd></dl><section><h3>ATIF 轨迹</h3><div id="trajectory-content">加载中…</div></section>`;
}

function fileBlob(row, path, signal) {
  return api(`/trajectories/${row.id}/file?path=${encodeURIComponent(path)}&pool_id=${encodeURIComponent(row.pool_id)}`, {raw: true, signal});
}

function render(row, target, value, signal, metricsTarget = null, path = null) {
  target.replaceChildren();
  target.classList.add("trajectory-reader");
  const visited = new Set(path ? [fileKey(path)] : []);
  const options = {
    openEmbedded: (child, title) => trajectoryDialog(row, title, async () => child),
    openFile: (file, trajectory) => openTrajectoryFile(row, file, {trajectory, signal}),
    openContent: (title, text) => {
      const modal = showModal(title, '<div class="trajectory-text"></div>', {wide: true});
      modal.querySelector(".trajectory-text").textContent = text;
    },
    mediaURL: file => `/api/trajectories/${row.id}/file?path=${encodeURIComponent(file)}&pool_id=${encodeURIComponent(row.pool_id)}`,
    onError: error => { if (!signal.aborted) toast(error.message, true); },
  };
  function append(trajectory, continuation = false, startIndex = 0) {
    const segment = document.createElement("div");
    target.append(segment);
    let nextIndex;
    try {
      nextIndex = renderTrajectory(segment, trajectory, {...options, metricsTarget, continuation, startIndex});
    } catch (error) { segment.remove(); throw error; }
    const next = trajectory.continued_trajectory_ref;
    if (!next) return;
    const file = "agent/" + next;
    const pending = document.createElement("div");
    pending.className = "trace-continuation";
    target.append(pending);
    if (visited.has(fileKey(file))) { pending.textContent = "续接轨迹存在循环引用"; return; }
    pending.textContent = "加载后续步骤…";
    async function load() {
      pending.textContent = "加载后续步骤…";
      try {
        const blob = await fileBlob(row, file, signal);
        const document = JSON.parse(await blob.text());
        if (signal.aborted) return;
        visited.add(fileKey(file));
        try { append(document, true, nextIndex); }
        catch (error) { visited.delete(fileKey(file)); throw error; }
        pending.remove();
      } catch (error) {
        if (signal.aborted) return;
        pending.innerHTML = `<span role="alert">${esc(error.message)}</span> <button class="text-button">重试</button>`;
        pending.querySelector("button").onclick = load;
      }
    }
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) { cleanup(); load(); }
    }, {root: target, rootMargin: "200px"});
    function cleanup() { observer.disconnect(); signal.removeEventListener("abort", cleanup); }
    signal.addEventListener("abort", cleanup, {once: true});
    observer.observe(pending);
  }
  append(value);
}

function fileKey(path) {
  return new URL(path, "https://trajectory.invalid/").pathname;
}

function trajectoryDialog(row, title, load, path = null) {
  const dialog = document.createElement("dialog");
  dialog.className = "trajectory-viewer";
  dialog.setAttribute("aria-label", title);
  dialog.innerHTML = `<div class="dialog-heading"><h2>${esc(title)}</h2><button class="quiet" aria-label="关闭轨迹">×</button></div><div class="trajectory-body">加载中…</div>`;
  const controller = new AbortController();
  dialog.querySelector(".quiet").onclick = () => dialog.close();
  dialog.addEventListener("close", () => {
    controller.abort();
    dialog.remove();
  }, {once: true});
  document.body.append(dialog);
  dialog.showModal();
  const target = dialog.querySelector(".trajectory-body");
  async function refresh() {
    target.textContent = "加载中…";
    try {
      const value = await load(controller.signal);
      if (!controller.signal.aborted) render(row, target, value, controller.signal, null, path);
    } catch (error) {
      if (controller.signal.aborted) return;
      target.innerHTML = `<p role="alert">${esc(error.message)}</p><button class="text-button">重试</button>`;
      target.querySelector("button").onclick = refresh;
    }
  }
  refresh();
}

export async function loadTrajectory(row, dialog, signal) {
  const target = dialog.querySelector("#trajectory-content");
  const metrics = dialog.querySelector("#trajectory-metrics");
  const path = row.spec.agent_result?.artifacts?.trajectory;
  if (!path) { target.textContent = "没有已保存的轨迹"; metrics.textContent = "—"; return; }
  try {
    const blob = await fileBlob(row, "agent/" + path, signal);
    const value = JSON.parse(await blob.text());
    if (!signal.aborted) render(row, target, value, signal, metrics, "agent/" + path);
  } catch (error) {
    if (!signal.aborted) {metrics.textContent = "加载失败"; target.textContent = "轨迹加载失败"; toast(error.message, true);}
  }
}

export async function openTrajectoryFile(row, path, {trajectory = false, signal} = {}) {
  if (trajectory) {
    trajectoryDialog(row, path, async childSignal => {
      const blob = await fileBlob(row, path, childSignal);
      return JSON.parse(await blob.text());
    }, path);
    return;
  }
  const blob = await fileBlob(row, path, signal);
  if (signal?.aborted) return;
  if (/\.(png|jpe?g|webp|gif)$/i.test(path)) {
    const img = new Image();
    const url = URL.createObjectURL(blob);
    img.src = url;
    img.alt = path;
    try {
      await img.decode();
      if (signal?.aborted) { URL.revokeObjectURL(url); return; }
      const dialog = showImage(img);
      dialog.addEventListener("close", () => URL.revokeObjectURL(url), {once: true});
    } catch (error) {
      URL.revokeObjectURL(url);
      throw error;
    }
    return;
  }
  if (blob.size < 2 * 1024 * 1024 && /\.(json|jsonl|txt|log|md)$/i.test(path)) {
    const text = await blob.text();
    if (!signal?.aborted) {
      const dialog = showModal(path, "<pre><code></code></pre>", {wide: true});
      dialog.querySelector("pre code").textContent = text;
    }
    return;
  }
  download(blob, path.split("/").pop());
}
