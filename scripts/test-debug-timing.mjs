import assert from 'node:assert/strict';
import test from 'node:test';
import { DEBUG_TIMING_SEGMENTS, buildDebugTiming, formatTimingMs } from '../src/lerobot_monitor/web/static/debug-timing.js';

const row = (count) => Array.from({ length: count }, () => ({}));
const keys = (rows) => rows.map((entry) => entry.key);
const rowOf = (timing, key) => timing.detailRows.find((entry) => entry.key === key);
const barKeys = (timing) => timing.segments.map((segment) => segment.key);
const barSum = (timing) => timing.segments.reduce((total, segment) => total + segment.pct, 0);
const approx = (value, expected, tolerance = 0.01) => Math.abs(value - expected) < tolerance;

test('the bar walks the request legs in order and keeps the session leg out', () => {
  assert.deepEqual(
    DEBUG_TIMING_SEGMENTS.map((segment) => segment.key),
    ['encode', 'upload', 'compute', 'download', 'other', 'chunk', 'ghost'],
  );
  const timing = buildDebugTiming({
    fps: 30, latency_ms: 2000, model_wait_ms: 800, model_load_ms: 800, compute_ms: 900,
    actions: row(16), generated_steps: 50,
    stage_ms: { cloud_encode: 10, cloud_upload: 250, cloud_compute: 300, cloud_download: 20 },
  });
  assert.deepEqual(barKeys(timing), ['encode', 'upload', 'compute', 'download', 'other', 'chunk', 'ghost']);
  assert.ok(approx(barSum(timing), 100));
  assert.ok(approx(timing.segments[1].pct, 8.7209)); // upload 250 of 2866.67 ms
  assert.ok(approx(timing.segments[2].pct, 10.4651)); // compute 300 of 2866.67 ms
  assert.equal(timing.sessionMs, 800);
  assert.equal(timing.loadMs, 800);
  assert.equal(timing.waitMs, 0);
  assert.ok(timing.segments.every((segment) => segment.key !== 'load'));
});

test('inference excludes the session wait while compute stays GPU only', () => {
  const timing = buildDebugTiming({
    fps: 30, latency_ms: 2000, model_wait_ms: 800, model_load_ms: 800, compute_ms: 900,
    actions: row(16), generated_steps: 50,
    stage_ms: { cloud_encode: 10, cloud_upload: 250, cloud_compute: 300, cloud_download: 20 },
  });
  assert.equal(timing.inferenceMs, 1200);
  assert.equal(timing.computeMs, 300);
  assert.equal(timing.transferMs, 280);
  assert.equal(timing.otherMs, 620);
  assert.equal(timing.inferenceMs, timing.computeMs + timing.transferMs + timing.otherMs);
  assert.deepEqual(timing.chips.map((chip) => [chip.key, chip.text]), [
    ['inference', '1.20 s'],
    ['encode', '10 ms'],
    ['upload', '250 ms'],
    ['compute', '300 ms'],
    ['download', '20 ms'],
    ['other', '620 ms'],
    ['chunk', '533 ms'],
  ]);
  assert.match(timing.caption, /chunk 16 \/ 50 steps/);
  assert.match(timing.caption, /533 ms of 1\.67 s/);
});

test('detail rows walk load, inference, its legs, then the chunk', () => {
  const timing = buildDebugTiming({
    fps: 30, latency_ms: 2000, model_wait_ms: 800, model_load_ms: 800, compute_ms: 900,
    actions: row(16), generated_steps: 50,
    stage_ms: { cloud_encode: 10, cloud_upload: 250, cloud_compute: 300, cloud_download: 20 },
  });
  assert.deepEqual(keys(timing.detailRows), [
    'load', 'inference', 'encode', 'upload', 'compute', 'download', 'other', 'chunk', 'ghost',
  ]);
  assert.deepEqual(timing.detailRows.map((entry) => entry.depth), [0, 0, 1, 1, 1, 1, 1, 0, 0]);
  assert.ok(approx(rowOf(timing, 'load').pct, 27.907)); // 800 of 2866.67 ms
  assert.ok(approx(rowOf(timing, 'inference').pct, 41.860));
  assert.ok(approx(rowOf(timing, 'encode').pct, 0.349));
  assert.ok(approx(rowOf(timing, 'upload').pct, 8.721));
  assert.ok(approx(rowOf(timing, 'compute').pct, 10.465));
  assert.ok(approx(rowOf(timing, 'download').pct, 0.698));
  assert.ok(approx(rowOf(timing, 'other').pct, 21.628));
  assert.ok(approx(rowOf(timing, 'chunk').pct, 18.605));
  assert.ok(approx(rowOf(timing, 'ghost').pct, 39.535));
  assert.equal(rowOf(timing, 'compute').detail, 'gpu');
  assert.equal(rowOf(timing, 'inference').detail, 'excludes load');
  assert.equal(rowOf(timing, 'load').detail, 'session');
  assert.equal(rowOf(timing, 'load').text, '800 ms');
  const children = timing.detailRows.filter((entry) => entry.depth === 1);
  assert.ok(approx(children.reduce((total, entry) => total + entry.pct, 0), rowOf(timing, 'inference').pct));
  assert.equal(rowOf(timing, 'chunk').detail, '16 / 50 steps · 30 fps');
  assert.equal(rowOf(timing, 'ghost').detail, '34 more steps');
});

