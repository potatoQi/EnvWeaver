// Observe OpenClaw's model traffic without changing fetch, headers or routing.
const dc = require("node:diagnostics_channel");
const fs = require("node:fs");
const zlib = require("node:zlib");

function capture(path) {
  const active = new WeakMap();
  function record(row) {
    // A diagnostics subscriber must not throw into the provider transport.
    try { fs.appendFileSync(path, JSON.stringify(row) + "\n", { mode: 0o600 }); } catch {}
  }
  dc.subscribe("undici:request:create", ({ request }) => {
    if (request.method === "POST" &&
        /\/(?:messages|chat\/completions|responses)\/?$/.test(request.path.split("?")[0])) {
      active.set(request, { sent: [], received: [], id: require("node:crypto").randomUUID() });
    }
  });
  for (const [event, field] of [["bodyChunkSent", "sent"], ["bodyChunkReceived", "received"]]) {
    dc.subscribe("undici:request:" + event, ({ request, chunk }) => {
      active.get(request)?.[field].push(Buffer.from(chunk));
    });
  }
  dc.subscribe("undici:request:bodySent", ({ request }) => {
    const c = active.get(request);
    if (!c) return;
    try {
      const payload = JSON.parse(Buffer.concat(c.sent));
      if (typeof payload?.model === "string" &&
          (Array.isArray(payload.messages) || Array.isArray(payload.input) || typeof payload.input === "string")) {
        record({ id: c.id, request: payload });
      } else { active.delete(request); }
    } catch { /* Non-JSON or unobservable transport. */ }
    c.sent = [];
  });
  dc.subscribe("undici:request:headers", ({ request, response }) => {
    const c = active.get(request);
    if (!c) return;
    c.status = response.statusCode;
    for (let i = 0; i < response.headers.length; i += 2) {
      if (String(response.headers[i]).toLowerCase() === "content-encoding") {
        c.encoding = String(response.headers[i + 1]).toLowerCase();
      }
    }
  });
  function finish({ request, error }) {
    const c = active.get(request);
    if (!c) return;
    let bytes = Buffer.concat(c.received);
    try {
      const decode = { gzip: zlib.gunzipSync, br: zlib.brotliDecompressSync, deflate: zlib.inflateSync }[c.encoding];
      if (decode) bytes = decode(bytes);
      // A provider may close before undici can send the body. Record only a
      // transport code in that case, never headers, URLs or an inferred payload.
      const code = error?.code || error?.cause?.code;
      const transport = typeof code === "string" &&
        /^(?:ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|EPIPE|UND_ERR_(?:SOCKET|CONNECT_TIMEOUT|HEADERS_TIMEOUT|BODY_TIMEOUT))$/.test(code);
      record({ id: c.id, status: c.status, body: bytes.toString("utf8"),
               ...(transport ? {transport_error: code} : {}) });
    } catch {
      record({ id: c.id, status: c.status, incomplete_response: true });
    } finally { active.delete(request); }
  }
  dc.subscribe("undici:request:trailers", finish);
  dc.subscribe("undici:request:error", finish);
}

// OpenClaw may respawn its launcher. Compare the actual executable on PATH
// so unrelated Node processes spawned by tools never enter capture.
const entry = process.env.ENVWEAVER_OPENCLAW_ENTRY;
if (entry && process.argv[1] && fs.existsSync(process.argv[1]) &&
    fs.realpathSync(process.argv[1]) === fs.realpathSync(entry)) {
  capture(require("node:path").join(__dirname, "trace", "http.jsonl"));
}
