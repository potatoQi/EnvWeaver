import { enableHorizontalScroll } from "./ui.js";

export function setupTableColumns(table) {
  const container = table.parentElement;
  let columns = [...table.querySelectorAll("col")];
  let headers = [...table.querySelectorAll("th")];
  const base = columns.map((column, i) => [column.dataset.column, headers[i].textContent.trim(), Number(column.dataset.width), Number(column.dataset.min)]);
  const trajectory = [base[0], ["name", "Rollout", 150, 140], ["tags", "标签", 160, 120],
    ["agent", "Agent", 95, 80], ["model", "模型", 130, 100],
    ["duration_seconds", "耗时", 90, 80], ["input_tokens", "输入 Token", 105, 100],
    ["output_tokens", "输出 Token", 105, 100], ["score", "得分", 65, 65],
    ["agent_stop_reason", "退出原因", 110, 100], ["errors", "Errors", 100, 90]];
  let kind;
  let widths = {};
  let drag = null;
  const key = () => `envweaver.tableWidths.${kind}`;
  const clamp = (column, value) =>
    Math.round(Math.max(Number(column.dataset.min), Math.min(2400, value)));
  const apply = () => {
    const sizes = columns.map(
      column => widths[column.dataset.column] ?? Number(column.dataset.width),
    );
    const extra = Math.max(
      0,
      container.clientWidth - sizes.reduce((a, b) => a + b, 0),
    );
    const flexible = columns
      .map((column, i) => i)
      .filter(
        (i) =>
          ["name", "tags"].includes(columns[i].dataset.column) &&
          widths[columns[i].dataset.column] === undefined,
      );
    flexible.forEach((i) => {
      sizes[i] += extra / flexible.length;
    });
    columns.forEach((column, i) => {
      column.style.width = `${sizes[i]}px`;
    });
    table.style.width = `${sizes.reduce((a, b) => a + b, 0)}px`;
  };
  const snapshot = () =>
    Object.fromEntries(
      columns.map((column, i) => [
        column.dataset.column,
        headers[i].getBoundingClientRect().width,
      ]),
    );
  const save = () => {
    try {
      localStorage.setItem(key(), JSON.stringify(widths));
    } catch {
      // Column widths still apply when browser storage is unavailable.
    }
  };
  const finish = () => {
    if (!drag) return;
    const { pointerId } = drag;
    drag = null;
    table.classList.remove("resizing-columns");
    if (table.hasPointerCapture(pointerId))
      table.releasePointerCapture(pointerId);
    save();
  };
  function installHandles() {
    columns.forEach((column, i) => {
      if (i === 0) return;
      const handle = document.createElement("button");
      handle.className = "column-resize-handle";
      handle.type = "button";
      handle.setAttribute(
        "aria-label",
        `调整${headers[i].textContent.trim()}列宽`,
      );
      handle.title = "拖动调整列宽";
      headers[i].append(handle);
      handle.onpointerdown = (event) => {
        if (event.button !== 0 || !event.isPrimary) return;
        event.preventDefault();
        widths = snapshot();
        drag = {
          pointerId: event.pointerId,
          column,
          x: event.clientX,
          width: widths[column.dataset.column],
        };
        table.setPointerCapture(event.pointerId);
        table.classList.add("resizing-columns");
      };
      handle.onkeydown = (event) => {
        if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
        event.preventDefault();
        widths = snapshot();
        const name = column.dataset.column;
        widths[name] = clamp(
          column,
          widths[name] + (event.key === "ArrowRight" ? 24 : -24),
        );
        apply();
        save();
      };
    });
  }
  table.addEventListener("pointermove", (event) => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    widths[drag.column.dataset.column] = clamp(
      drag.column,
      drag.width + event.clientX - drag.x,
    );
    apply();
  });
  table.addEventListener("pointerup", finish);
  table.addEventListener("pointercancel", finish);
  table.addEventListener("lostpointercapture", finish);
  enableHorizontalScroll(container);
  new ResizeObserver(apply).observe(container);
  return (nextKind) => {
    if (kind === nextKind) return;
    finish();
    kind = nextKind;
    const schema = kind === "trajectory" ? trajectory : base;
    const colgroup = table.querySelector("colgroup");
    const heading = table.querySelector("thead tr");
    colgroup.replaceChildren();
    [...heading.children].slice(1).forEach(header => header.remove());
    schema.forEach(([name, label, width, min], index) => {
      const column = document.createElement("col");
      Object.assign(column.dataset, {column: name, width, min});
      colgroup.append(column);
      if (index) {
        const header = document.createElement("th");
        const span = document.createElement("span");
        span.textContent = label;
        header.append(span);
        heading.append(header);
      }
    });
    columns = [...colgroup.children];
    headers = [...heading.children];
    installHandles();
    widths = {};
    try {
      const saved = JSON.parse(localStorage.getItem(key()));
      columns.forEach((column, i) => {
        const value = saved?.[column.dataset.column];
        if (i > 0 && Number.isFinite(value) && value > 0)
          widths[column.dataset.column] = clamp(column, value);
      });
    } catch {
      // Ignore invalid or unavailable saved preferences.
    }
    apply();
    container.scrollLeft = 0;
  };
}
