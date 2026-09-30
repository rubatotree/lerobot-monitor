// Pure timing math for the Debug panel's inference bar; no DOM access.
// One bar spans the request: wait/load/compute/other, then the action chunk's own
// play time, then — when the request truncated the chunk — a hatched tail for the
// steps the policy would also have produced.
export const DEBUG_TIMING_SEGMENTS = [
  { key: "wait", label: "wait", kind: "time" },
  { key: "load", label: "load", kind: "time" },
  { key: "compute", label: "compute", kind: "time" },
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
  // wait/load/compute/other must add up to latency even when a measurement overshoots.
  const waitTotal = Math.min(num(result.model_wait_ms), latency);
  const load = Math.min(num(result.model_load_ms), waitTotal);
  const wait = waitTotal - load;
  const compute = Math.min(num(result.compute_ms), Math.max(0, latency - waitTotal));
  const other = Math.max(0, latency - waitTotal - compute);
  const chunkMs = fps > 0 ? (steps / fps) * 1000 : 0;
  const ghostMs = fps > 0 && generatedSteps != null ? ((generatedSteps - steps) / fps) * 1000 : 0;
  const fullMs = chunkMs + ghostMs;
  const totalMs = wait + load + compute + other + fullMs;
  const segments = [
    { key: "wait", label: "wait", kind: "time", ms: wait },
    { key: "load", label: "load", kind: "time", ms: load },
    { key: "compute", label: "compute", kind: "time", ms: compute },
    { key: "other", label: "other", kind: "time", ms: other },
    { key: "chunk", label: "chunk", kind: "chunk", ms: chunkMs },
    { key: "ghost", label: "would-be", kind: "ghost", ms: ghostMs },
  ]
    .filter((segment) => segment.ms > 0)
    .map((segment) => ({ ...segment, pct: totalMs > 0 ? (segment.ms / totalMs) * 100 : 0 }));
  const caption = [`chunk ${steps}${generatedSteps != null ? ` / ${generatedSteps}` : ""} steps`];
  if (chunkMs > 0) {
    caption.push(
      generatedSteps != null
        ? `${formatTimingMs(chunkMs)} of ${formatTimingMs(fullMs)}`
        : formatTimingMs(chunkMs),
    );
  }
  if (fps > 0) caption.push(`@ ${Math.round(fps)} fps`);
  return {
    hasTiming: latency > 0 && segments.length > 0,
    totalMs,
    latencyMs: latency,
    steps,
    generatedSteps,
    fps: fps > 0 ? fps : null,
    chunkMs,
    fullMs,
    truncated: generatedSteps != null,
    segments,
    chips: [
      { key: "inference", label: "inference", text: formatTimingMs(latency) },
      ...segments
        .filter((segment) => segment.kind === "time")
        .map((segment) => ({ key: segment.key, label: segment.label, text: formatTimingMs(segment.ms) })),
    ],
    caption: caption.join(" · "),
  };
}

if (typeof window !== "undefined") {
  window.DebugTiming = { DEBUG_TIMING_SEGMENTS, buildDebugTiming, formatTimingMs };
}
