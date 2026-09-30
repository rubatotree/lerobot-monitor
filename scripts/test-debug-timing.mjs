import assert from 'node:assert/strict';
import test from 'node:test';
import { buildDebugTiming, formatTimingMs } from '../src/lerobot_monitor/web/static/debug-timing.js';

const row = (count) => Array.from({ length: count }, () => ({}));
const kinds = (timing) => timing.segments.map((segment) => segment.key);
const msOf = (timing, key) => timing.segments.find((segment) => segment.key === key)?.ms ?? 0;
const pctSum = (timing) => timing.segments.reduce((total, segment) => total + segment.pct, 0);

test('cold cloud chunk keeps transfer, compute and the would-be tail apart', () => {
  const timing = buildDebugTiming({
    fps: 30, latency_ms: 1600, model_wait_ms: 1250, model_load_ms: 1250,
    compute_ms: 300, actions: row(16), generated_steps: 50,
  });
  assert.equal(timing.hasTiming, true);
  assert.deepEqual(kinds(timing), ['load', 'compute', 'other', 'chunk', 'ghost']);
  assert.equal(msOf(timing, 'load'), 1250);
  assert.equal(msOf(timing, 'compute'), 300);
  assert.equal(msOf(timing, 'other'), 50);
  assert.ok(Math.abs(msOf(timing, 'chunk') - 533.333) < 0.01);
  assert.ok(Math.abs(timing.fullMs - 1666.667) < 0.01);
  assert.ok(Math.abs(pctSum(timing) - 100) < 0.01);
  assert.equal(timing.truncated, true);
  assert.equal(timing.generatedSteps, 50);
  assert.ok(Math.abs(msOf(timing, 'ghost') / timing.totalMs * 100 - 34.69) < 0.1);
  assert.match(timing.caption, /chunk 16 \/ 50 steps/);
  assert.match(timing.caption, /533 ms of 1\.67 s/);
  assert.match(timing.caption, /@ 30 fps/);
  assert.deepEqual(timing.chips.map((chip) => [chip.key, chip.text]), [
    ['inference', '1.60 s'], ['load', '1.25 s'], ['compute', '300 ms'], ['other', '50 ms'],
  ]);
});

test('resident model separates wait from compute and drops the empty load leg', () => {
  const timing = buildDebugTiming({
    fps: 30, latency_ms: 400, model_wait_ms: 30, model_load_ms: 0,
    compute_ms: 350, actions: row(4),
  });
  assert.deepEqual(kinds(timing), ['wait', 'compute', 'other', 'chunk']);
  assert.equal(msOf(timing, 'wait'), 30);
  assert.equal(msOf(timing, 'other'), 20);
  assert.equal(timing.truncated, false);
  assert.equal(timing.generatedSteps, null);
  assert.match(timing.caption, /chunk 4 steps/);
  assert.match(timing.caption, /133 ms/);
});

test('measurement overshoot is clamped so the legs always add up to latency', () => {
  const timing = buildDebugTiming({ latency_ms: 100, model_wait_ms: 80, compute_ms: 500, actions: row(2), fps: 10 });
  const time = (timing.segments || []).filter((segment) => segment.kind === 'time');
  assert.equal(time.reduce((total, segment) => total + segment.ms, 0), 100);
  assert.equal(msOf(timing, 'compute'), 20);
  assert.equal(msOf(timing, 'other'), 0);
});

test('missing fps reports steps without inventing a duration', () => {
  const timing = buildDebugTiming({ latency_ms: 100, compute_ms: 100, actions: row(3) });
  assert.deepEqual(kinds(timing), ['compute']);
  assert.equal(timing.fps, null);
  assert.equal(timing.caption, 'chunk 3 steps');
  const explicit = buildDebugTiming({ latency_ms: 100, compute_ms: 100, steps: 9, actions: row(3) });
  assert.match(explicit.caption, /chunk 9 steps/);
});

