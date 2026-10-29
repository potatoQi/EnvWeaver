export const $ = (s) => document.querySelector(s);
export const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
export const pretty = (value) => JSON.stringify(value, null, 2);
export const tagList = (value) =>
  value
    .split(/[,，\n]/)
    .map((v) => v.trim())
    .filter(Boolean);
export const date = (value) =>
  new Date(value).toLocaleString("zh-CN", { hour12: false });
const compactNumber = new Intl.NumberFormat("en-US", {maximumFractionDigits: 2, useGrouping: false});
export function formatDuration(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  const [scale, unit] = seconds >= 3600 ? [3600, "h"] : seconds >= 60 ? [60, "min"] : [1, "s"];
  return `${compactNumber.format(seconds / scale)} ${unit}`;
}
export function formatTokens(tokens) {
  if (!Number.isFinite(tokens)) return "—";
  const [scale, unit] = tokens >= 1e6 ? [1e6, "M"] : tokens >= 1e3 ? [1e3, "K"] : [1, ""];
  return `${compactNumber.format(tokens / scale)}${unit}`;
}
export function formatSize(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let unit = 0;
  while (bytes >= 1024 && unit < units.length - 1) {
    bytes /= 1024;
    unit++;
  }
  return `${unit === 0 ? bytes : bytes.toFixed(1)} ${units[unit]}`;
}
export function toast(message, error = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = error ? "error" : "";
  el.hidden = false;
  clearTimeout(el.timer);
  el.timer = setTimeout(() => (el.hidden = true), 6000);
}
export function field(label, name, value = "", options = {}) {
  return `<label>${esc(label)}${options.area ? `<textarea name="${name}" rows="${options.rows || 5}" ${options.readonly ? "readonly" : ""}>${esc(value)}</textarea>` : `<input name="${name}" value="${esc(value)}" ${options.required ? "required" : ""} ${options.placeholder ? `placeholder="${esc(options.placeholder)}"` : ""}>`}</label>`;
}
export function showModal(
  title,
  body,
  { submit = "保存", action, wide = false } = {},
) {
  const dialog = $("#modal");
  if (dialog.open) dialog.close();
  dialog.className = wide ? "wide" : "";
  dialog.innerHTML = `<form><div class="dialog-heading"><h2>${esc(title)}</h2><button type="button" class="quiet close" aria-label="关闭">×</button></div><div class="dialog-body">${body}</div><p class="form-error" role="alert" hidden></p><div class="dialog-actions"><button type="button" class="cancel">取消</button>${action ? `<button class="primary" type="submit">${esc(submit)}</button>` : ""}</div></form>`;
  dialog.querySelector(".close").onclick = dialog.querySelector(
    ".cancel",
  ).onclick = () => dialog.close();
  if (action) {
    const form = dialog.querySelector("form");
    const button = form.querySelector("[type=submit]");
    const requiredChecks = [
      ...form.querySelectorAll('input[type="checkbox"][required]'),
    ];
    let submitting = false;
    const updateSubmit = () => {
      button.disabled =
        submitting || requiredChecks.some((input) => !input.checked);
    };
    requiredChecks.forEach((input) =>
      input.addEventListener("change", updateSubmit),
    );
    updateSubmit();
    form.onsubmit = async (event) => {
      event.preventDefault();
      if (button.disabled || !form.reportValidity()) return;
      submitting = true;
      button.textContent = "处理中…";
      updateSubmit();
      const error = dialog.querySelector(".form-error");
      error.hidden = true;
      try {
        await action(new FormData(form));
        dialog.close();
      } catch (e) {
        error.textContent = e.message;
        error.hidden = false;
      } finally {
        submitting = false;
        button.textContent = submit;
        updateSubmit();
      }
    };
  }
  dialog.showModal();
  return dialog;
}
export function bind(id, handler) {
  $(id).onclick = async (event) => {
    try {
      await handler(event);
    } catch (error) {
      toast(error.message, true);
    }
  };
}

const horizontalScrollContainers = new WeakSet();

export function enableHorizontalScroll(container) {
  if (horizontalScrollContainers.has(container)) return;
  horizontalScrollContainers.add(container);
  container.addEventListener(
    "wheel",
    (event) => {
      if (
        !event.shiftKey ||
        event.defaultPrevented ||
        container.scrollWidth <= container.clientWidth
      )
        return;
      event.preventDefault();
      container.scrollLeft +=
        event.deltaX ||
        event.deltaY *
          (event.deltaMode === 1
            ? 20
            : event.deltaMode === 2
              ? container.clientWidth
              : 1);
    },
    { passive: false },
  );
}

export function download(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
