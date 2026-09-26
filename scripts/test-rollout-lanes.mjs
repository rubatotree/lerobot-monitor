import assert from "node:assert/strict";
import test from "node:test";

import {
  ROLLOUT_LANE_DEFAULTS,
  assignRows,
  buildRolloutLanes,
  chunkSpans,
  inferenceSpans,
  inputMarkers,
  overlapSpans,
} from "../src/lerobot_monitor/web/static/rollout-lanes.js";

test("chunk spans use handoff time, duration, two rows and overlap hatching", () => {
  const blocks = [
    { id: 1, kind: "rtc", active: 1.0, steps: 4, step_s: 0.1, failed: false },
    { id: 2, kind: "rtc", active: 1.2, steps: 2, step_s: 0.1, failed: false },
  ];

  const chunks = chunkSpans(blocks);

  assert.deepEqual(chunks.map(({ id, row, start, end, chunk }) => ({ id, row, start, end, chunk })), [
    { id: 1, row: 0, start: 1, end: 1.4, chunk: true },
    { id: 2, row: 1, start: 1.2, end: 1.4, chunk: true },
  ]);
  const overlaps = overlapSpans(chunks);
  assert.equal(overlaps.length, 1);
  assert.deepEqual(overlaps[0].ids, [1, 2]);
  assert.deepEqual(overlaps[0].rows, [0, 1]);
  assert.ok(Math.abs(overlaps[0].duration - 0.2) < 1e-9);
  assert.deepEqual(inputMarkers(blocks).map((marker) => marker.x), [1, 1.2]);
});

test("missing steps falls back to prediction length but never fabricates an input marker", () => {
  const blocks = [
    { id: 3, kind: "sync", active: 0.5, steps: null, step_s: null, failed: false },
  ];
  const prediction = { step_s: 0.2, actions: [{}, {}, {}] };

  const chunks = chunkSpans(blocks, { prediction, stepS: 0.05 });

  assert.equal(chunks.length, 1);
  assert.equal(chunks[0].steps, 3);
  assert.equal(chunks[0].step_s, 0.2);
  assert.equal(chunks[0].chunk, false);
  assert.equal(chunks[0].fallback, true);
  assert.deepEqual(inputMarkers(blocks), []);
});

test("row assignment wraps when more overlaps exist than rows", () => {
  const spans = [
    { id: 1, start: 0, end: 3 },
    { id: 2, start: 1, end: 4 },
    { id: 3, start: 2, end: 5 },
    { id: 4, start: 6, end: 7 },
  ];

  const rows = assignRows(spans, 2);

  assert.deepEqual(rows.map((span) => [span.id, span.row]), [
    [1, 0],
    [2, 1],
    [3, 0],
    [4, 0],
  ]);
});

test("inference spans preserve failures and reject malformed ranges", () => {
  const spans = inferenceSpans([
    { id: 1, start: 1, end: 1.3, failed: false },
    { id: 2, start: 2, end: 1.9, failed: true },
    { id: 3, start: null, end: 3, failed: false },
  ]);

  assert.deepEqual(spans, [
    { id: 1, kind: "", start: 1, end: 1.3, failed: false },
  ]);
});

test("build maps task-relative time and crops outside the chart window", () => {
  const timeline = {
    t_s: 5,
    step_s: 0.1,
    blocks: [
      { id: 1, kind: "rtc", start: 1.0, end: 1.2, active: 2.0, steps: 4, step_s: 0.1, failed: false },
      { id: 2, kind: "rtc", start: 3.0, end: 3.2, active: 4.0, steps: 2, step_s: 0.1, failed: false },
    ],
  };

  const lanes = buildRolloutLanes({
    timeline,
    chartNow: 10,
    windowS: 2,
  });

  assert.ok(lanes);
  assert.equal(lanes.offset, 5);
  assert.deepEqual(lanes.window, { start: 8, end: 10 });
  assert.deepEqual(lanes.chunks.map((chunk) => chunk.id), [2]);
  assert.deepEqual(lanes.inferences.map((span) => span.id), [2]);
  assert.deepEqual(lanes.inputs.map((marker) => marker.x), [9]);
});

test("max block trimming keeps active chunks and defaults stay bounded", () => {
  const timeline = {
    t_s: 4,
    step_s: 0.1,
    blocks: [
      { id: 1, kind: "rtc", start: 0, end: 0.1, active: 0.1, steps: 1, step_s: 0.1, failed: false },
      { id: 2, kind: "rtc", start: 1, end: 1.1, steps: 1, step_s: 0.1, failed: false },
      { id: 3, kind: "rtc", start: 2, end: 2.1, steps: 1, step_s: 0.1, failed: false },
      { id: 4, kind: "rtc", start: 3, end: 3.1, active: 3.1, steps: 1, step_s: 0.1, failed: false },
      { id: 5, kind: "rtc", start: 4, end: 4.1, steps: 1, step_s: 0.1, failed: false },
    ],
  };

  const lanes = buildRolloutLanes({
    timeline,
    chartNow: 4,
    windowS: 20,
    maxBlocks: 2,
  });

  assert.equal(ROLLOUT_LANE_DEFAULTS.maxBlocks, 200);
  assert.ok(lanes);
  assert.deepEqual(lanes.chunks.map((chunk) => chunk.id), [1, 4]);
});