test('a chunk that was not truncated has no hatched tail', () => {
  const timing = buildDebugTiming({ latency_ms: 200, compute_ms: 200, fps: 20, actions: row(12), generated_steps: 12 });
  assert.equal(timing.truncated, false);
  assert.equal(kinds(timing).includes('ghost'), false);
  assert.equal(msOf(timing, 'chunk'), 600);
});

test('missing timings hide the whole bar', () => {
  const timing = buildDebugTiming({});
  assert.equal(timing.hasTiming, false);
  assert.deepEqual(timing.segments, []);
  assert.equal(buildDebugTiming({ actions: row(4) }).hasTiming, false);
});

test('durations format as ms under a second and seconds above', () => {
  assert.equal(formatTimingMs(320), '320 ms');
  assert.equal(formatTimingMs(1600.4), '1.60 s');
  assert.equal(formatTimingMs(0), '0 ms');
  assert.equal(formatTimingMs(null), '—');
  assert.equal(formatTimingMs(Number.NaN), '—');
  assert.equal(formatTimingMs(-5), '—');
});

const rowKeys = (rows) => rows.map((row) => row.key);

test('timing detail rows share the bar denominator', () => {
  const timing = buildDebugTiming({
    fps: 30, latency_ms: 1600, model_wait_ms: 1250, model_load_ms: 1250,
    compute_ms: 300, actions: row(16), generated_steps: 50,
  });
  assert.deepEqual(rowKeys(timing.detailRows), ['load', 'compute', 'other', 'inference', 'chunk', 'ghost']);
  const perRow = (key) => timing.detailRows.find((entry) => entry.key === key);
  assert.ok(Math.abs(perRow('inference').pct - 48.98) < 0.01);
  const legs = timing.detailRows.filter((entry) => ['wait', 'load', 'compute', 'other'].includes(entry.key));
  assert.ok(Math.abs(legs.reduce((total, entry) => total + entry.pct, 0) - perRow('inference').pct) < 1e-6);
  assert.equal(perRow('load').text, '1.25 s');
  assert.equal(perRow('chunk').text, '533 ms');
  assert.equal(perRow('chunk').detail, '16 / 50 steps · 30 fps');
  assert.equal(perRow('ghost').detail, '34 more steps');
});

test('detail rows without fps keep the steps and drop the would-be tail', () => {
  const timing = buildDebugTiming({ latency_ms: 100, compute_ms: 100, actions: row(3) });
  assert.deepEqual(rowKeys(timing.detailRows), ['compute', 'inference', 'chunk']);
  const chunk = timing.detailRows.find((entry) => entry.key === 'chunk');
  assert.equal(chunk.text, '—');
  assert.equal(chunk.pct, 0);
  assert.equal(chunk.detail, '3 steps');
});

test('cloud legs report their own shares over the round trip', () => {
  const timing = buildDebugTiming({
    latency_ms: 10, compute_ms: 10, actions: row(2),
    stage_ms: { cloud_encode: 10, cloud_upload: 250, cloud_compute: 250, cloud_download: 20 },
  });
  assert.equal(timing.stageTotalMs, 530);
  assert.deepEqual(rowKeys(timing.stageRows), ['cloud_encode', 'cloud_upload', 'cloud_compute', 'cloud_download']);
  assert.ok(Math.abs(timing.stageRows.reduce((total, entry) => total + entry.pct, 0) - 100) < 0.01);
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
  assert.deepEqual(rowKeys(timing.jointRows), ['shoulder_pan', 'gripper']);
  assert.deepEqual(timing.jointRows[0], { key: 'shoulder_pan', mae: 1, rmse: 2, nrmse: 0.2 });
  assert.equal(timing.evaluationSummary.score, 61.2);
  assert.equal(timing.evaluationSummary.predictedSteps, 4);
  const none = buildDebugTiming({ latency_ms: 10, compute_ms: 10, actions: row(4) });
  assert.deepEqual(none.jointRows, []);
  assert.equal(none.evaluationSummary, null);
});
