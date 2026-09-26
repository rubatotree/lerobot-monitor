// Pure rollout timeline geometry. Rendering stays in app.js so this module can
// be tested without DOM or Chart.js.

export const ROLLOUT_LANE_DEFAULTS = {
  windowS: 20,
  maxBlocks: 200,
  overlapMinS: 0.02,
  rows: 2,
};

function finite(value) {
  if (value == null || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function positive(value) {
  const number = finite(value);
  return number != null && number > 0 ? number : null;
}

function fallbackSteps(prediction) {
  const actions = prediction && Array.isArray(prediction.actions) ? prediction.actions.length : 0;
  return actions > 0 ? actions : null;
}

function optionsWithDefaults(options = {}) {
  return { ...ROLLOUT_LANE_DEFAULTS, ...options };
}

export function assignRows(spans, rows = ROLLOUT_LANE_DEFAULTS.rows) {
  const rowCount = Math.max(1, Math.floor(Number(rows) || 1));
  const ordered = [...(spans || [])]
    .filter((span) => span && finite(span.start) != null && finite(span.end) != null)
    .sort((left, right) => (
      finite(left.start) - finite(right.start)
      || Number(left.id || 0) - Number(right.id || 0)
    ));
  const rowEnds = new Array(rowCount).fill(Number.NEGATIVE_INFINITY);
  return ordered.map((span) => {
    const start = finite(span.start);
    const end = Math.max(start, finite(span.end));
    let row = rowEnds.findIndex((rowEnd) => start >= rowEnd);
    if (row < 0) {
      row = rowEnds.indexOf(Math.min(...rowEnds));
    }
    rowEnds[row] = end;
    return { ...span, start, end, row };
  });
}

export function chunkSpans(blocks, options = {}) {
  const opts = optionsWithDefaults(options);
  const fallback = fallbackSteps(opts.prediction);
  const spans = [];
  (blocks || []).forEach((block) => {
    const active = finite(block && block.active);
    if (active == null) return;
    const realSteps = positive(block && block.steps);
    const steps = realSteps || fallback;
    const stepS = positive(block && block.step_s)
      || positive(opts.prediction && opts.prediction.step_s)
      || positive(opts.stepS);
    if (steps == null || stepS == null) return;
    const duration = steps * stepS;
    if (!(duration > 0)) return;
    spans.push({
      id: block.id,
      kind: String(block.kind || ""),
      start: active,
      end: active + duration,
      steps,
      step_s: stepS,
      failed: !!block.failed,
      active,
      chunk: realSteps != null,
      fallback: realSteps == null,
    });
  });
  return assignRows(spans, opts.rows);
}

export function inferenceSpans(blocks) {
  return (blocks || [])
    .map((block) => {
      const start = finite(block && block.start);
      const end = finite(block && block.end);
      if (start == null || end == null || end < start) return null;
      return {
        id: block.id,
        kind: String(block.kind || ""),
        start,
        end,
        failed: !!block.failed,
      };
    })
    .filter(Boolean)
    .sort((left, right) => left.start - right.start || Number(left.id || 0) - Number(right.id || 0));
}

export function overlapSpans(spans, minS = ROLLOUT_LANE_DEFAULTS.overlapMinS) {
  const minimum = Math.max(0, Number(minS) || 0);
  const ordered = (spans || [])
    .filter((span) => span && finite(span.start) != null && finite(span.end) != null)
    .map((span) => ({
      ...span,
      start: finite(span.start),
      end: Math.max(finite(span.start), finite(span.end)),
      row: Math.max(0, Math.floor(Number(span.row) || 0)),
    }))
    .sort((left, right) => left.start - right.start || left.end - right.end);
  const raw = [];
  for (let leftIndex = 0; leftIndex < ordered.length; leftIndex += 1) {
    const left = ordered[leftIndex];
    for (let rightIndex = leftIndex + 1; rightIndex < ordered.length; rightIndex += 1) {
      const right = ordered[rightIndex];
      if (right.start >= left.end) break;
      const start = Math.max(left.start, right.start);
      const end = Math.min(left.end, right.end);
      if (end - start + 1e-9 < minimum) continue;
      raw.push({
        start,
        end,
        ids: [left.id, right.id],
        rows: [left.row, right.row],
      });
    }
  }
  raw.sort((left, right) => left.start - right.start || left.end - right.end);
  const merged = [];
  raw.forEach((item) => {
    const previous = merged[merged.length - 1];
    if (previous && item.start <= previous.end + 1e-9) {
      previous.end = Math.max(previous.end, item.end);
      previous.ids = [...new Set([...previous.ids, ...item.ids])];
      previous.rows = [...new Set([...previous.rows, ...item.rows])].sort((a, b) => a - b);
      return;
    }
    merged.push({ ...item });
  });
  return merged.map((item) => ({ ...item, duration: item.end - item.start }));
}

export function inputMarkers(blocks) {
  return (blocks || [])
    .map((block) => {
      const x = finite(block && block.active);
      if (x == null || positive(block && block.steps) == null || block.failed) return null;
      return {
        id: block.id,
        x,
        kind: String(block.kind || ""),
        steps: Number(block.steps),
      };
    })
    .filter(Boolean)
    .sort((left, right) => left.x - right.x || Number(left.id || 0) - Number(right.id || 0));
}

function limitBlocks(blocks, maxBlocks) {
  const limit = Math.max(1, Math.floor(Number(maxBlocks) || ROLLOUT_LANE_DEFAULTS.maxBlocks));
  if (blocks.length <= limit) return blocks;
  const active = blocks.filter((block) => finite(block && block.active) != null);
  const inactive = blocks.filter((block) => finite(block && block.active) == null);
  const inactiveRoom = Math.max(0, limit - active.length);
  const kept = [...active, ...inactive.slice(-inactiveRoom)];
  return kept.sort((left, right) => Number(left.id || 0) - Number(right.id || 0));
}

function cropSpans(spans, window) {
  return (spans || [])
    .map((span) => {
      const start = Math.max(window.start, finite(span.start));
      const end = Math.min(window.end, finite(span.end));
      if (!(end > start)) return null;
      return { ...span, start, end };
    })
    .filter(Boolean);
}

export function buildRolloutLanes({
  timeline,
  chartNow,
  prediction = null,
  windowS = ROLLOUT_LANE_DEFAULTS.windowS,
  lookaheadS = 0,
  maxBlocks = ROLLOUT_LANE_DEFAULTS.maxBlocks,
  overlapMinS = ROLLOUT_LANE_DEFAULTS.overlapMinS,
  rows = ROLLOUT_LANE_DEFAULTS.rows,
} = {}) {
  const now = finite(chartNow);
  const timelineTime = finite(timeline && timeline.t_s);
  const rawBlocks = timeline && Array.isArray(timeline.blocks) ? timeline.blocks : [];
  if (now == null || timelineTime == null || !rawBlocks.length) return null;
  const offset = now - timelineTime;
  const blocks = limitBlocks(rawBlocks, maxBlocks);
  const window = {
    start: now - Math.max(0, Number(windowS) || ROLLOUT_LANE_DEFAULTS.windowS),
    end: now + Math.max(0, Number(lookaheadS) || 0),
  };
  const chunks = cropSpans(
    chunkSpans(blocks, {
      rows,
      prediction,
      stepS: timeline.step_s,
    }).map((span) => ({
      ...span,
      start: span.start + offset,
      end: span.end + offset,
      active: span.active + offset,
    })),
    window,
  );
  const inferences = cropSpans(
    inferenceSpans(blocks).map((span) => ({
      ...span,
      start: span.start + offset,
      end: span.end + offset,
    })),
    window,
  );
  const inputs = inputMarkers(blocks)
    .map((marker) => ({ ...marker, x: marker.x + offset }))
    .filter((marker) => marker.x >= window.start && marker.x <= window.end);
  return {
    offset,
    window,
    rows: Math.max(1, Math.floor(Number(rows) || ROLLOUT_LANE_DEFAULTS.rows)),
    chunks,
    inferences,
    overlaps: overlapSpans(chunks, overlapMinS),
    inputs,
  };
}

if (typeof window !== "undefined") {
  window.RolloutLanes = {
    ROLLOUT_LANE_DEFAULTS,
    assignRows,
    buildRolloutLanes,
    chunkSpans,
    inferenceSpans,
    inputMarkers,
    overlapSpans,
  };
}
