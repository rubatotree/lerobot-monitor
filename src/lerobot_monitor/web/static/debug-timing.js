// Pure timing math for the Debug panel's timeline bar; no DOM access.
//
// One bar walks the request the way the wall clock does: the observation legs
// (encode/upload/GPU compute/download), then the client-side chores, then the action
// chunk's own play time and — when the request truncated the chunk — a hatched tail
// for the steps the policy would also have produced. Opening the session (deploy /
// model load) happens before the request and never enters the bar; it keeps its own
// row above inference in the detail table.
export const DEBUG_TIMING_SEGMENTS = [
  { key: "encode", label: "encode", kind: "time" },
  { key: "upload", label: "upload", kind: "time" },
  { key: "compute", label: "compute", kind: "time" },
  { key: "download", label: "download", kind: "time" },
  { key: "other", label: "other", kind: "time" },
  { key: "chunk", label: "chunk", kind: "chunk" },
  { key: "ghost", label: "would-be", kind: "ghost" },
];

export function formatTimingMs(value) {
  if (value == null || value === "") return "—";
  const ms = Number(value);
  if (!Number.isFinite(ms) || ms < 0) return "—";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`;
}

export function buildDebugTiming(result = {}) {
  const num = (value) => {
    const n = Number(value);
    return Number.isFinite(n) && n > 0 ? n : 0;
  };
  const actions = Array.isArray(result.actions) ? result.actions.length : 0;
  const explicit = Number(result.steps);
  const steps = Number.isFinite(explicit) && explicit > 0 ? Math.round(explicit) : actions;
  const generated = Math.max(0, Math.round(num(result.generated_steps)));
  // Only a chunk that was actually cut short earns the hatched tail.
  const generatedSteps = generated > steps ? generated : null;
  const fps = num(result.fps);
  const latency = num(result.latency_ms);
  // Opening the session (deploy / model load) precedes the request: it is measured on
  // its own and subtracted, so it can never inflate the inference number.
  const sessionMs = Math.min(num(result.model_wait_ms), latency);
  const load = Math.min(num(result.model_load_ms), sessionMs);
  const wait = sessionMs - load;
  const inference = Math.max(0, latency - sessionMs);
  const stageMs = result.stage_ms && typeof result.stage_ms === "object" ? result.stage_ms : {};
  const stage = (name) => num(stageMs[name]);
  const encode = stage("cloud_encode");
  const upload = stage("cloud_upload");
  const download = stage("cloud_download");
  const transfer = encode + upload + download;
  // GPU only: the cloud compute leg when the session reported stages, otherwise the
  // locally measured compute window. Clamped so the parts can never exceed the request.
  const compute = Math.min(
    stage("cloud_compute") || num(result.compute_ms),
    Math.max(0, inference - transfer),
  );
  const other = Math.max(0, inference - transfer - compute);
  const chunkMs = fps > 0 ? (steps / fps) * 1000 : 0;
  const ghostMs = fps > 0 && generatedSteps != null ? ((generatedSteps - steps) / fps) * 1000 : 0;
  const fullMs = chunkMs + ghostMs;
  const barMs = inference + fullMs;
  const shareOf = (base, value) => (base > 0 ? (value / base) * 100 : 0);
  const segmentMs = { encode, upload, compute, download, other, chunk: chunkMs, ghost: ghostMs };
  const segments = DEBUG_TIMING_SEGMENTS
    .map((segment) => ({ ...segment, ms: segmentMs[segment.key] }))
    .filter((segment) => segment.ms > 0)
    .map((segment) => ({ ...segment, pct: shareOf(barMs, segment.ms) }));
  const caption = [`chunk ${steps}${generatedSteps != null ? ` / ${generatedSteps}` : ""} steps`];
  if (chunkMs > 0) {
    caption.push(
      generatedSteps != null
        ? `${formatTimingMs(chunkMs)} of ${formatTimingMs(fullMs)}`
        : formatTimingMs(chunkMs),
    );
  }
  if (fps > 0) caption.push(`@ ${Math.round(fps)} fps`);
  const stageRows = Object.entries(stageMs)
    .map(([key, value]) => ({ key, ms: num(value) }))
    .filter((row) => row.ms > 0);
  const stageTotalMs = stageRows.reduce((total, row) => total + row.ms, 0);
  const jointSource = result.evaluation && result.evaluation.joints ? result.evaluation.joints : {};
  const jointRows = Object.entries(jointSource)
    .map(([key, value]) => ({
      key,
      mae: Number(value?.mae) || 0,
      rmse: Number(value?.rmse) || 0,
      nrmse: Number(value?.nrmse) || 0,
    }))
    .sort((left, right) => right.nrmse - left.nrmse);
  return {
    hasTiming: latency > 0 && segments.length > 0,
    barMs,
    latencyMs: latency,
    // Headline numbers: inference excludes the session/deploy wait; compute is GPU only.
    inferenceMs: inference,
    computeMs: compute,
    transferMs: transfer,
    otherMs: other,
    sessionMs,
    loadMs: load,
    waitMs: wait,
    steps,
    generatedSteps,
    fps: fps > 0 ? fps : null,
    chunkMs,
    fullMs,
    truncated: generatedSteps != null,
    segments,
    chips: [
      { key: "inference", label: "inference", text: formatTimingMs(inference) },
      ...segments
        .filter((segment) => segment.kind === "time")
        .map((segment) => ({
          key: segment.key,
          label: segment.key === "compute" ? "gpu" : segment.label,
          text: formatTimingMs(segment.ms),
        })),
      { key: "chunk", label: "chunk", text: chunkMs > 0 ? formatTimingMs(chunkMs) : "—" },
    ],
    caption: caption.join(" · "),
    // Rows walk the wall clock: session open, the request, its legs, then the chunk's
    // play time. Every row's share uses the bar's own denominator, so a row tint lines
    // up with the segment above it.
    detailRows: [
      ...(sessionMs > 0
        ? [{ key: "load", depth: 0, text: formatTimingMs(sessionMs), ms: sessionMs, detail: "session", pct: shareOf(barMs, sessionMs) }]
        : []),
      ...(inference > 0
        ? [{ key: "inference", depth: 0, text: formatTimingMs(inference), ms: inference, detail: "excludes load", pct: shareOf(barMs, inference) }]
        : []),
      ...segments
        .filter((segment) => segment.kind === "time")
        .map((segment) => ({
          key: segment.key,
          depth: 1,
          text: formatTimingMs(segment.ms),
          ms: segment.ms,
          detail: segment.key === "compute" ? "gpu" : "",
          pct: segment.pct,
        })),
      {
        key: "chunk",
        depth: 0,
        text: chunkMs > 0 ? formatTimingMs(chunkMs) : "—",
        ms: chunkMs,
        detail: `${steps}${generatedSteps != null ? ` / ${generatedSteps}` : ""} steps${fps > 0 ? ` · ${Math.round(fps)} fps` : ""}`,
        pct: shareOf(barMs, chunkMs),
      },
      ...(generatedSteps != null
        ? [{ key: "ghost", depth: 0, text: formatTimingMs(ghostMs), ms: ghostMs, detail: `${generatedSteps - steps} more steps`, pct: shareOf(barMs, ghostMs) }]
        : []),
    ],
    // Cloud legs keep their raw stage names; the renderer maps them to enc/up/gpu/down
    // and paints the matching dot colour. Shares are over the round trip they partition.
    stageRows: stageRows.map((row) => ({
      key: row.key,
      text: formatTimingMs(row.ms),
      ms: row.ms,
      pct: stageTotalMs > 0 ? (row.ms / stageTotalMs) * 100 : 0,
    })),
    stageTotalMs,
    jointRows,
    evaluationSummary: result.evaluation
      ? {
          score: Number(result.evaluation.score) || 0,
          steps: Number(result.evaluation.steps) || 0,
          predictedSteps: Number(result.evaluation.predicted_steps) || steps,
          coverage: Number(result.evaluation.coverage) || 0,
        }
      : null,
  };
}

if (typeof window !== "undefined") {
  window.DebugTiming = { DEBUG_TIMING_SEGMENTS, buildDebugTiming, formatTimingMs };
}
