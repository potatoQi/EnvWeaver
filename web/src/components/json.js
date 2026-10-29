import { esc } from "./ui.js";

export function renderJson(value) {
  const lines = JSON.stringify(value, null, 4).split("\n");
  const token =
    /"(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\b(?:true|false|null)\b|[{}[\],:]/g;
  const html = lines
    .map((line) => {
      const indent = line.match(/^ */)[0].length;
      let output = '<span class="json-indent">    </span>'.repeat(indent / 4);
      let offset = indent;
      for (const match of line.matchAll(token)) {
        const text = match[0];
        let kind = "punctuation";
        if (text.startsWith('"')) {
          kind = line
            .slice(match.index + text.length)
            .trimStart()
            .startsWith(":")
            ? "key"
            : "string";
        } else if (/^-?\d/.test(text)) kind = "number";
        else if (/^(true|false|null)$/.test(text)) kind = "literal";
        output += esc(line.slice(offset, match.index));
        output += `<span class="json-${kind}">${esc(text)}</span>`;
        offset = match.index + text.length;
      }
      return output + esc(line.slice(offset));
    })
    .join("\n");
  return `<pre class="json-view" tabindex="0" aria-label="JSON"><code>${html}</code></pre>`;
}
