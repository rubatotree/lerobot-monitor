const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname, "../src/lerobot_monitor/web/static/app.js"), "utf8");
function extract(startMarker, endMarker) {
  const start = app.indexOf(startMarker);
  const end = app.indexOf(endMarker, start);
  assert(start >= 0 && end > start, `${startMarker} was not found`);
  return app.slice(start, end);
}

const functions = [
  extract("function toggleJointControl()", "async function setJointSerial("),
  extract("function toggleJointAutoSync()", 'if ($("joint-sync-source"))'),
  extract("function syncBackendPlayback(d)", "function renderReplayMarkers("),
].join("\n");

const events = [];
const state = {
  jointControlEnabled: false,
  jointAutoSyncEnabled: false,
  jointSyncSource: "command",
  replayActive: true,
  armReplay: false,
  vizState: { previewReady: true },
  last: { joints: {} },
  jointSerialOutputReady: () => true,
  syncJointControls: () => {},
  syncJointTargetFromSource: () => events.push("sync sliders"),
  updateJoints: () => {},
  sendCurrentCommandOnce: () => events.push("send once"),
  startBackendPlayback: async () => { events.push("start playback"); },
  stopBackendPlayback: async () => { events.push("stop playback"); },
  toastError: (error) => { throw error; },
};
const context = vm.createContext(state);
vm.runInContext(functions, context);

vm.runInContext("toggleJointAutoSync()", context);
assert.deepEqual(events, ["sync sliders"]);
vm.runInContext("toggleJointControl()", context);
assert.deepEqual(events, ["sync sliders", "start playback"]);
assert.equal(state.jointControlEnabled, true);

state.jointSerialOutputReady = () => false;
vm.runInContext("toggleJointControl()", context);
assert.deepEqual(events, ["sync sliders", "start playback", "stop playback"]);
assert.equal(state.jointControlEnabled, false);

state.armReplay = true;
state.jointControlEnabled = true;
vm.runInContext("toggleJointControl()", context);
assert.deepEqual(events, ["sync sliders", "start playback", "stop playback", "stop playback"]);
assert.equal(state.jointControlEnabled, false);

process.stdout.write("Joints Auto playback transition: passed\n");

state.playbackSession = { id: "new-session", version: 1 };
state.playbackStartStatusTs = 11;
state.armReplay = true;
state.playbackStoppingId = null;
state.playbackHeartbeat = 0;
state.syncArmToggle = () => {};
state.clearInterval = () => {};
vm.runInContext("syncBackendPlayback({ ts: 10, playback: null })", context);
assert.equal(state.playbackSession.id, "new-session");
vm.runInContext("syncBackendPlayback({ ts: 12, playback: null })", context);
assert.equal(state.playbackSession, null);
assert.equal(state.armReplay, false);
process.stdout.write("Stale playback snapshot: passed\n");

const transitions = extract("function queuePlaybackTransition(work)", "function sampleArmJoints(");
const requests = [];
const queued = {
  playbackTransition: Promise.resolve(),
  playbackIntent: 0,
  playbackSession: null,
  playbackRequest: Promise.resolve(),
  playbackHeartbeat: 0,
  playbackStartStatusTs: 0,
  playbackStoppingId: null,
  armReplay: false,
  vizState: { kind: "video", id: "demo", episode: 0, speed: 1, playing: true },
  jointMaxSpeed: 180,
  last: { ts: 1 },
  api: async (route) => {
    requests.push(route);
    return route === "/api/control/playback" ? { playback: { id: "session", version: 1 } } : {};
  },
  syncArmToggle: () => {},
  setInterval: () => 1,
  clearInterval: () => {},
};
const queuedContext = vm.createContext(queued);
vm.runInContext(transitions, queuedContext);
vm.runInContext(extract("function requestBackendPlaybackState(playing)", "function fillReplayChart("), queuedContext);
vm.runInContext(extract("function requestBackendPlaybackSpeed(speed)", "function setVideoPlaybackRate("), queuedContext);
vm.runInContext(extract("function requestBackendPlaybackSeek(elapsed)", "function seekReplayFraction("), queuedContext);

async function checkQueuedPlayback() {
  const start = vm.runInContext("startBackendPlayback('command')", queuedContext);
  const stop = vm.runInContext("stopBackendPlayback()", queuedContext);
  await Promise.all([start, stop]);
  assert.deepEqual(requests, [], "a cancelled queued start must not reach the arm");

  await vm.runInContext("startBackendPlayback('command')", queuedContext);
  await vm.runInContext("stopBackendPlayback()", queuedContext);
  assert.deepEqual(requests, ["/api/control/playback", "/api/control/playback/action"]);
  assert.equal(queued.playbackSession, null);
  process.stdout.write("Queued playback cancellation: passed\n");

  queued.vizState.playing = false;
  let releaseStart;
  let startRequested;
  const started = new Promise((resolve) => { startRequested = resolve; });
  queued.api = async (route, payload) => {
    requests.push({ route, payload });
    if (route === "/api/control/playback") {
      startRequested();
      return new Promise((resolve) => { releaseStart = resolve; });
    }
    return { playback: { id: "session", version: 2 } };
  };
  const pending = vm.runInContext("startBackendPlayback('command')", queuedContext);
  await started;
  assert.equal(requests.at(-1).payload.playing, false);
  vm.runInContext("requestBackendPlaybackState(true)", queuedContext);
  vm.runInContext("requestBackendPlaybackSpeed(0.5)", queuedContext);
  vm.runInContext("requestBackendPlaybackSeek(4)", queuedContext);
  releaseStart({ playback: { id: "session", version: 1 } });
  await pending;
  assert.deepEqual(requests.slice(-3).map((item) => item.payload.operation), ["speed", "seek", "resume"]);
  assert.equal(requests.at(-3).payload.speed, 0.5);
  assert.equal(requests.at(-2).payload.elapsed_s, 4);
  process.stdout.write("Transport changes during playback start: passed\n");
}

checkQueuedPlayback().catch((error) => { process.stderr.write(`${error.stack}\n`); process.exitCode = 1; });
