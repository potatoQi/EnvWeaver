export function showImage(source) {
  const dialog = document.createElement("dialog");
  dialog.className = "image-viewer";
  dialog.setAttribute("aria-label", "查看图片");
  dialog.innerHTML = `<div class="dialog-heading"><h2>查看图片</h2><button class="quiet" aria-label="关闭图片">×</button></div><div class="image-viewport"><img alt="任务图片"></div>`;
  const image = dialog.querySelector("img");
  image.src = source.src;
  image.alt = source.alt;
  dialog.querySelector(".quiet").onclick = () => dialog.close();
  dialog.onclick = (event) => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (
      event.clientX < bounds.left ||
      event.clientX > bounds.right ||
      event.clientY < bounds.top ||
      event.clientY > bounds.bottom
    )
      dialog.close();
  };
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  document.body.append(dialog);
  dialog.showModal();
  return dialog;
}
