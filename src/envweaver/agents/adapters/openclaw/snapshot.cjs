// A fresh --state-dir belongs to this attempt. agent exec creates an explicit
// session for its generated ID. Child sessions have different catalog keys.
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { createRequire } = require("node:module");
const { pathToFileURL } = require("node:url");

async function snapshot(cli, mode, main) {
  let sessions = main ? [main] : [];
  if (!main || mode === "final") {
    const catalog = JSON.parse(execFileSync(cli, ["sessions", "--json", "--all-agents", "--limit", "all"], {
      encoding: "utf8", maxBuffer: 64 * 1024 * 1024, stdio: ["ignore", "pipe", "inherit"],
    }));
    const stores = new Map(catalog.stores.map(s => [s.agentId, s.path]));
    sessions = catalog.sessions.map(({key, sessionId, agentId}) => ({
      sessionKey: key, sessionId, agentId, storePath: stores.get(agentId),
    }));
    const roots = sessions.filter(s => s.sessionKey === `agent:${s.agentId}:explicit:${s.sessionId}`);
    main = roots.find(s => s.sessionId === main?.sessionId && s.sessionKey === main.sessionKey)
      ?? (roots.length === 1 ? roots[0] : null);
  }
  if (mode === "live") sessions = main ? [main] : [];
  if (sessions.length || mode === "final") {
    const resolve = createRequire(fs.realpathSync(cli)).resolve;
    const sdk = await import(pathToFileURL(resolve("openclaw/plugin-sdk/session-transcript-runtime")));
    const result = {};
    for (const session of sessions) {
      const events = await sdk.readSessionTranscriptEvents(session);
      result[session.sessionId] = events.filter(e => e.type === "message").map(e => e.message);
    }
    const file = path.join(__dirname, "trace", "messages.json");
    const data = JSON.stringify(result);
    if (!fs.existsSync(file) || fs.readFileSync(file, "utf8") !== data) {
      fs.writeFileSync(file, data, {mode: 0o600});
    }
  }
  process.stdout.write(JSON.stringify(main));
}

snapshot(process.argv[2], process.argv[3], JSON.parse(process.argv[4])).then(() => process.exit(0)).catch(e => {
  console.error(e.message);
  process.exit(1);
});
