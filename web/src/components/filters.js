import { esc } from "./ui.js";
import { openMenu } from "./menu.js";

export function renderMultiFilter(
  button,
  available,
  selected,
  onChange,
  allLabel,
  label = (value) => value,
) {
  const updateLabel = () => {
    const text = selected.size ? [...selected].map(label).join("、") : allLabel;
    button.textContent = `${text} ▾`;
    button.title = text;
  };
  updateLabel();
  button.onclick = () => {
    const options = [...new Set([...available, ...selected])];
    openMenu(
      button,
      `<label class="filter-option filter-all"><span>${esc(allLabel)}</span><input type="checkbox" data-all ${selected.size ? "" : "checked"}></label>${options.map((value) => `<label class="filter-option"><span>${esc(label(value))}</span><input type="checkbox" value="${esc(value)}" ${selected.has(value) ? "checked" : ""}></label>`).join("")}`,
      (menu) => {
        const all = menu.querySelector("[data-all]");
        const inputs = [...menu.querySelectorAll("input[value]")];
        const update = () => {
          all.checked = !selected.size;
          updateLabel();
          onChange();
        };
        all.onchange = () => {
          selected.clear();
          inputs.forEach((input) => (input.checked = false));
          update();
        };
        inputs.forEach(
          (input) =>
            (input.onchange = () => {
              input.checked
                ? selected.add(input.value)
                : selected.delete(input.value);
              update();
            }),
        );
      },
    );
  };
}

export function renderSingleFilter(button, options, selected, onChange) {
  const updateLabel = () => {
    button.textContent = `${options.find(([value]) => value === selected)[1]} ▾`;
  };
  updateLabel();
  button.onclick = () => {
    openMenu(
      button,
      options
        .map(
          ([value, label]) =>
            `<label class="filter-option"><span>${esc(label)}</span><input type="radio" name="single-filter" value="${esc(value)}" ${value === selected ? "checked" : ""}></label>`,
        )
        .join(""),
      (menu, close) => {
        menu.querySelectorAll("input").forEach((input) => {
          input.onchange = () => {
            selected = input.value;
            updateLabel();
            close();
            button.focus();
            onChange(selected);
          };
        });
        menu.querySelector(":checked").focus();
      },
    );
  };
}
