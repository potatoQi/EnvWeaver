import { toast } from "./ui.js";

export function openMenu(anchor, content, setup) {
  document.querySelector(".popup-menu")?.remove();
  const menu = document.createElement("div");
  menu.className = "popup-menu";
  menu.setAttribute("popover", "auto");
  menu.innerHTML = content;
  document.body.append(menu);
  menu.showPopover();
  const box = anchor.getBoundingClientRect();
  const left = Math.min(box.left, window.innerWidth - menu.offsetWidth - 8);
  const top =
    box.bottom + menu.offsetHeight + 8 > window.innerHeight
      ? Math.max(8, box.top - menu.offsetHeight - 4)
      : box.bottom + 4;
  menu.style.left = `${Math.max(8, left)}px`;
  menu.style.top = `${top}px`;
  const close = () => menu.remove();
  menu.addEventListener("toggle", (event) => {
    if (event.newState === "closed") close();
  });
  setup(menu, close);
}

export function menuAction(button, action) {
  button.onclick = async () => {
    button.disabled = true;
    try {
      await action();
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  };
}
