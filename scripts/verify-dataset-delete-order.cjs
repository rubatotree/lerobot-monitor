const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname, "../src/lerobot_monitor/web/static/app.js"), "utf8");
const start = app.indexOf("async function waitForReplayMediaRelease(");
const end = app.indexOf("async function syncLibraryResource(", start);
assert(start >= 0 && end > start, "dataset delete functions were not found");
const functions = app.slice(start, end);

async function check(confirmed) {
  const events = [];
  const row = { id: "local/blocks", repo_id: "user/blocks", path: "C:/datasets/blocks" };
  const state = {
    row,
    episodeSource: { kind: "dataset", id: row.id },
    vizState: { kind: "dataset", id: row.id },
    datasetsCache: [row],
    window: { confirm: () => { events.push("confirm"); return confirmed; } },
    librarySourceId: (_kind, item) => item.repo_id || item.id,
    libraryDisplayName: (_kind, item) => item.repo_id,
    isLibrarySelected: () => true,
    vizVideos: () => [{ readyState: 0, networkState: 0, NETWORK_EMPTY: 0 }],
    closeEpisodeSelection: () => { events.push("exit"); },
    requestAnimationFrame: (callback) => { events.push("paint"); callback(); },
    api: async () => { events.push("delete"); assert(events.includes("exit")); assert(events.includes("paint")); },
    clearLibrarySelection: () => {},
    renderDatasets: () => {},
    refreshLibrarySection: async () => { events.push("refresh"); },
    toastError: (error) => { throw error; },
    setTimeout,
    clearTimeout,
  };
  await vm.runInNewContext(`${functions}\ndeleteLibraryResource("dataset", row)`, state);
  assert.deepEqual(events, confirmed
    ? ["confirm", "exit", "paint", "delete", "refresh"]
    : ["confirm"]);
}

Promise.all([check(true), check(false)])
  .then(() => process.stdout.write("dataset delete order: passed\n"))
  .catch((error) => { process.stderr.write(`${error.stack}\n`); process.exitCode = 1; });
