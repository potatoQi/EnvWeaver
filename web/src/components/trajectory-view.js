import { esc } from "./ui.js";
import { renderJson } from "./json.js";

function isTrajectory(value) {
  return value && typeof value === "object" && Array.isArray(value.steps)
    && value.agent && typeof value.schema_version === "string" && value.schema_version.startsWith("ATIF-");
}

// Render only recorded execution. Request-context snapshots stay in the artifact viewer.
export function renderTrajectory(target, trajectory, {
  openEmbedded, openFile, openContent, mediaURL, onError,
  metricsTarget = null, continuation = false, startIndex = 0,
}) {
  if (!isTrajectory(trajectory)) throw new Error("文件不是有效的 ATIF 轨迹");
  const children = trajectory.subagent_trajectories || [];
  const childrenById = new Map(children.map(child => [child.trajectory_id, child]));
  const referenced = new Set();
  const actions = [];
  function link(label, action, path) {
    const index = actions.push(action) - 1;
    return `<button class="text-button" data-trajectory-action="${index}"${path ? ` data-traj-file="${esc("agent/" + path)}"` : ""}>${esc(label)}</button>`;
  }
  function childLink(child, label = `子 Agent · ${child.agent?.name || "Agent"}`) {
    referenced.add(child);
    return link(label, () => openEmbedded(child, label));
  }
  function references(refs, compaction = false) {
    return (refs || []).map((ref, index) => {
      const child = childrenById.get(ref.trajectory_id);
      const label = compaction ? `压缩过程 ${index + 1}` : undefined;
      if (child) return childLink(child, label);
      if (ref.trajectory_path) return link(label || "子 Agent", () => openFile("agent/" + ref.trajectory_path, true), ref.trajectory_path);
      return `<span class="danger-text">未找到子轨迹：${esc(ref.trajectory_id || ref.session_id)}</span>`;
    }).join("");
  }
  function content(value, trimLeading = false) {
    if (value == null) return "";
    if (typeof value === "string") {
      const text = trimLeading ? value.replace(/^(?:[ \t]*\r?\n)+/, "") : value;
      return `<div class="trajectory-text">${esc(text)}</div>`;
    }
    if (!Array.isArray(value)) return renderJson(value);
    return value.map((part, index) => {
      if (part.type === "text") return content(part.text, trimLeading && index === 0);
      const path = part.source?.path;
      if (!path) return renderJson(part);
      if (part.type === "image") {
        const index = actions.push(() => openFile("agent/" + path, false)) - 1;
        return `<button class="trajectory-image" data-trajectory-action="${index}" aria-label="查看图片 ${esc(path)}"><img src="${esc(mediaURL("agent/" + path))}" alt="${esc(path)}" loading="lazy"></button>`;
      }
      return link(`${part.type} · ${path}`, () => openFile("agent/" + path, false), path);
    }).join("");
  }
  function card(number, role, type, body, extra = "") {
    return `<article class="trace-card"><div class="trace-card-heading"><span>#${number}</span><span>${esc(role)}</span><span class="trace-kind">${esc(type)}</span>${extra}</div><div class="trace-card-body">${body}</div></article>`;
  }
  const metrics = trajectory.final_metrics == null ? "—" : renderJson(trajectory.final_metrics);
  if (metricsTarget) metricsTarget.innerHTML = metrics;
  const roles = {system: "SYSTEM", user: "USER", agent: "AGENT"};
  let index = startIndex;
  let encryptedContext = false;
  const steps = trajectory.steps.filter(step => !continuation || !step.is_copied_context);
  const html = steps.map(step => {
    const number = ++index;
    const results = step.observation?.results || [];
    const extra = step.extra || {};
    const compact = extra.context_management?.type === "compaction" || extra.pi_compaction != null;
    const encrypted = extra.context?.missing?.includes("encrypted_compaction");
    let body = "";
    if (encrypted && !encryptedContext) body += card(number, "SYSTEM", "上下文压缩", "");
    encryptedContext = Boolean(encrypted);
    if (compact) {
      const refs = results.flatMap(result => result.subagent_trajectory_ref || []);
      const summary = extra.pi_compaction?.compactionEntry?.summary || extra.pi_compaction?.summary
        || results.map(result => typeof result.content === "string" ? result.content : "").filter(Boolean).join("\n\n");
      body += card(number, "SYSTEM", "上下文压缩", `<div class="trajectory-links">${references(refs, true)}${summary ? link("查看摘要", () => openContent("压缩摘要", summary)) : ""}</div>`);
    } else {
      if (step.reasoning_content) body += card(number, roles[step.source] || step.source, "REASONING", content(step.reasoning_content));
      if (step.message?.length) body += card(number, roles[step.source] || step.source, "MESSAGE", content(step.message, true));
      for (const call of step.tool_calls || []) {
        body += card(number, "AGENT", call.function_name, renderJson(call.arguments), `<span class="trace-call-id">${esc(call.tool_call_id)}</span>`);
      }
      for (const result of results) {
        body += card(number, "ENVIRONMENT", "RESULT", content(result.content) + `<div class="trajectory-links">${references(result.subagent_trajectory_ref)}</div>`, result.source_call_id ? `<span class="trace-call-id">${esc(result.source_call_id)}</span>` : "");
      }
      if (!body) body = card(number, roles[step.source] || step.source, "MESSAGE", "");
    }
    return `<div class="trajectory-step" data-step="${esc(step.step_id)}">${body}</div>`;
  }).join("");
  // Preserve access when a producer saved a child without its call-site reference.
  const orphans = children.filter(child => !referenced.has(child));
  target.innerHTML = (metricsTarget || continuation ? "" : `<dl class="facts"><dt>汇总指标</dt><dd class="trajectory-metrics">${metrics}</dd></dl>`)
    + html + (steps.length ? "" : '<p class="muted">暂无步骤</p>')
    + (orphans.length ? `<details><summary>未记录调用位置的子 Agent</summary><div class="trajectory-links">${orphans.map(child => childLink(child)).join("")}</div></details>` : "");
  target.onclick = async event => {
    const button = event.target.closest("[data-trajectory-action]");
    if (!button || button.disabled) return;
    button.disabled = true;
    try { await actions[Number(button.dataset.trajectoryAction)](); }
    catch (error) { onError(error); }
    finally { button.disabled = false; }
  };
  return index;
}