test('local runs keep the session row and have no transfer legs', () => {
  const timing = buildDebugTiming({
    fps: 30, latency_ms: 400, model_wait_ms: 30, model_load_ms: 0, compute_ms: 350, actions: row(4),
  });
  assert.deepEqual(barKeys(timing), ['compute', 'other', 'chunk']);
  assert.equal(timing.inferenceMs, 370);
  assert.equal(timing.computeMs, 350);
  assert.equal(timing.transferMs, 0);
  assert.equal(timing.otherMs, 20);
  assert.equal(timing.sessionMs, 30);
  assert.equal(timing.loadMs, 0);
  assert.equal(timing.waitMs, 30);
  assert.deepEqual(keys(timing.detailRows), ['load', 'inference', 'compute', 'other', 'chunk']);
  assert.equal(rowOf(timing, 'load').text, '30 ms');
  assert.deepEqual(timing.chips.map((chip) => [chip.key, chip.text]), [
    ['inference', '370 ms'], ['compute', '350 ms'], ['other', '20 ms'], ['chunk', '133 ms'],
  ]);
});

test('an inflated compute measurement is clamped inside the request window', () => {
  const timing = buildDebugTiming({
    latency_ms: 1000, model_wait_ms: 400, model_load_ms: 400, compute_ms: 950, actions: row(2),
  });
  assert.equal(timing.inferenceMs, 600);
  assert.equal(timing.computeMs, 600);
  assert.equal(timing.otherMs, 0);
  assert.deepEqual(barKeys(timing), ['compute']);
  assert.deepEqual(keys(timing.detailRows), ['load', 'inference', 'compute', 'chunk']);
});

test('missing fps reports steps without inventing a duration', () => {
  const timing = buildDebugTiming({ latency_ms: 100, compute_ms: 100, actions: row(3) });
  assert.deepEqual(barKeys(timing), ['compute']);
  assert.equal(timing.fps, null);
  assert.equal(rowOf(timing, 'chunk').text, '—');
  assert.equal(rowOf(timing, 'chunk').detail, '3 steps');
  const explicit = buildDebugTiming({ latency_ms: 100, compute_ms: 100, steps: 9, actions: row(3) });
  assert.equal(rowOf(explicit, 'chunk').detail, '9 steps');
});

test('a chunk that was not truncated has no hatched tail', () => {
  const timing = buildDebugTiming({
    latency_ms: 200, compute_ms: 200, fps: 20, actions: row(12), generated_steps: 12,
  });
  assert.equal(timing.truncated, false);
  assert.deepEqual(barKeys(timing), ['compute', 'chunk']);
  assert.equal(timing.chunkMs, 600);
});

test('missing timings hide the whole bar', () => {
  const timing = buildDebugTiming({});
  assert.equal(timing.hasTiming, false);
  assert.deepEqual(timing.segments, []);
  assert.equal(buildDebugTiming({ actions: row(4) }).hasTiming, false);
});

test('cloud legs report their own shares over the round trip', () => {
  const timing = buildDebugTiming({
    latency_ms: 10, compute_ms: 10, actions: row(2),
    stage_ms: { cloud_encode: 10, cloud_upload: 250, cloud_compute: 250, cloud_download: 20 },
  });
  assert.equal(timing.stageTotalMs, 530);
  assert.deepEqual(keys(timing.stageRows), ['cloud_encode', 'cloud_upload', 'cloud_compute', 'cloud_download']);
  assert.ok(approx(timing.stageRows.reduce((total, entry) => total + entry.pct, 0), 100));
  assert.equal(timing.stageRows[1].text, '250 ms');
  const empty = buildDebugTiming({ latency_ms: 10, compute_ms: 10, actions: row(2) });
  assert.deepEqual(empty.stageRows, []);
  assert.equal(empty.stageTotalMs, 0);
});

test('per-joint rows list the worst normalised error first', () => {
  const timing = buildDebugTiming({
    latency_ms: 10, compute_ms: 10, actions: row(4),
    evaluation: {
      score: 61.2, steps: 4, predicted_steps: 4, coverage: 1,
      joints: {
        gripper: { mae: 0.1, rmse: 0.2, nrmse: 0.02 },
        shoulder_pan: { mae: 1, rmse: 2, nrmse: 0.2 },
      },
    },
  });
  assert.deepEqual(keys(timing.jointRows), ['shoulder_pan', 'gripper']);
  assert.deepEqual(timing.jointRows[0], { key: 'shoulder_pan', mae: 1, rmse: 2, nrmse: 0.2 });
  assert.equal(timing.evaluationSummary.score, 61.2);
  assert.equal(timing.evaluationSummary.predictedSteps, 4);
  const none = buildDebugTiming({ latency_ms: 10, compute_ms: 10, actions: row(4) });
  assert.deepEqual(none.jointRows, []);
  assert.equal(none.evaluationSummary, null);
});

test('durations format as ms under a second and seconds above', () => {
  assert.equal(formatTimingMs(320), '320 ms');
  assert.equal(formatTimingMs(1600.4), '1.60 s');
  assert.equal(formatTimingMs(0), '0 ms');
  assert.equal(formatTimingMs(null), '—');
  assert.equal(formatTimingMs(Number.NaN), '—');
  assert.equal(formatTimingMs(-5), '—');
});
