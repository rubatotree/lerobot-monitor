import assert from "node:assert/strict";
import test from "node:test";
import { createPreviewScheduler, advancePose } from "../src/lerobot_monitor/web/static/preview-scheduler.js";

function fixture(updatePose = () => false, onMain = () => {}) {
  let now = 0;
  let nextId = 0;
  let peakPending = 0;
  const queued = new Map();
  const frames = { main: [], wrist: [], pose: [] };
  const scheduler = createPreviewScheduler({
    requestFrame(callback) {
      queued.set(++nextId, callback);
      peakPending = Math.max(peakPending, queued.size);
      return nextId;
    },
    cancelFrame: (id) => queued.delete(id),
    updatePose(dt) { frames.pose.push(now); return updatePose(dt); },
    renderMain() { frames.main.push(now); onMain(scheduler); },
    renderWrist() { frames.wrist.push(now); },
  });
  const advance = (duration, hz = 120) => {
    const end = now + duration;
    while (now < end) {
      now += 1000 / hz;
      const callbacks = [...queued.values()];
      queued.clear();
      for (const callback of callbacks) callback(now);
    }
  };
  scheduler.setVisibility({ enabled: true, main: true, wrist: true });
  return { scheduler, advance, frames, queued, peak: () => peakPending };
}

test("bursts and reentrant invalidation have one frame owner", () => {
  let repeat = 3;
  const f = fixture(() => false, (s) => { if (repeat-- > 0) s.invalidate({ main: true }); });
  for (let i = 0; i < 1000; i++) f.scheduler.invalidate({ main: true });
  assert.equal(f.queued.size, 1);
  f.advance(1000);
  assert.equal(f.peak(), 1);
  assert.equal(f.frames.main.length, 4);
  assert.equal(f.queued.size, 0);
});

test("camera refreshes cap at 60 Hz and never refresh a settled wrist", () => {
  const f = fixture();
  f.advance(200);
  const wrist = f.frames.wrist.length;
  for (let i = 0; i < 240; i++) {
    f.scheduler.invalidate({ main: true });
    f.advance(1000 / 240, 240);
  }
  assert.ok(f.frames.main.length <= 62);
  assert.equal(f.frames.wrist.length, wrist);
  f.advance(200);
  assert.equal(f.queued.size, 0);
});

test("pose and wrist remain capped at 30 / 10 Hz under camera input", () => {
  const f = fixture(() => true);
  f.scheduler.invalidate({ pose: true });
  for (let i = 0; i < 120; i++) {
    f.scheduler.invalidate({ main: true });
    f.advance(1000 / 120);
  }
  assert.ok(f.frames.main.length <= 61);
  assert.ok(f.frames.pose.length <= 31);
  assert.ok(f.frames.wrist.length <= 11);
  assert.ok(f.frames.pose.length >= 29);
  assert.equal(f.peak(), 1);
});

test("pose converges exactly, then both views remain idle for five seconds", () => {
  const display = { shoulder: 0 };
  const target = { shoulder: 90 };
  const f = fixture((dt) => advancePose(display, target, dt, () => {}));
  f.scheduler.invalidate({ pose: true });
  f.advance(700);
  assert.equal(display.shoulder, 90);
  assert.ok(f.frames.pose.length <= 18);
  assert.equal(f.queued.size, 0);
  const counts = f.scheduler.debugInfo();
  f.advance(5000);
  assert.deepEqual(f.scheduler.debugInfo(), counts);
});

test("hidden main does not pause visible wrist; hidden page cancels all work", () => {
  const f = fixture(() => true);
  f.advance(200);
  const mainCount = f.frames.main.length;
  f.scheduler.setVisibility({ enabled: true, main: false, wrist: true });
  f.scheduler.invalidate({ pose: true });
  f.advance(1000);
  assert.equal(f.frames.main.length, mainCount);
  assert.ok(f.frames.wrist.length > 1);
  f.scheduler.setVisibility({ enabled: false, main: false, wrist: true });
  assert.equal(f.queued.size, 0);
  const counts = f.scheduler.debugInfo();
  f.scheduler.invalidate({ main: true, wrist: true, pose: true });
  f.advance(1000);
  assert.deepEqual(f.scheduler.debugInfo(), counts);
  f.scheduler.setVisibility({ enabled: true, main: true, wrist: true });
  f.advance(100);
  assert.ok(f.frames.main.length > mainCount);
});

test("both views offscreen cancel frames; dispose rejects late invalidation", () => {
  const f = fixture(() => true);
  f.scheduler.invalidate({ pose: true });
  f.scheduler.setVisibility({ enabled: true, main: false, wrist: false });
  assert.equal(f.queued.size, 0);
  f.scheduler.setVisibility({ enabled: true, main: true, wrist: false });
  assert.equal(f.queued.size, 1);
  f.scheduler.dispose();
  f.scheduler.invalidate({ main: true, wrist: true, pose: true });
  f.advance(1000);
  assert.equal(f.frames.main.length, 0);
  assert.equal(f.queued.size, 0);
});

test("time based pose smoothing does not depend on camera frequency", () => {
  function run(hz) {
    const display = { shoulder: 0 };
    const f = fixture((dt) => advancePose(display, { shoulder: 90 }, dt, () => {}));
    f.scheduler.invalidate({ pose: true });
    for (let i = 0; i < hz / 2; i++) {
      f.scheduler.invalidate({ main: true });
      f.advance(1000 / hz, hz);
    }
    return display.shoulder;
  }
  assert.ok(Math.abs(run(60) - run(120)) < 0.3);
});
