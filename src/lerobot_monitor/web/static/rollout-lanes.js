// Pure time and pixel geometry; all phases retain uncropped source times.
export const ROLLOUT_LANE_DEFAULTS = { windowS: 20, maxBlocks: 200, overlapMinS: 0.02, rows: 1 };
const finite = (v) => v == null || v === '' || !Number.isFinite(Number(v)) ? null : Number(v);
const positive = (v) => finite(v) > 0 ? Number(v) : null;
export const formatDuration = (seconds) => seconds == null || !Number.isFinite(seconds) ? '—' : seconds < 1 ? `${Math.round(Math.max(0, seconds) * 1000)} ms` : `${Math.max(0, seconds).toFixed(2)} s`;
// Retained export for older callers: overlap never allocates additional rows.
export function assignRows(spans) {
  return (spans || []).filter(s => finite(s.start) != null && finite(s.end) != null)
    .map(s => ({ ...s, row: 0 })).sort((a, b) => a.start - b.start || Number(a.id) - Number(b.id));
}
export function chunkSpans(blocks, options = {}) {
  return assignRows((blocks || []).flatMap(block => {
    const active = finite(block.active);
    const realSteps = finite(block.accepted_steps) ?? finite(block.steps);
    const steps = realSteps ?? options.prediction?.actions?.length;
    const step = positive(block.step_s) || positive(options.prediction?.step_s) || positive(options.stepS);
    if (active == null || !(steps > 0) || !step || block.failed) return [];
    return [{ ...block, start: active, end: active + steps * step, active, steps, step_s: step,
      chunk: realSteps != null, fallback: realSteps == null }];
  }));
}
export function inferenceSpans(blocks, now = null) {
  return (blocks || []).flatMap(block => {
    const start = finite(block.start), end = finite(block.end) ?? finite(now);
    return start == null || end == null || end < start ? [] : [{ ...block, start, end }];
  }).sort((a, b) => a.start - b.start || Number(a.id) - Number(b.id));
}
export function inputMarkers(blocks) {
  return chunkSpans(blocks).filter(s => s.chunk).map(s => ({ id: s.id, x: s.active, kind: s.kind, steps: s.steps }));
}
// Sweep exact coverage intervals: pairwise unions falsely report triple coverage.
export function overlapSpans(spans, minS = 0.02) {
  const points = [...new Set(spans.flatMap(s => [s.start, s.end]))].sort((a, b) => a - b);
  const overlaps = [];
  for (let i = 0; i + 1 < points.length; i++) {
    const start = points[i], end = points[i + 1];
    const ids = spans.filter(s => s.start < end && s.end > start).map(s => s.id);
    if (ids.length < 2 || end - start + 1e-9 < minS) continue;
    overlaps.push({ start, end, duration: end - start, ids, rows: [0], count: ids.length });
  }
  return overlaps;
}
export function buildRolloutLanes({ timeline, chartNow, epoch = null, prediction = null,
  windowS = 20, lookaheadS = 0, maxBlocks = 200, overlapMinS = 0.02 } = {}) {
  const now = finite(chartNow), offset = finite(timeline?.epoch_ts) ?? finite(epoch);
  if (now == null || offset == null || !timeline?.blocks?.length) return null;
  const window = { start: now - windowS, end: now + lookaheadS };
  const mapTime = v => finite(v) == null ? null : Number(v) + offset;
  const blocks = timeline.blocks.slice(-Math.max(1, maxBlocks)).map(block => ({ ...block,
    start: mapTime(block.start), end: mapTime(block.end), active: mapTime(block.active),
    accepted_at: mapTime(block.accepted_at), action_end: mapTime(block.action_end),
    last_dispatched: mapTime(block.last_dispatched), replaced_at: mapTime(block.replaced_at),
    stages: (block.stages || []).map(stage => ({ ...stage, start: mapTime(stage.start), end: mapTime(stage.end) })),
  }));
  const visible = span => span.end >= window.start && span.start <= window.end;
  const chunks = chunkSpans(blocks, { prediction, stepS: timeline.step_s }).filter(visible);
  const inferences = inferenceSpans(blocks, now).filter(visible);
  const ribbons = blocks.flatMap(block => {
    const inference = inferenceSpans([block], now)[0];
    const action = chunkSpans([block], { prediction, stepS: timeline.step_s })[0] || null;
    if (!inference) return [];
    const end = action?.end ?? block.action_end ?? inference.end;
    if (!visible({ start: inference.start, end })) return [];
    return [{ id: block.id, block, inference, action, end }];
  });
  return { offset, now, window, rows: 1, ribbons, chunks, inferences,
    overlaps: overlapSpans(chunks, overlapMinS), inputs: inputMarkers(blocks).filter(m => m.x >= window.start && m.x <= window.end) };
}
export function ribbonSegments(lanes, pixelForTime, top, visibility = {}) {
  const segments = [];
  const push = (ribbon, phase, start, end, y1, y2, extra = {}) => {
    if (end < lanes.window.start || start > lanes.window.end || end < start) return;
    const from = Math.max(start, lanes.window.start), to = Math.min(end, lanes.window.end);
    const ratio = t => end > start ? (t - start) / (end - start) : 0;
    segments.push({ ribbon, phase, start, end, x1: pixelForTime(from), x2: pixelForTime(to),
      y1: top + y1 + (y2-y1)*ratio(from), y2: top + y1 + (y2-y1)*ratio(to), ...extra });
  };
  for (const ribbon of lanes?.ribbons || []) {
    const inf = ribbon.inference, action = ribbon.action;
    if (visibility.inference !== false) {
      if (visibility.inferenceStages && ribbon.block.stages?.length) {
        // Base inference preserves gaps between recorded stages and unfinished work.
        push(ribbon, 'inference', inf.start, inf.end, 58, 36);
        for (const stage of ribbon.block.stages) {
          const end = stage.end ?? Math.min(lanes.now, inf.end);
          const y = t => 58 - 22 * Math.min(1, Math.max(0, (t-inf.start)/Math.max(1e-9, inf.end-inf.start)));
          push(ribbon, 'inference', stage.start, end, y(stage.start), y(end), { stage: stage.name });
        }
      } else push(ribbon, 'inference', inf.start, inf.end, 58, 36);
    }
    if (action && visibility.chunkSpan !== false) {
      if (visibility.inference !== false) push(ribbon, 'wait', inf.end, action.start, 36, 28);
      // Queue replacement is earlier than control handoff: only actual end closes execution.
      const tail = finite(ribbon.block.action_end);
      const end = Math.max(action.start, Math.min(action.end, tail ?? action.end));
      const elapsed = Math.max(action.start, Math.min(end, lanes.now));
      const y = t => 28 - 22 * (t - action.start) / (action.end - action.start);
      push(ribbon, 'action', action.start, elapsed, 28, y(elapsed));
      if (elapsed < end) push(ribbon, 'planned', elapsed, end, y(elapsed), y(end));
      if (end < action.end) push(ribbon, 'replaced', end, action.end, y(end), 6);
    } else if (!action && !ribbon.block.failed && ['inferring', 'waiting'].includes(ribbon.block.status) && visibility.inference !== false) {
      push(ribbon, 'wait', inf.end, Math.max(inf.end, lanes.now), 36, 28);
    }
  }
  return segments;
}
export function hitTestRibbon(segments, x, y, tolerance = 7) {
  let hit = null, best = tolerance * tolerance;
  for (const segment of segments || []) {
    const dx = segment.x2 - segment.x1, dy = segment.y2 - segment.y1;
    const t = Math.min(1, Math.max(0, ((x-segment.x1)*dx + (y-segment.y1)*dy) / (dx*dx+dy*dy || 1)));
    const distance = (x-segment.x1-t*dx)**2 + (y-segment.y1-t*dy)**2;
    if (distance <= best + 1e-9) { hit = segment; best = distance; }
  }
  return hit;
}
export function ribbonAnalysis(ribbon, now) {
  const b = ribbon.block;
  const terminal = ['completed','replaced','discarded','failed','stopped'].includes(b.status) || b.failed;
  // A replaced chunk may still have one already-consumed goal awaiting control handoff.
  const running = !terminal || (b.status === 'replaced' && b.active != null && b.action_end == null);
  const end = b.action_end ?? (running ? now : b.replaced_at ?? b.accepted_at ?? b.end ?? now);
  const waitEnd = b.active ?? (running ? now : end);
  return { total: Math.max(0, end - b.start),
    queueWait: b.end == null ? null : Math.max(0, waitEnd - b.end),
    plan: Math.max(0, Number(b.accepted_steps ?? b.steps ?? 0) * Number(b.step_s || 0)),
    actionElapsed: b.active == null ? null : Math.max(0, (b.action_end ?? now) - b.active) };
}
export function snapshotDisplay(value) { return structuredClone(value); }
if (typeof window !== 'undefined') window.RolloutLanes = { ROLLOUT_LANE_DEFAULTS, assignRows, buildRolloutLanes,
  chunkSpans, inferenceSpans, inputMarkers, overlapSpans, ribbonSegments, hitTestRibbon, formatDuration, ribbonAnalysis, snapshotDisplay };
