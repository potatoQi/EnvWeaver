import { api } from "../api/client.js";
import { esc } from "./ui.js";

// Group pages contain Task headers. Rollout rows are fetched only when expanded.
export function taskGroups(container, renderRow, bindRows, selection) {
  const expanded = new Set();
  let requests = [];
  let signature = "";
  let scope = "";
  let generation = 0;
  let groupChecks = [];

  function updateSelection() {
    for (const [input, group] of groupChecks) {
      const count = selection.count(group);
      input.checked = count > 0 && count === group.count;
      input.indeterminate = count > 0 && count < group.count;
    }
  }

  function cancel() {
    generation += 1;
    requests.forEach(controller => controller.abort());
    requests = [];
  }

  function reset() {
    cancel();
    expanded.clear();
    groupChecks = [];
    signature = scope = "";
  }

  function render(groups, query, revision) {
    const nextScope = JSON.stringify(query);
    const nextSignature = JSON.stringify([groups, nextScope, revision]);
    if (signature === nextSignature) return;
    cancel();
    if (scope !== nextScope) expanded.clear();
    scope = nextScope;
    signature = nextSignature;
    const current = generation;
    const columnCount = container.closest("table").querySelectorAll("thead th").length;
    container.innerHTML = groups.map(group => `
      <tr class="task-group"><td class="check"><input type="checkbox" data-group-select="${esc(group.key)}" aria-label="选择 ${esc(group.name)} 的全部轨迹"></td><td colspan="${columnCount - 1}"><button class="task-group-toggle" aria-expanded="false" title="${esc(group.task_ref || "未记录 Task 路径")}">
        <span class="task-group-arrow" aria-hidden="true">▸</span><strong>${esc(group.name)}</strong><span class="task-group-count">${group.count} 条轨迹</span><span class="task-group-path">${esc(group.task_ref || "")}</span>
      </button></td></tr><tr class="task-group-more" hidden><td colspan="${columnCount}"></td></tr>`).join("");

    groupChecks = [];
    [...container.querySelectorAll(".task-group")].forEach((header, index) => {
      const group = groups[index];
      const input = header.querySelector("[data-group-select]");
      groupChecks.push([input, group]);
      input.onchange = () => selection.toggle(group, input.checked);
      const footer = header.nextElementSibling;
      const button = header.querySelector("button");
      const rows = [];
      let open = expanded.has(group.key);
      let page = 1;
      let total = group.count;
      let loading = false;
      let error = "";

      function update() {
        button.setAttribute("aria-expanded", String(open));
        header.querySelector(".task-group-arrow").textContent = open ? "▾" : "▸";
        rows.forEach(row => row.hidden = !open);
        footer.hidden = !open || (!loading && !error && rows.length >= total);
        footer.firstElementChild.innerHTML = loading ? "加载中…" : error
          ? `<span class="danger-text">${esc(error)}</span> <button class="text-button">重试</button>`
          : `<button class="text-button">加载更多（${rows.length} / ${total}）</button>`;
        const more = footer.querySelector("button");
        if (more) more.onclick = loadMore;
      }

      async function loadMore() {
        if (loading) return;
        loading = true;
        error = "";
        update();
        const controller = new AbortController();
        requests.push(controller);
        try {
          const data = await api("/assets/page", {
            method: "POST", signal: controller.signal,
            body: {...query, selection: {...query.selection, task_group: group.key}, page, page_size: 30},
          });
          if (generation !== current || !header.isConnected) return;
          total = data.total;
          const fragment = document.createElement("tbody");
          fragment.innerHTML = data.items.map(row => renderRow(row, group.key)).join("");
          const added = [...fragment.children];
          rows.push(...added);
          footer.before(...added);
          page += 1;
          bindRows();
        } catch (failure) {
          if (generation !== current || controller.signal.aborted) return;
          error = failure.message;
        } finally {
          requests = requests.filter(item => item !== controller);
          loading = false;
          if (generation === current) update();
        }
      }

      button.onclick = () => {
        open = !open;
        if (open) expanded.add(group.key);
        else expanded.delete(group.key);
        update();
        if (open && !rows.length) loadMore();
      };
      update();
      if (open) loadMore();
    });
  }

  return {render, reset, updateSelection, busy: () => requests.length > 0};
}
