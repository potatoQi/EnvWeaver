import { $ } from "./ui.js";

export function renderPagination(current, total, onSelect) {
  const pages = new Set([
    1,
    2,
    3,
    total - 1,
    total,
    current - 1,
    current,
    current + 1,
  ]);
  const visible = [...pages]
    .filter((page) => page > 0 && page <= total)
    .sort((a, b) => a - b);
  const items = [];
  let previous = 0;
  const button = (page) =>
    `<button data-page="${page}" aria-label="第 ${page} 页" ${page === current ? 'aria-current="page"' : ""}>${page}</button>`;
  for (const page of visible) {
    if (page - previous === 2) items.push(button(previous + 1));
    else if (page - previous > 2) items.push('<span class="page-gap">…</span>');
    items.push(button(page));
    previous = page;
  }
  $("#page-links").innerHTML = items.join("");
  $("#page-links")
    .querySelectorAll("button")
    .forEach((el) => {
      el.onclick = () => {
        const page = Number(el.dataset.page);
        if (page !== current) onSelect(page);
      };
    });
}
