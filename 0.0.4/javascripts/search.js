// Reuse MkDocs' index and dialog; match Chinese text and API names literally.
document.addEventListener("DOMContentLoaded", () => {
  const input = document.getElementById("mkdocs-search-query");
  const results = document.getElementById("mkdocs-search-results");
  const indexUrl = new URL(`${base_url}/search/search_index.json`, location.href);
  const siteUrl = new URL("../", indexUrl);
  let index;

  input.closest("form").addEventListener("submit", event => event.preventDefault());

  function highlight(element, text, pattern) {
    let offset = 0;
    for (const match of text.matchAll(pattern)) {
      element.append(text.slice(offset, match.index));
      const mark = document.createElement("mark");
      mark.textContent = match[0];
      element.append(mark);
      offset = match.index + match[0].length;
    }
    element.append(text.slice(offset));
  }

  async function search() {
    const query = input.value.trim().toLowerCase();
    results.replaceChildren();
    if (!query) return;
    results.textContent = "正在搜索…";
    try {
      index ??= fetch(indexUrl).then(response => {
        if (!response.ok) throw new Error("Search index unavailable");
        return response.json();
      }).then(data => data.docs).catch(error => {
        index = undefined;
        throw error;
      });
      const docs = await index;
      if (query !== input.value.trim().toLowerCase()) return;
      const terms = query.split(/\s+/);
      const pattern = new RegExp(
        [...new Set(terms)].sort((a, b) => b.length - a.length)
          .map(term => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"),
        "giu",
      );
      const matches = docs.filter(doc => {
        const text = `${doc.title} ${doc.text}`.toLowerCase();
        return doc.text.trim() && terms.every(term => text.includes(term));
      }).sort((a, b) => {
        const score = doc => terms.filter(term => doc.title.toLowerCase().includes(term)).length;
        return score(b) - score(a);
      });
      results.replaceChildren();
      for (const doc of matches) {
        const article = document.createElement("article");
        const heading = document.createElement("h3");
        const link = document.createElement("a");
        link.href = new URL(doc.location, siteUrl).href;
        highlight(link, doc.title, pattern);
        const summary = document.createElement("p");
        const positions = terms.map(term => doc.text.toLowerCase().indexOf(term))
          .filter(position => position >= 0);
        const position = positions.length ? Math.min(...positions) : 0;
        const start = Math.max(0, position - 40);
        const excerpt = (start ? "…" : "") + doc.text.slice(start, start + 180)
          + (start + 180 < doc.text.length ? "…" : "");
        highlight(summary, excerpt, pattern);
        heading.append(link);
        article.append(heading, summary);
        results.append(article);
      }
      if (!matches.length) results.textContent = "没有找到相关内容";
    } catch {
      if (query === input.value.trim().toLowerCase()) {
        results.textContent = "搜索索引加载失败，请重新输入以重试。";
      }
    }
  }

  input.addEventListener("input", search);
  input.value = new URLSearchParams(location.search).get("q") || "";
  search();
});
