// Pure timing math for the Debug panel's inference bar; no DOM access.
//
// The bar carries model execution only: GPU compute, then the action chunk's own play
// time, then — when the request truncated the chunk — a hatched tail for the steps the
// policy would also have produced. Session/deploy wait and the transfer legs are never
// charged to inference here; they stay in the detail rows below the bar.
export const DEBUG_TIMING_SEGMENTS = [
  { key: "compute", label: "compute", kind: "time" },
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
  // Opening the session (which may include the model load) precedes the request: it is
  // reported on its own and subtracted, so it can never inflate the inference number.
  const waitTotal = Math.min(num(result.model_wait_ms), latency);
  const load = Math.min(num(result.model_load_ms), waitTotal);
  const wait = waitTotal - load;
  const inference = Math.max(0, latency - waitTotal);
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
  const barMs = compute + fullMs;
  const shareOf = (base, value) => (base > 0 ? (value / base) * 100 : 0);
  const segments = DEBUG_TIMING_SEGMENTS
    .map((segment) => ({
      ...segment,
      ms: segment.key === "compute" ? compute : segment.key === "chunk" ? chunkMs : ghostMs,
    }))
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
      ...(compute > 0 ? [{ key: "compute", label: "gpu", text: formatTimingMs(compute) }] : []),
      ...(transfer > 0 ? [{ key: "transfer", label: "transfer", text: formatTimingMs(transfer) }] : []),
      ...(other > 0 ? [{ key: "other", label: "other", text: formatTimingMs(other) }] : []),
      { key: "chunk", label: "chunk", text: chunkMs > 0 ? formatTimingMs(chunkMs) : "—" },
    ],
    caption: caption.join(" · "),
    // Detail rows are hierarchical: rows inside the request share the inference total,
    // the phases around it share the whole latency, so every bar length stays readable.
    detailRows: [
      { key: "inference", depth: 0, text: formatTimingMs(inference), ms: inference, detail: "excludes load", pct: shareOf(latency, inference) },
      { key: "compute", depth: 1, text: formatTimingMs(compute), ms: compute, detail: "gpu", pct: shareOf(inference, compute) },
      ...(encode > 0 ? [{ key: "encode", depth: 1, text: formatTimingMs(encode), ms: encode, detail: "", pct: shareOf(inference, encode) }] : []),
      ...(upload > 0 ? [{ key: "upload", depth: 1, text: formatTimingMs(upload), ms: upload, detail: "", pct: shareOf(inference, upload) }] : []),
      ...(download > 0 ? [{ key: "download", depth: 1, text: formatTimingMs(download), ms: download, detail: "", pct: shareOf(inference, download) }] : []),
      ...(other > 0 ? [{ key: "other", depth: 1, text: formatTimingMs(other), ms: other, detail: "", pct: shareOf(inference, other) }] : []),
      {
        key: "chunk",
        depth: 0,
        text: chunkMs > 0 ? formatTimingMs(chunkMs) : "—",
        ms: chunkMs,
        detail: `${steps}${generatedSteps != null ? ` / ${generatedSteps}` : ""} steps${fps > 0 ? ` · ${Math.round(fps)} fps` : ""}`,
        pct: shareOf(latency, chunkMs),
      },
      ...(generatedSteps != null
        ? [{ key: "ghost", depth: 0, text: formatTimingMs(ghostMs), ms: ghostMs, detail: `${generatedSteps - steps} more steps`, pct: shareOf(latency, ghostMs) }]
        : []),
      ...(load > 0 ? [{ key: "load", depth: 0, text: formatTimingMs(load), ms: load, detail: "session", pct: shareOf(latency, load) }] : []),
      ...(wait > 0 ? [{ key: "wait", depth: 0, text: formatTimingMs(wait), ms: wait, detail: "", pct: shareOf(latency, wait) }] : []),
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
