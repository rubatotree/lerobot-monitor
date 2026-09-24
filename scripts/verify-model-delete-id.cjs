const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/lerobot_monitor/web/static/app.js"), "utf8");
const idStart = source.indexOf("function librarySourceId(");
const idEnd = source.indexOf("function libraryCache(", idStart);
const deleteStart = source.indexOf("async function deleteLibraryResource(");
const deleteEnd = source.indexOf("async function syncLibraryResource(", deleteStart);
assert(idStart >= 0 && idEnd > idStart && deleteStart >= 0 && deleteEnd > deleteStart);

const model = {
  id: "rubatotree-classify-blocks-2-smolvla",
  repo_id: "rubatotree/classify-blocks-2-smolvla",
  name: "rubatotree/classify-blocks-2-smolvla",
};
const requests = [];
const context = {
  window: { confirm: () => true },
  libraryDisplayName: (_kind, row) => row.name,
  isLibrarySelected: () => false,
  episodeSource: null,
  vizState: {},
  api: async (url, _body, method) => { requests.push({ url, method }); },
  clearLibrarySelection: () => {},
  refreshLibrarySection: async () => {},
  toastError: (error) => { throw error; },
};

vm.runInNewContext(`${source.slice(idStart, idEnd)}\n${source.slice(deleteStart, deleteEnd)}`, context);
context.deleteLibraryResource("model", model)
  .then(() => {
    assert.equal(context.librarySourceId("model", model), model.id);
    assert.deepEqual(requests, [{
      url: `/api/library?kind=model&id=${encodeURIComponent(model.id)}`,
      method: "DELETE",
    }]);
    process.stdout.write("model delete ID: passed\n");
  })
  .catch((error) => { process.stderr.write(`${error.stack}\n`); process.exitCode = 1; });
