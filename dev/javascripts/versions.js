document.addEventListener("DOMContentLoaded", async () => {
  const current = document.getElementById("docs-version-script").dataset.version;
  const versionRoot = new URL(`${base_url}/`, location.href);
  try {
    const response = await fetch(new URL("../versions.json", versionRoot));
    if (!response.ok) return;
    const versions = await response.json();
    const item = document.createElement("li");
    item.className = "nav-item dropdown";
    const button = document.createElement("button");
    button.className = "nav-link dropdown-toggle";
    button.dataset.bsToggle = "dropdown";
    button.dataset.bsDisplay = "static";
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-label", `文档版本 ${current}`);
    button.textContent = current;
    const menu = document.createElement("ul");
    menu.className = "dropdown-menu dropdown-menu-end";
    for (const version of versions) {
      if (version.properties?.hidden && version.version !== current) continue;
      const row = document.createElement("li");
      const link = document.createElement("a");
      link.className = "dropdown-item";
      link.href = new URL(`../${version.version}/`, versionRoot).href;
      link.textContent = version.title;
      if (version.version === current) {
        link.classList.add("active");
        link.setAttribute("aria-current", "true");
      }
      row.append(link);
      menu.append(row);
    }
    item.append(button, menu);
    document.getElementById("theme-menu").closest("li").after(item);
  } catch (error) {
    console.warn("Unable to load documentation versions", error);
  }
});
