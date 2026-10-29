import { api } from "../api/client.js";
import { $, esc, field, showModal, enableHorizontalScroll } from "./ui.js";
import { openMenu, menuAction } from "./menu.js";

const colors = [
  "#2563eb",
  "#0284c7",
  "#0891b2",
  "#0d9488",
  "#059669",
  "#16a34a",
  "#65a30d",
  "#a16207",
  "#ca8a04",
  "#d97706",
  "#ea580c",
  "#dc2626",
  "#e11d48",
  "#db2777",
  "#c026d3",
  "#9333ea",
  "#7c3aed",
  "#4f46e5",
  "#475569",
  "#64748b",
  "#78716c",
  "#92400e",
  "#be123c",
  "#1e3a8a",
];
const icon =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3h8l10 10-8 8L3 11Z"/><circle cx="7.5" cy="7.5" r="1.3"/></svg>';
const colorStyle = (color) => `--tag-color:${esc(color)}`;

export function tagChip(tag, definitions, assetId) {
  const color = definitions.find((t) => t.tag === tag)?.color || colors[0];
  return `<span class="chip colored-tag" style="${colorStyle(color)}"><span title="${esc(tag)}">${esc(tag)}</span>${assetId ? `<button class="remove-tag" data-asset="${esc(assetId)}" data-tag="${esc(tag)}" aria-label="移除标签 ${esc(tag)}" title="移除标签">×</button>` : ""}</span>`;
}

export function renderTags(kind, tags, selected, onFilter, onChange) {
  const filter = $("#tag-search").value.toLowerCase();
  const container = $("#tag-list");
  const signature = JSON.stringify([kind, tags, [...selected], filter]);
  if (container.dataset.signature === signature && container.childNodes.length)
    return;
  container.dataset.signature = signature;
  container.innerHTML =
    tags
      .filter((t) => t.tag.toLowerCase().includes(filter))
      .map(
        (t) =>
          `<div class="tag-option"><button class="tag-icon" style="${colorStyle(t.color)}" data-tag="${esc(t.tag)}" aria-label="管理标签 ${esc(t.tag)}" title="管理标签">${icon}</button><label><span title="${esc(t.tag)}">${esc(t.tag)}</span><input type="checkbox" value="${esc(t.tag)}" ${selected.has(t.tag) ? "checked" : ""}></label></div>`,
      )
      .join("") || '<p class="muted small">暂无标签</p>';
  $("#tag-list")
    .querySelectorAll("input")
    .forEach((input) => {
      input.onchange = () => {
        input.checked
          ? selected.add(input.value)
          : selected.delete(input.value);
        onFilter();
      };
    });
  $("#tag-list")
    .querySelectorAll(".tag-icon")
    .forEach((button) => {
      const tag = tags.find((t) => t.tag === button.dataset.tag);
      button.onclick = () =>
        openMenu(
          button,
          `<strong class="menu-title">${esc(tag.tag)}</strong><button data-rename>重命名</button><div class="menu-title">换色</div><div class="color-grid">${colors.map((color) => `<button class="color-swatch" data-color="${color}" style="background:${color}" aria-label="颜色 ${color}" aria-pressed="${color === tag.color}" title="${color}">${color === tag.color ? "✓" : ""}</button>`).join("")}</div><button data-delete class="danger-text">删除标签</button>`,
          (menu, close) => {
            menu.querySelectorAll("[data-color]").forEach((swatch) =>
              menuAction(swatch, async () => {
                await api(
                  `/tags?kind=${kind}&tag=${encodeURIComponent(tag.tag)}`,
                  {
                    method: "PATCH",
                    body: { tag: tag.tag, color: swatch.dataset.color },
                  },
                );
                close();
                await onChange();
              }),
            );
            menu.querySelector("[data-rename]").onclick = () => {
              close();
              showModal(
                "重命名标签",
                field("标签名称", "tag", tag.tag, { required: true }),
                {
                  action: async (form) => {
                    const updated = await api(
                      `/tags?kind=${kind}&tag=${encodeURIComponent(tag.tag)}`,
                      {
                        method: "PATCH",
                        body: { tag: form.get("tag"), color: tag.color },
                      },
                    );
                    if (selected.delete(tag.tag)) selected.add(updated.tag);
                    await onChange();
                  },
                },
              );
            };
            menu.querySelector("[data-delete]").onclick = () => {
              close();
              showModal(
                "删除标签",
                `<p>删除标签“${esc(tag.tag)}”，并从 ${kind === "task" ? "Task Pool" : "Runtime Pool"} 的所有记录中移除？当前关联 ${tag.count} 条记录，资产本身保留。</p>`,
                {
                  submit: "确认删除",
                  action: async () => {
                    await api(
                      `/tags?kind=${kind}&tag=${encodeURIComponent(tag.tag)}`,
                      {
                        method: "DELETE",
                      },
                    );
                    selected.delete(tag.tag);
                    await onChange();
                  },
                },
              );
            };
          },
        );
    });
}

export function createTag(kind, onChange) {
  showModal("新增标签", field("标签名称", "tag", "", { required: true }), {
    action: async (form) => {
      await api(`/tags?kind=${kind}`, {
        method: "POST",
        body: { tag: form.get("tag") },
      });
      await onChange();
    },
  });
}

export function assignTags(selection, definitions, onChange) {
  showModal(
    "打标签",
    definitions.length
      ? `<div class="tag-picker">${definitions.map((t) => `<label class="check-label"><input type="checkbox" name="tags" value="${esc(t.tag)}">${tagChip(t.tag, definitions)}</label>`).join("")}</div>`
      : "<p>请先在左侧新增标签。</p>",
    {
      submit: "添加",
      action: definitions.length
        ? async (form) => {
            const add = form.getAll("tags");
            if (!add.length) throw Error("请选择标签");
            await api("/assets/tags", {
              method: "POST",
              body: { selection, add },
            });
            await onChange();
          }
        : undefined,
    },
  );
}

export function bindRowTags(kind, onChange) {
  $("#rows").querySelectorAll(".table-tags").forEach(enableHorizontalScroll);
  $("#rows")
    .querySelectorAll(".remove-tag")
    .forEach((button) => {
      button.onclick = () =>
        showModal(
          "移除标签",
          `<p>从这条记录移除标签“${esc(button.dataset.tag)}”？</p>`,
          {
            submit: "确认移除",
            action: async () => {
              await api("/assets/tags", {
                method: "POST",
                body: {
                  selection: { kind, ids: [button.dataset.asset] },
                  remove: [button.dataset.tag],
                },
              });
              await onChange();
            },
          },
        );
    });
}
