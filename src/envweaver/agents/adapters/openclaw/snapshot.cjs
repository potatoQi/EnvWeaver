// Use the same public session catalog and transcript reader as the CLI.
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { createRequire } = require("node:module");
const { pathToFileURL } = require("node:url");

async function snapshot(cli) {
  const catalog = JSON.parse(execFileSync(cli, ["sessions", "--json", "--all-agents", "--limit", "all"], {
    encoding: "utf8", maxBuffer: 64 * 1024 * 1024, stdio: ["ignore", "pipe", "inherit"],
  }));
  const stores = new Map(catalog.stores.map(s => [s.agentId, s.path]));
  const resolve = createRequire(fs.realpathSync(cli)).resolve;
  const sdk = await import(pathToFileURL(resolve("openclaw/plugin-sdk/session-transcript-runtime")));
  const result = {};
  for (const {key, sessionId, agentId} of catalog.sessions) {
    const events = await sdk.readSessionTranscriptEvents({
      sessionId, sessionKey: key, agentId, storePath: stores.get(agentId),
    });
    result[sessionId] = events.filter(e => e.type === "message").map(e => e.message);
  }
  fs.writeFileSync(path.join(__dirname, "trace", "messages.json"), JSON.stringify(result), {mode: 0o600});
}

snapshot(process.argv[2]).then(() => process.exit(0)).catch(e => {
  console.error(e.message);
  process.exit(1);
});
