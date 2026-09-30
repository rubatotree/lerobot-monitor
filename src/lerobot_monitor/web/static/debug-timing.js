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

// Measured phases of one cloud debug inference, in wall-clock order. ``parent`` gives
// the row its indent (the tree mirrors what contains what, not what the bar shows) and
// ``bucket`` only colors the row with the bar segment it belongs to. Client phases are
// measured by the Monitor, ``server_*`` ones by the serving process (and its worker
// process) and arrive in the response, so an older cloud build simply omits them.
export const DEBUG_PHASES = [
  { key: "session", label: "session", parent: null, bucket: "load", detail: "load" },
  { key: "inference", label: "inference", parent: null, bucket: "inference", detail: "request walk" },
  { key: "client_lease", label: "lease", parent: "inference", bucket: "other", detail: "acquire + release" },
  { key: "client_build", label: "build", parent: "inference", bucket: "other", detail: "state + payload" },
  { key: "client_encode", label: "encode", parent: "inference", bucket: "encode", detail: "png" },
  { key: "client_serialize", label: "serialize", parent: "inference", bucket: "other", detail: "json payload" },
  { key: "client_transport", label: "client setup", parent: "inference", bucket: "upload", detail: "http client" },
  { key: "client_ttfb", label: "tunnel", parent: "inference", bucket: "upload", detail: "request + headers" },
  { key: "server_read", label: "server read", parent: "client_ttfb", bucket: "compute", detail: "body received" },
  { key: "server_parse", label: "validate", parent: "client_ttfb", bucket: "compute", detail: "" },
  { key: "server_service", label: "service", parent: "client_ttfb", bucket: "compute", detail: "" },
  { key: "server_ipc", label: "worker ipc", parent: "server_service", bucket: "compute", detail: "json + pipe" },
  { key: "server_worker", label: "policy worker", parent: "server_ipc", bucket: "compute", detail: "" },
  { key: "server_decode", label: "decode", parent: "server_worker", bucket: "compute", detail: "state + png" },
  { key: "server_prepare", label: "prepare", parent: "server_worker", bucket: "compute", detail: "" },
  { key: "server_policy", label: "policy (gpu)", parent: "server_worker", bucket: "compute", detail: "" },
  { key: "server_emit", label: "emit", parent: "server_worker", bucket: "compute", detail: "to cpu list" },
  { key: "client_read", label: "read", parent: "inference", bucket: "download", detail: "reply body" },
  { key: "client_parse", label: "parse", parent: "inference", bucket: "other", detail: "json reply" },
  { key: "client_poses", label: "poses", parent: "inference", bucket: "other", detail: "" },
  { key: "client_close", label: "close", parent: "inference", bucket: "other", detail: "DELETE + join" },
  { key: "client_close_http", label: "session delete", parent: "client_close", bucket: "other", detail: "" },
  { key: "client_close_join", label: "heartbeat join", parent: "client_close", bucket: "other", detail: "" },
  { key: "other_unmeasured", label: "unmeasured", parent: "inference", bucket: "other", detail: "unmeasured tail" },
];

// Phases whose share is the whole click rather than the inference alone.
const DEBUG_PHASE_ROOT_BUCKETS = new Set(["load"]);

function phaseDepths() {
  const byKey = new Map(DEBUG_PHASES.map((phase) => [phase.key, phase]));
  const depths = new Map();
  DEBUG_PHASES.forEach((phase) => {
    let depth = 0;
    let current = phase;
    while (current && current.parent) {
      depth += 1;
      current = byKey.get(current.parent);
    }
    depths.set(phase.key, depth);
  });
  return depths;
}

const DEBUG_PHASE_DEPTHS = phaseDepths();
const DEBUG_PHASE_UNMEASURED_FLOOR_MS = 0.5;

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
  // Measured phases (cloud debug only): the request walk in wall-clock order, with the
  // chores inside the bar's "other" segment listed one by one and any leftover named.
  const timingMs = result.timing_ms && typeof result.timing_ms === "object" ? result.timing_ms : {};
  const measured = Object.keys(timingMs).length > 0;
  const phaseValue = (key) => {
    if (key === "inference") return inference;
    if (key === "session") return sessionMs;
    return num(timingMs[key]);
  };
  const phaseMs = Object.fromEntries(DEBUG_PHASES.map((phase) => [phase.key, phaseValue(phase.key)]));
  // "other" is the request minus the four stage legs, and the legs cover the whole
  // round trip [sent_at, received_at] — serialization happens inside it, so only the
  // phases outside that window belong to this reconciliation.
  const otherPhases = ["client_lease", "client_build", "client_parse", "client_poses", "client_close"];
  const choreMs = otherPhases.reduce((total, key) => total + phaseMs[key], 0);
  const phaseRows = [];
  if (measured) {
    DEBUG_PHASES.forEach((phase) => {
      if (phase.key === "other_unmeasured") return;
      const ms = phaseMs[phase.key];
      if (!(ms > 0)) return;
      phaseRows.push({
        key: phase.key,
        label: phase.label,
        detail: phase.detail,
        bucket: phase.bucket,
        depth: DEBUG_PHASE_DEPTHS.get(phase.key) || 0,
        ms,
        text: formatTimingMs(ms),
        // Shares are over the request; the session row sits outside it and uses the clock.
        pct: shareOf(DEBUG_PHASE_ROOT_BUCKETS.has(phase.bucket) ? latency : inference, ms),
      });
    });
    const unmeasured = other - choreMs;
    if (unmeasured > DEBUG_PHASE_UNMEASURED_FLOOR_MS) {
      phaseRows.push({
        key: "other_unmeasured",
        label: "unmeasured",
        detail: "outside measured phases",
        bucket: "other",
        depth: DEBUG_PHASE_DEPTHS.get("other_unmeasured") || 0,
        ms: unmeasured,
        text: formatTimingMs(unmeasured),
        pct: shareOf(inference, unmeasured),
      });
    }
  }
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
    // Section meta: the whole click (session + request), matching the first two rows.
    phaseTotalMs: latency,
    phaseRows,
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
  window.DebugTiming = { DEBUG_TIMING_SEGMENTS, DEBUG_PHASES, buildDebugTiming, formatTimingMs };
}
