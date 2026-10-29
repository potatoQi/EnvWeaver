const widthKey = "envweaver.detailWidth";

export function setupDetailPanel(dialog) {
  try {
    const width = Number(localStorage.getItem(widthKey));
    if (Number.isFinite(width) && width > 0)
      dialog.style.setProperty("--detail-width", `${width}px`);
  } catch {
    // Browser storage may be unavailable; resizing still works for this page.
  }

  let drag = null;
  let outside = false;
  const bounds = () => {
    const max = Math.min(
      innerWidth,
      parseFloat(getComputedStyle(dialog).maxWidth),
    );
    return { min: Math.min(360, max), max };
  };
  const resize = (width) => {
    const { min, max } = bounds();
    dialog.style.setProperty(
      "--detail-width",
      `${Math.round(Math.max(min, Math.min(max, width)))}px`,
    );
  };
  const remember = () => {
    try {
      const width = dialog.open
        ? dialog.getBoundingClientRect().width
        : parseFloat(dialog.style.getPropertyValue("--detail-width"));
      if (width > 0) localStorage.setItem(widthKey, width);
    } catch {
      // Keep the in-memory width when browser storage is unavailable.
    }
  };
  const isOutside = (event) => {
    const rect = dialog.getBoundingClientRect();
    return (
      event.target === dialog &&
      (event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom)
    );
  };
  const finishDrag = () => {
    if (!drag) return;
    const { pointerId } = drag;
    drag = null;
    dialog.classList.remove("resizing");
    if (dialog.hasPointerCapture(pointerId))
      dialog.releasePointerCapture(pointerId);
    remember();
  };

  dialog.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || !event.isPrimary) return;
    outside = isOutside(event);
    if (!event.target.closest(".detail-resize-handle")) return;
    event.preventDefault();
    drag = {
      pointerId: event.pointerId,
      x: event.clientX,
      width: dialog.getBoundingClientRect().width,
    };
    dialog.setPointerCapture(event.pointerId);
    dialog.classList.add("resizing");
  });
  dialog.addEventListener("pointermove", (event) => {
    if (drag && event.pointerId === drag.pointerId)
      resize(drag.width + drag.x - event.clientX);
  });
  dialog.addEventListener("pointerup", (event) => {
    if (drag) {
      if (event.pointerId === drag.pointerId) finishDrag();
    } else if (outside && isOutside(event)) {
      dialog.close();
    }
    outside = false;
  });
  dialog.addEventListener("pointercancel", () => {
    outside = false;
    finishDrag();
  });
  dialog.addEventListener("lostpointercapture", finishDrag);
  dialog.addEventListener("close", () => {
    outside = false;
    finishDrag();
  });
  dialog.addEventListener("keydown", (event) => {
    if (!event.target.matches(".detail-resize-handle")) return;
    const { min, max } = bounds();
    const width = dialog.getBoundingClientRect().width;
    const sizes = {
      ArrowLeft: width + 32,
      ArrowRight: width - 32,
      Home: min,
      End: max,
    };
    if (!(event.key in sizes)) return;
    event.preventDefault();
    resize(sizes[event.key]);
    remember();
  });
}
