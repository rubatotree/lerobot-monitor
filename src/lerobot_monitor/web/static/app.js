const JOINT_FALLBACK = [
  "shoulder_pan", "shoulder_lift", "elbow_flex",
  "wrist_flex", "wrist_roll", "gripper",
];

const PAL = ["#8b7cf7", "#6ea8ff", "#c084fc", "#5dba9a", "#e06b7a", "#7dd3fc"];
const HISTORY_CAPACITY = 36000;
const CHART_MAX_POINTS = 1200;
const BASE = (document.documentElement.dataset.base || "/lerobot").replace(/\/$/, "");
const WALL_CLOCK_OFFSET_S = Date.now() / 1000 - performance.now() / 1000;

let meta = { joints: JOINT_FALLBACK, limits: {}, presets: {}, cameras: [] };
let targets = {};
let last = {};
let ws;
let busLive = false;
let pendingLive = {};
let liveTimer = null;
let latestStatus = null;
let replayResumeOnVisible = false;
let pageVisible = !document.hidden;
let camMenu = [];
let lastPorts = [];
let lastHwKey = "";
let followSliders = false;
let autoRecord = false;
let capturing = false;
let selectedVideoId = "";
let selectedDatasetId = "";
let videosCache = [];
let datasetsCache = [];
let snapshotsCache = [];
let modelsCache = [];
const LIBRARY_SEARCH_KEY = "lerobot-monitor-library-search";
const librarySearch = {
  videos: "",
  datasets: "",
  snapshots: "",
  models: "",
  ...loadJsonStorage(LIBRARY_SEARCH_KEY),
};
let activeLibrarySearchKind = "videos";
let activeSnapshot = null;
let snapshotActive = false;
let snapshotCaptureBusy = false;
let episodeSource = null;
let episodeRows = [];
let expandedEpisode = null;
let episodeSignatureCache = "";
let replayActive = false;
let replaySeekId = null;
let replayScrubPointerId = null;
let armReplay = false;
let replayRobotTimer = null;
let replayRobotInFlight = false;
let episodeRequestGeneration = 0;
let previewRequestGeneration = 0;
let episodeLoading = false;
let episodeError = "";
let episodeSelectionClosed = true;
let episodeMutationPending = false;
const episodeDrafts = new Map();
let replayScrubWasPlaying = false;
const libraryState = {
  videos: { loading: false, error: "", generation: 0 },
  datasets: { loading: false, error: "", generation: 0 },
  snapshots: { loading: false, error: "", generation: 0 },
  models: { loading: false, error: "", generation: 0 },
};
const MATCH_TOL = {
  shoulder_pan: 2, shoulder_lift: 2, elbow_flex: 2,
  wrist_flex: 2, wrist_roll: 2, gripper: 3,
};

function $(id) { return document.getElementById(id); }

let mjpegObserver = null;

function deactivateMjpeg(img) {
  if (!img || !img.hasAttribute("src")) return;
  img.removeAttribute("src");
  img.classList.remove("live");
  const card = img.closest(".cam-card");
  if (card) card.classList.remove("has-sig");
}

function activateMjpeg(img) {
  if (!img || !pageVisible || img.dataset.mjpegVisible !== "1") return;
  const src = img.dataset.mjpegSrc;
  if (src && img.getAttribute("src") !== src) img.src = src;
}

function syncMjpegStreams() {
  document.querySelectorAll("img[data-mjpeg-src]").forEach((img) => {
    if (pageVisible && img.dataset.mjpegVisible === "1") activateMjpeg(img);
    else deactivateMjpeg(img);
  });
}

function mjpegObservedTarget(img) {
  return img.closest(".cam-card") || img;
}

function ensureMjpegObserver() {
  if (mjpegObserver || typeof IntersectionObserver !== "function") return mjpegObserver;
  mjpegObserver = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      const img = entry.target.matches("img[data-mjpeg-src]")
        ? entry.target
        : entry.target.querySelector("img[data-mjpeg-src]");
      if (!img) return;
      img.dataset.mjpegVisible = entry.isIntersecting ? "1" : "0";
      if (entry.isIntersecting) activateMjpeg(img);
      else deactivateMjpeg(img);
    });
  }, { threshold: 0.01 });
  return mjpegObserver;
}

function observeMjpeg(img) {
  if (!img || !img.dataset.mjpegSrc) return;
  const observer = ensureMjpegObserver();
  if (observer) observer.observe(mjpegObservedTarget(img));
  else img.dataset.mjpegVisible = "1";
  activateMjpeg(img);
}

const CHART_UPDATE_INTERVAL_MS = 16;
const CHART_Y_LIMIT = 180;
const LIVE_MODE_HISTORY_S = 600;
const CHART_TOOLTIP_TOLERANCE_S = 0.35;
const CHART_TIME_AXIS_FADE_MS = 140;
const CHART_TIME_AXIS_PADDING = 18;
const LIVE_RIGHT_PADDING_S = 0.2;
const LIVE_FRAME_RATE = 30;
const TIME_LABEL_EDGE_FADE_PX = 24;
const TIME_LABEL_EDGE_GAP_PX = 8;
const MODE_MARKER_LABEL_OFFSET_PX = 10;
const TIME_TICK_STEPS_S = [
  0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60,
  120, 300, 600, 1200, 1800, 3600,
];
const chartLegendVisibility = {
  command: true,
  prediction: true,
  gap: true,
  mode: true,
  now: true,
  joints: new Map(),
};
const CHART_SCALE_OPTIONS = [
  { seconds: 2, label: "2s" },
  { seconds: 10, label: "10s" },
  { seconds: 30, label: "30s" },
  { seconds: 60, label: "1m" },
  { seconds: 600, label: "10m" },
];
const CHART_SCALE_STORAGE_KEY = "lerobot-monitor-chart-scale";
let chartUpdateTimer = null;
const pendingChartUpdates = new Set();
let liveModeMarkers = [];
let lastDisplayMode = "";
let chartTimeBasis = "wall";
let chartScaleSeconds = 10;
let chartAnimationFrame = null;
let lastChartAnimationMs = 0;
try {
  const savedScale = Number(localStorage.getItem(CHART_SCALE_STORAGE_KEY));
  if (CHART_SCALE_OPTIONS.some((option) => option.seconds === savedScale)) {
    chartScaleSeconds = savedScale;
  }
} catch { /* ignore */ }

function flushChartUpdates() {
  chartUpdateTimer = null;
  const charts = [...pendingChartUpdates];
  pendingChartUpdates.clear();
  charts.forEach((chart) => chart.update("none"));
}

function scheduleChartUpdate(chart) {
  if (!chart || document.hidden) return;
  if (!replayActive && chart.$timeAxis === true) return;
  pendingChartUpdates.add(chart);
  if (chartUpdateTimer) return;
  chartUpdateTimer = setTimeout(flushChartUpdates, CHART_UPDATE_INTERVAL_MS);
}

function fmt(n, d = 1) {
  if (n == null || Number.isNaN(n)) return "—";
  return Number(n).toFixed(d);
}

function fmtTime(s) {
  if (s == null || s < 0) return "—";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return m > 0 ? `${m}:${String(sec).padStart(2, "0")}` : `${sec}s`;
}

async function api(path, body, method = "POST") {
  const opts = { method };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(BASE + path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok || (data && (data.ok === false || data.error))) {
    const detail = data && (data.error || data.detail);
    const message = typeof detail === "string"
      ? detail
      : (detail && typeof detail.message === "string" ? detail.message : "");
    throw new Error(message || res.statusText || "request failed");
  }
  return data;
}

function localLog(message, level) {
  const log = $("log");
  if (!log) return;
  const li = document.createElement("li");
  if (level === "error") li.className = "error";
  li.textContent = `${new Date().toLocaleTimeString()}  ${message}`;
  log.prepend(li);
}

function toastError(err) {
  localLog(err.message || err, "error");
}

let actionBusy = false;
let stopRequested = false;
const disconnectPending = { arm: false, leader: false };
const SLOW_STOP_MODES = new Set(["teleop", "record", "rollout"]);
const SLOW_START_PENDING = new Set(["teleop_start", "record_start", "rollout_start"]);
function exitReplayForControl() {
  if (episodeSource || replayActive || armReplay) closeEpisodeSelection();
}

function isSlowStopActive(mode, pending) {
  return SLOW_STOP_MODES.has(mode) || SLOW_START_PENDING.has(pending);
}

function syncStopButton(mode, pending) {
  const active = isSlowStopActive(mode, pending);
  if (!active) stopRequested = false;
  const button = $("btn-hdr-stop");
  if (!button) return;
  button.classList.toggle("stop-requested", stopRequested);
  button.title = stopRequested ? "Press again to force stop" : "Stop teleop / record / rollout";
}

function requestStop() {
  const mode = (last && last.mode) || "";
  const pending = last && last.task && last.task.pending;
  if (stopRequested) {
    localLog("force stop requested", "error");
    return api("/api/task/force_stop").catch(toastError);
  }
  if (replayActive || episodeSource) closeEpisodeSelection();
  if (isSlowStopActive(mode, pending)) {
    stopRequested = true;
    syncStopButton(mode, pending);
    localLog("stop requested — press Stop again to force");
  } else {
    localLog("stop requested");
  }
  return api("/api/task/stop").catch(toastError);
}
async function runAction(btnId, message, fn) {
  if (btnId === "btn-hdr-stop") {
    return requestStop();
  }
  if (actionBusy) {
    localLog("busy — extra click ignored");
    return;
  }
  actionBusy = true;
  const btn = $(btnId);
  if (btn) btn.classList.add("pending");
  localLog(message);
  try {
    await fn();
  } catch (err) {
    toastError(err);
  } finally {
    actionBusy = false;
    if (btn) btn.classList.remove("pending");
  }
}

setInterval(() => { $("clock").textContent = new Date().toLocaleTimeString(); }, 500);

function positionReplayChartCursor(chart) {
  const cursor = chart && chart.$replayCursorElement;
  const x = chart && chart.scales && chart.scales.x;
  const area = chart && chart.chartArea;
  const canvas = chart && chart.canvas;
  const host = cursor && cursor.parentElement;
  const time = Number(chart && chart.$replayCursorTime);
  if (!cursor || !x || !area || !canvas || !host || !replayActive || !Number.isFinite(time)) {
    if (cursor) cursor.hidden = true;
    return;
  }
  const canvasRect = canvas.getBoundingClientRect();
  const hostRect = host.getBoundingClientRect();
  if (!canvasRect.width || !canvasRect.height || !chart.width || !chart.height) {
    cursor.hidden = true;
    return;
  }
  const clampedTime = Math.min(x.max, Math.max(x.min, time));
  const scaleX = canvasRect.width / chart.width;
  const scaleY = canvasRect.height / chart.height;
  const pixel = x.getPixelForValue(clampedTime);
  cursor.style.left = `${canvasRect.left - hostRect.left + pixel * scaleX}px`;
  cursor.style.top = `${canvasRect.top - hostRect.top + area.top * scaleY}px`;
  cursor.style.height = `${Math.max(0, (area.bottom - area.top) * scaleY)}px`;
  cursor.hidden = false;
}

function positionLiveNowCursor(chart) {
  const cursor = chart && chart.$replayCursorElement;
  const x = chart && chart.scales && chart.scales.x;
  const area = chart && chart.chartArea;
  const canvas = chart && chart.canvas;
  const host = cursor && cursor.parentElement;
  const time = Number(chart && chart.$nowTime);
  if (
    !cursor
    || !x
    || !area
    || !canvas
    || !host
    || replayActive
    || !chart.$showNowLine
    || !Number.isFinite(time)
  ) {
    if (cursor) cursor.hidden = true;
    return;
  }
  const canvasRect = canvas.getBoundingClientRect();
  const hostRect = host.getBoundingClientRect();
  if (!canvasRect.width || !canvasRect.height || !chart.width || !chart.height) {
    cursor.hidden = true;
    return;
  }
  const scaleX = canvasRect.width / chart.width;
  const scaleY = canvasRect.height / chart.height;
  let pixel = x.getPixelForValue(time);
  if (!Number.isFinite(pixel) || pixel < area.left) {
    cursor.hidden = true;
    return;
  }
  pixel = Math.min(pixel, area.right);
  cursor.style.left = `${canvasRect.left - hostRect.left + pixel * scaleX}px`;
  cursor.style.top = `${canvasRect.top - hostRect.top + area.top * scaleY}px`;
  cursor.style.height = `${Math.max(0, (area.bottom - area.top) * scaleY)}px`;
  cursor.hidden = false;
}

function formatWallClockTime(seconds, milliseconds = false) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return "—";
  const date = new Date(value * 1000);
  const time = date.toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  if (!milliseconds) return time;
  return `${time}.${String(date.getMilliseconds()).padStart(3, "0")}`;
}

function formatDurationCompact(seconds, digits = 1) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return "—";
  const duration = Math.max(0, value);
  if (duration < 60) return `${duration.toFixed(digits)}s`;
  const minutes = Math.floor(duration / 60);
  const remainder = duration - minutes * 60;
  return `${minutes}:${remainder.toFixed(digits).padStart(digits + 3, "0")}`;
}

function formatRelativeDuration(seconds, digits = 1) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return "—";
  return value < 0
    ? `-${formatDurationCompact(-value, digits)}`
    : formatDurationCompact(value, digits);
}

function chartModeEpoch(chart, seconds) {
  const value = Number(seconds);
  let epoch = Number(chart && chart.$historyStart);
  (chart && chart.$modeMarkers || []).forEach((marker) => {
    const markerTime = Number(marker.x);
    if (!Number.isFinite(markerTime) || markerTime > value) return;
    if (!Number.isFinite(epoch) || markerTime > epoch) epoch = markerTime;
  });
  return Number.isFinite(epoch) ? epoch : value;
}

function formatChartTimeValue(seconds, chart, milliseconds = false) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return "—";
  const basis = replayActive ? "start" : ((chart && chart.$timeBasis) || chartTimeBasis);
  const digits = milliseconds ? 3 : 1;
  if (basis === "wall") return formatWallClockTime(value, milliseconds);
  if (basis === "mode") {
    const elapsed = value - chartModeEpoch(chart, value);
    return `${elapsed >= 0 ? "+" : ""}${formatRelativeDuration(elapsed, digits)}`;
  }
  const historyStart = Number(chart && chart.$historyStart);
  const start = Number.isFinite(historyStart) ? historyStart : (replayActive ? 0 : value);
  const elapsed = value - start;
  const formatted = formatRelativeDuration(elapsed, digits);
  return replayActive ? formatted : `${elapsed >= 0 ? "+" : ""}${formatted}`;
}

function chartTimeTicks(chart, scale) {
  const min = Number(scale && scale.min);
  const max = Number(scale && scale.max);
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return [];
  const span = max - min;
  const targetStep = span / 6;
  const step = TIME_TICK_STEPS_S.find((candidate) => candidate >= targetStep)
    || 10 ** Math.ceil(Math.log10(targetStep));
  const basis = replayActive ? "start" : ((chart && chart.$timeBasis) || chartTimeBasis);
  let origin = 0;
  if (basis === "mode") origin = chartModeEpoch(chart, min);
  else if (basis === "start") {
    const historyStart = Number(chart && chart.$historyStart);
    origin = Number.isFinite(historyStart) ? historyStart : 0;
  }
  const first = origin + Math.ceil((min - origin) / step) * step;
  const ticks = [];
  for (let value = first; value <= max + step * 1e-6 && ticks.length < 20; value += step) {
    ticks.push({
      value,
      x: scale.getPixelForValue(value),
      label: formatChartTimeValue(value, chart),
    });
  }
  return ticks;
}

function nearestChartPoint(points, seconds, tolerance, endIndex = points.length) {
  if (!Array.isArray(points) || !points.length) return null;
  const limit = Math.max(0, Math.min(endIndex, points.length));
  if (!limit) return null;
  const target = Number(seconds);
  if (!Number.isFinite(target)) return null;
  let low = 0;
  let high = limit - 1;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    const middleTime = Number(points[middle] && points[middle].x);
    if (!Number.isFinite(middleTime) || middleTime < target) low = middle + 1;
    else high = middle;
  }
  let nearest = null;
  let nearestDistance = Number.POSITIVE_INFINITY;
  for (let index = Math.max(0, low - 1); index <= Math.min(limit - 1, low + 1); index += 1) {
    const point = points[index];
    const pointTime = Number(point && point.x);
    const pointValue = Number(point && point.y);
    if (!Number.isFinite(pointTime) || !Number.isFinite(pointValue)) continue;
    const distance = Math.abs(pointTime - target);
    if (distance < nearestDistance) {
      nearest = point;
      nearestDistance = distance;
    }
  }
  return nearestDistance <= Number(tolerance) ? nearest : null;
}

function interpolatedChartPoint(points, seconds, endIndex = points.length, tolerance = Number.POSITIVE_INFINITY) {
  if (!Array.isArray(points) || !points.length) return null;
  const limit = Math.max(0, Math.min(endIndex, points.length));
  if (!limit) return null;
  const target = Number(seconds);
  if (!Number.isFinite(target)) return null;
  const nextIndex = lowerBoundPointTime(points, target, limit);
  if (nextIndex <= 0 || nextIndex >= limit) {
    return nearestChartPoint(points, target, tolerance, limit);
  }
  const previous = points[nextIndex - 1];
  const next = points[nextIndex];
  const previousTime = Number(previous && previous.x);
  const nextTime = Number(next && next.x);
  const previousValue = Number(previous && previous.y);
  const nextValue = Number(next && next.y);
  if (
    !Number.isFinite(previousTime)
    || !Number.isFinite(nextTime)
    || !Number.isFinite(previousValue)
    || !Number.isFinite(nextValue)
    || nextTime <= previousTime
  ) {
    return nearestChartPoint(points, target, tolerance, limit);
  }
  const ratio = Math.max(0, Math.min(1, (target - previousTime) / (nextTime - previousTime)));
  return {
    x: target,
    y: previousValue + ratio * (nextValue - previousValue),
  };
}

function lastChartPointAtOrBefore(points, seconds) {
  if (!Array.isArray(points) || !points.length) return null;
  const target = Number(seconds);
  if (!Number.isFinite(target)) return null;
  let low = 0;
  let high = points.length - 1;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    const middleTime = Number(points[middle] && points[middle].x);
    if (!Number.isFinite(middleTime) || middleTime <= target) low = middle;
    else high = middle - 1;
  }
  for (let index = low; index >= 0; index -= 1) {
    const point = points[index];
    if (Number.isFinite(Number(point && point.y))) return point;
  }
  return null;
}

function lowerBoundPointTime(points, target, endIndex = points.length) {
  let low = 0;
  let high = Math.max(0, Math.min(endIndex, points.length));
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (Number(points[middle].x) < target) low = middle + 1;
    else high = middle;
  }
  return low;
}

function interpolateScaleValueFromTicks(scale, pixel) {
  const targetPixel = Number(pixel);
  if (!scale || !Number.isFinite(targetPixel)) return Number.NaN;
  const ticks = (scale.ticks || [])
    .map((tick) => ({
      value: Number(tick.value),
      pixel: scale.getPixelForValue(Number(tick.value)),
    }))
    .filter((tick) => Number.isFinite(tick.value) && Number.isFinite(tick.pixel))
    .sort((left, right) => left.pixel - right.pixel);
  if (!ticks.length) return scale.getValueForPixel(targetPixel);
  if (targetPixel <= ticks[0].pixel) return ticks[0].value;
  for (let index = 1; index < ticks.length; index += 1) {
    const previous = ticks[index - 1];
    const next = ticks[index];
    if (targetPixel > next.pixel) continue;
    if (next.pixel <= previous.pixel) return previous.value;
    const ratio = (targetPixel - previous.pixel) / (next.pixel - previous.pixel);
    return previous.value + ratio * (next.value - previous.value);
  }
  return ticks[ticks.length - 1].value;
}

function leftBoundaryPoint(points, minTime, endIndex = points.length) {
  if (!Array.isArray(points) || !points.length) return null;
  const limit = Math.max(0, Math.min(endIndex, points.length));
  if (!limit) return null;
  const index = lowerBoundPointTime(points, minTime, limit);
  if (index <= 0) return null;
  const previous = points[index - 1];
  const next = index < limit ? points[index] : previous;
  const previousTime = Number(previous.x);
  const nextTime = Number(next.x);
  const previousValue = Number(previous.y);
  const nextValue = Number(next.y);
  if (!Number.isFinite(previousTime) || !Number.isFinite(nextTime)) return null;
  if (!Number.isFinite(previousValue) || !Number.isFinite(nextValue)) return null;
  if (nextTime <= previousTime) return { x: minTime, y: previousValue };
  const ratio = Math.max(0, Math.min(1, (minTime - previousTime) / (nextTime - previousTime)));
  return { x: minTime, y: previousValue + ratio * (nextValue - previousValue) };
}

function decimateVisibleSeries(
  points,
  minTime,
  maxTime,
  maxPoints = CHART_MAX_POINTS,
  endIndex = points.length,
) {
  if (!Array.isArray(points) || !points.length) return [];
  const limit = Math.max(0, Math.min(endIndex, points.length));
  if (!limit) return [];
  const start = lowerBoundPointTime(points, minTime, limit);
  let low = start;
  let high = limit;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (Number(points[middle].x) <= maxTime) low = middle + 1;
    else high = middle;
  }
  const end = Math.min(low, limit);
  const count = end - start;
  if (count <= 0) return [];
  if (count <= maxPoints) return points.slice(start, end);
  const sampled = [];
  const bucketSize = Math.max(chartScaleSeconds / maxPoints, 0.001);
  let previousBucket = null;
  for (let index = start; index < end; index += 1) {
    const point = points[index];
    const bucket = Math.floor(Number(point.x) / bucketSize);
    if (bucket !== previousBucket) {
      sampled.push(point);
      previousBucket = bucket;
    } else {
      sampled[sampled.length - 1] = point;
    }
  }
  return sampled;
}

function applyLeftBoundary(dataset, minTime, endIndex) {
  const data = dataset.data || (dataset.data = []);
  if (data.length && data[0].$boundary) data.shift();
  while (data.length && Number(data[0].x) < minTime) data.shift();
  const boundary = leftBoundaryPoint(dataset.$raw || [], minTime, endIndex);
  if (!boundary) return;
  if (data.length && Number(data[0].x) <= minTime + 1e-6) return;
  data.unshift({ x: boundary.x, y: boundary.y, $boundary: true });
}

function refreshLiveChartSeries(chart, now) {
  if (!chart || chart.$timeAxis !== true) return;
  const future = rolloutFutureWindowS(now);
  const minTime = now - chartScaleSeconds;
  const maxTime = now + future;
  (chart.$live || []).forEach((dataset) => {
    const raw = dataset.$raw || [];
    const visible = decimateVisibleSeries(raw, minTime, maxTime);
    const lastVisible = visible.length ? visible[visible.length - 1] : null;
    if (lastVisible && Number.isFinite(lastVisible.y) && lastVisible.x < now) {
      visible.push({ x: now, y: lastVisible.y, $hold: true });
    }
    dataset.data = visible;
    applyLeftBoundary(dataset, minTime, raw.length);
  });
  chart.$historyEnd = now;
}

function liveWallClockNow() {
  return WALL_CLOCK_OFFSET_S + performance.now() / 1000;
}

function animateLiveCharts(frameMs) {
  if (document.hidden || replayActive) {
    [stateChart, actionChart].forEach((chart) => {
      if (!chart) return;
      chart.$lastAnimationNow = Number.NaN;
      chart.$pendingAnimationNow = Number.NaN;
      chart.$lastRenderedNow = Number.NaN;
    });
  } else if (frameMs - lastChartAnimationMs >= CHART_UPDATE_INTERVAL_MS) {
    lastChartAnimationMs = frameMs;
    const now = liveWallClockNow();
    const currentSlot = Math.floor(now * LIVE_FRAME_RATE) / LIVE_FRAME_RATE;
    [stateChart, actionChart].forEach((chart) => {
      if (!chart || chart.$timeAxis !== true) return;
      const renderNow = Number.isFinite(Number(chart.$pendingAnimationNow))
        ? Number(chart.$pendingAnimationNow)
        : currentSlot - 1 / LIVE_FRAME_RATE;
      chart.$pendingAnimationNow = currentSlot;
      chart.$lastAnimationNow = currentSlot;
      if (renderNow === Number(chart.$lastRenderedNow)) return;
      chart.$lastRenderedNow = renderNow;
      refreshLiveChartSeries(chart, renderNow);
      chart.$nowTime = renderNow;
      chart.$showNowLine = showLiveNowLine();
      chart.options.scales.x = liveTimeAxis(renderNow);
      chart.update("none");
      positionLiveNowCursor(chart);
    });
  }
  chartAnimationFrame = requestAnimationFrame(animateLiveCharts);
}

function chartTooltipModel(chart, seconds) {
  const rawTarget = Number(seconds);
  if (!chart || !Number.isFinite(rawTarget)) return null;
  const target = snapChartTimeToFrame(chart, rawTarget);
  const fixedFrames = chart.$timeAxis === false;
  const tolerance = Number(chart.$tooltipToleranceS) || CHART_TOOLTIP_TOLERANCE_S;
  const actualCutoff = chartActualCutoff(chart);
  const actualEligible = rawTarget <= actualCutoff + 1e-6;
  const gap = (chart.$predictionGaps || []).find((entry) => {
    const start = Math.min(Number(entry.start), Number(entry.end));
    const end = Math.max(Number(entry.start), Number(entry.end));
    return target >= start && target <= end;
  });
  const actual = new Map();
  const predicted = new Map();
  const actualVisible = new Map();
  const predictedVisible = new Map();
  (chart.data.datasets || []).forEach((dataset) => {
    if (!dataset || !Array.isArray(dataset.data) || !dataset.data.length) return;
    const label = String(dataset.label || "");
    const isPrediction = !!dataset.$prediction || label.endsWith(" · pred");
    const name = label.replace(/ · pred$/, "");
    if (!name) return;
    const visible = isDatasetLegendVisible(dataset);
    const points = dataset.$raw || dataset.data;
    let point = null;
    if (!isPrediction && actualEligible) {
      point = fixedFrames
        ? nearestChartPoint(points, target, Number.POSITIVE_INFINITY, points.length)
        : interpolatedChartPoint(points, target, points.length, tolerance);
      if (!point && !fixedFrames) point = lastChartPointAtOrBefore(points, target);
    } else if (isPrediction) {
      point = fixedFrames
        ? nearestChartPoint(
          dataset.data,
          target,
          Math.max(tolerance * 2, 0.1),
          dataset.data.length,
        )
        : interpolatedChartPoint(dataset.data, target, dataset.data.length, tolerance * 2);
    }
    if (!point) return;
    (isPrediction ? predicted : actual).set(name, point);
    (isPrediction ? predictedVisible : actualVisible).set(name, visible);
  });
  const known = jointNames();
  const names = [...new Set([...actual.keys(), ...predicted.keys()])]
    .sort((left, right) => {
      const leftIndex = known.indexOf(left);
      const rightIndex = known.indexOf(right);
      return (leftIndex < 0 ? known.length : leftIndex) - (rightIndex < 0 ? known.length : rightIndex);
    });
  if (!names.length) return null;
  return {
    gap: !!gap,
    rows: names.map((name) => ({
      name,
      actual: actual.get(name) || null,
      predicted: predicted.get(name) || null,
      actualVisible: actual.has(name) && actualVisible.get(name) !== false,
      predictedVisible: predicted.has(name) && predictedVisible.get(name) !== false,
    })),
    time: target,
  };
}

function chartActualCutoff(chart) {
  const liveNow = Number(chart && chart.$nowTime);
  if (chart && chart.$timeAxis === true && Number.isFinite(liveNow)) return liveNow;
  let cutoff = Number.NEGATIVE_INFINITY;
  (chart && chart.data.datasets || []).forEach((dataset) => {
    if (!dataset || dataset.$prediction || String(dataset.label || "").endsWith(" · pred")) return;
    const points = dataset.$raw || dataset.data || [];
    for (let index = points.length - 1; index >= 0; index -= 1) {
      const pointTime = Number(points[index] && points[index].x);
      if (!Number.isFinite(pointTime)) continue;
      cutoff = Math.max(cutoff, pointTime);
      break;
    }
  });
  return cutoff;
}

function snapChartTimeToFrame(chart, seconds) {
  const target = Number(seconds);
  if (!chart || chart.$timeAxis !== false || !Number.isFinite(target)) return target;
  const frames = Array.isArray(chart.$frameTimes) ? chart.$frameTimes : [];
  if (!frames.length) return target;
  let low = 0;
  let high = frames.length - 1;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (Number(frames[middle]) < target) low = middle + 1;
    else high = middle;
  }
  const next = Number(frames[low]);
  const previous = Number(frames[Math.max(0, low - 1)]);
  if (!Number.isFinite(previous)) return next;
  if (!Number.isFinite(next)) return previous;
  return Math.abs(target - previous) <= Math.abs(next - target) ? previous : next;
}

function hideChartHoverTooltip(chart) {
  const tooltip = chart && chart.$hoverTooltipElement;
  if (!tooltip) return;
  if (chart.$tooltipFrame) {
    cancelAnimationFrame(chart.$tooltipFrame);
    chart.$tooltipFrame = null;
  }
  chart.$tooltipRequest = null;
  tooltip.hidden = true;
  delete tooltip.dataset.timeValue;
}

function positionChartHoverTooltip(chart, pointerX, pointerY) {
  const tooltip = chart && chart.$hoverTooltipElement;
  const host = tooltip && tooltip.parentElement;
  const canvas = chart && chart.canvas;
  if (!tooltip || !host || !canvas) return;
  const canvasRect = canvas.getBoundingClientRect();
  const hostRect = host.getBoundingClientRect();
  const tooltipWidth = tooltip.offsetWidth;
  const tooltipHeight = tooltip.offsetHeight;
  let left = canvasRect.left - hostRect.left + pointerX + 12;
  let top = canvasRect.top - hostRect.top + pointerY + 12;
  if (left + tooltipWidth > hostRect.width - 4) {
    left = canvasRect.left - hostRect.left + pointerX - tooltipWidth - 12;
  }
  if (top + tooltipHeight > hostRect.height - 4) {
    top = canvasRect.top - hostRect.top + pointerY - tooltipHeight - 12;
  }
  tooltip.style.left = `${Math.max(4, Math.min(left, hostRect.width - tooltipWidth - 4))}px`;
  tooltip.style.top = `${Math.max(4, Math.min(top, hostRect.height - tooltipHeight - 4))}px`;
}

function formatTooltipValue(value) {
  if (value == null || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const magnitude = Math.abs(number);
  const digits = magnitude >= 100 ? 2 : magnitude >= 10 ? 3 : 4;
  return number.toFixed(digits);
}

function buildChartHoverTooltip(chart, model) {
  const tooltip = chart.$hoverTooltipElement;
  tooltip.replaceChildren();
  const header = document.createElement("div");
  header.className = "chart-tooltip-header";
  const time = document.createElement("span");
  time.className = "chart-tooltip-time";
  time.textContent = formatChartTimeValue(model.time, chart, true);
  header.appendChild(time);
  if (model.gap) {
    const gap = document.createElement("span");
    gap.className = "chart-tooltip-gap";
    gap.textContent = "gap";
    header.appendChild(gap);
  }
  const grid = document.createElement("div");
  grid.className = "chart-tooltip-grid";
  ["joint", "actual", "pred"].forEach((label) => {
    const cell = document.createElement("span");
    cell.className = "chart-tooltip-head";
    cell.textContent = label;
    grid.appendChild(cell);
  });
  chart.$tooltipActualCells = [];
  chart.$tooltipPredictionCells = [];
  model.rows.forEach((row) => {
    const name = document.createElement("span");
    name.textContent = row.name;
    const actual = document.createElement("span");
    actual.className = "chart-tooltip-actual";
    const predicted = document.createElement("span");
    predicted.className = "chart-tooltip-pred";
    grid.append(name, actual, predicted);
    chart.$tooltipActualCells.push(actual);
    chart.$tooltipPredictionCells.push(predicted);
  });
  tooltip.append(header, grid);
}

function renderChartHoverTooltip(chart, seconds, pointerX, pointerY) {
  const tooltip = chart && chart.$hoverTooltipElement;
  if (!tooltip || chart.$tooltipVisible === false) return;
  const model = chartTooltipModel(chart, seconds);
  if (!model) {
    hideChartHoverTooltip(chart);
    return;
  }
  const structure = [
    model.gap ? "gap" : "no-gap",
    model.rows.map((row) => row.name).join("|"),
  ].join("::");
  if (tooltip.dataset.structure !== structure) {
    buildChartHoverTooltip(chart, model);
    tooltip.dataset.structure = structure;
  }
  const time = tooltip.querySelector(".chart-tooltip-time");
  if (time) time.textContent = formatChartTimeValue(model.time, chart, true);
  const gap = tooltip.querySelector(".chart-tooltip-gap");
  if (gap) gap.hidden = !model.gap;
  model.rows.forEach((row, index) => {
    const actual = chart.$tooltipActualCells && chart.$tooltipActualCells[index];
    const predicted = chart.$tooltipPredictionCells && chart.$tooltipPredictionCells[index];
    if (actual) {
      actual.textContent = formatTooltipValue(row.actual && row.actual.y);
      actual.title = row.actual ? "Latest actual value at or before this time" : "";
      actual.classList.toggle("muted", !row.actualVisible);
    }
    if (predicted) {
      predicted.textContent = formatTooltipValue(row.predicted && row.predicted.y);
      predicted.classList.toggle("muted", !row.predictedVisible);
    }
  });
  tooltip.dataset.timeValue = String(model.time);
  tooltip.hidden = false;
  positionChartHoverTooltip(chart, pointerX, pointerY);
}

function scheduleChartHoverTooltip(chart, seconds, pointerX, pointerY) {
  if (chart.$tooltipVisible === false) return;
  chart.$tooltipRequest = { seconds, pointerX, pointerY };
  if (chart.$tooltipFrame) return;
  chart.$tooltipFrame = requestAnimationFrame(() => {
    chart.$tooltipFrame = null;
    const request = chart.$tooltipRequest;
    chart.$tooltipRequest = null;
    if (!request || !chart.$pointerPosition || !chart.$pointerPosition.inside) return;
    renderChartHoverTooltip(chart, request.seconds, request.pointerX, request.pointerY);
  });
}

const chartHoverTooltipPlugin = {
  id: "hoverTooltip",
  afterEvent(chart, args) {
    const event = args.event;
    if (!event) return;
    if (event.type === "mouseout") {
      chart.$pointerPosition = null;
      hideChartHoverTooltip(chart);
      if (chart.$timeAxis === false) chart.draw();
      return;
    }
    if (!["mousemove", "mouseover", "touchstart", "touchmove"].includes(event.type)) return;
    const xScale = chart.scales && chart.scales.x;
    const area = chart.chartArea;
    const pointerX = Number(event.x);
    const pointerY = Number(event.y);
    if (!xScale || !area || !Number.isFinite(pointerX) || !Number.isFinite(pointerY)) {
      chart.$pointerPosition = null;
      hideChartHoverTooltip(chart);
      return;
    }
    if (pointerX < area.left || pointerX > area.right || pointerY < area.top || pointerY > area.bottom) {
      chart.$pointerPosition = null;
      hideChartHoverTooltip(chart);
      return;
    }
    chart.$pointerPosition = { x: pointerX, y: pointerY, inside: true };
    const seconds = snapChartTimeToFrame(chart, xScale.getValueForPixel(pointerX));
    scheduleChartHoverTooltip(chart, seconds, pointerX, pointerY);
    if (chart.$timeAxis === false) chart.draw();
  },
};

const predictionGapPlugin = {
  id: "predictionGap",
  beforeDatasetsDraw(chart) {
    if (chartLegendVisibility.gap === false) return;
    const gaps = chart.$predictionGaps || [];
    if (!gaps.length) return;
    const xScale = chart.scales && chart.scales.x;
    const area = chart.chartArea;
    if (!xScale || !area) return;
    const ctx = chart.ctx;
    ctx.save();
    ctx.fillStyle = "rgba(224, 107, 122, 0.16)";
    gaps.forEach((gap) => {
      const start = Number(gap.start);
      const end = Number(gap.end);
      if (!Number.isFinite(start) || !Number.isFinite(end)) return;
      const left = Math.max(area.left, Math.min(xScale.getPixelForValue(start), xScale.getPixelForValue(end)));
      const right = Math.min(area.right, Math.max(xScale.getPixelForValue(start), xScale.getPixelForValue(end)));
      if (right <= left) return;
      ctx.fillRect(left, area.top, right - left, area.bottom - area.top);
    });
    ctx.restore();
  },
};

const modeMarkerPlugin = {
  id: "modeMarkers",
  beforeDatasetsDraw(chart) {
    if (replayActive || chartLegendVisibility.mode === false) return;
    const markers = chart.$modeMarkers || [];
    if (!markers.length) return;
    const xScale = chart.scales && chart.scales.x;
    const area = chart.chartArea;
    if (!xScale || !area) return;
    const ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = "rgba(168, 166, 184, 0.72)";
    ctx.lineWidth = 1;
    ctx.setLineDash([]);
    markers.forEach((marker) => {
      const x = xScale.getPixelForValue(Number(marker.x));
      if (!Number.isFinite(x) || x < area.left || x > area.right) return;
      ctx.beginPath();
      ctx.moveTo(x, area.top);
      ctx.lineTo(x, area.bottom);
      ctx.stroke();
      if (chart.canvas.id !== "chart-action") return;
      const label = String(marker.mode || "");
      if (!label) return;
      ctx.save();
      ctx.fillStyle = "rgba(197, 195, 210, 0.92)";
      ctx.font = "8px IBM Plex Mono";
      ctx.textAlign = "left";
      ctx.textBaseline = "bottom";
      ctx.translate(x + MODE_MARKER_LABEL_OFFSET_PX, area.bottom - 4);
      ctx.rotate(-Math.PI / 2);
      ctx.fillText(label, 0, 0);
      ctx.restore();
    });
    ctx.restore();
  },
  afterDatasetsDraw(chart) {
    if (replayActive || chartLegendVisibility.mode === false || chart.$timeBasis !== "mode") return;
    const markers = chart.$modeMarkers || [];
    if (!markers.length) return;
    const xScale = chart.scales && chart.scales.x;
    const area = chart.chartArea;
    if (!xScale || !area) return;
    const ctx = chart.ctx;
    chart.$modeZeroLabels = [];
    ctx.save();
    ctx.fillStyle = "rgba(211, 208, 224, 0.96)";
    ctx.font = "8px IBM Plex Mono";
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    markers.forEach((marker) => {
      const x = xScale.getPixelForValue(Number(marker.x));
      if (!Number.isFinite(x) || x < area.left || x > area.right) return;
      const labelX = Math.max(area.left + 6, Math.min(area.right - 6, x));
      ctx.fillText("0s", labelX, area.top + 2);
      chart.$modeZeroLabels.push({ x: labelX, mode: marker.mode });
    });
    ctx.restore();
  },
};

const pixelAlignedLinePlugin = {
  id: "pixelAlignedLine",
  beforeDatasetsDraw(chart) {
    const devicePixelRatio = Math.max(1, Number(chart.currentDevicePixelRatio) || 1);
    chart.data.datasets.forEach((_dataset, datasetIndex) => {
      const meta = chart.getDatasetMeta(datasetIndex);
      if (!meta || meta.hidden) return;
      meta.data.forEach((element) => {
        const x = Number(element && element.x);
        const y = Number(element && element.y);
        if (!Number.isFinite(x) || !Number.isFinite(y)) return;
        element.x = Math.round(x * devicePixelRatio) / devicePixelRatio;
        element.y = (Math.round(y * devicePixelRatio) + 0.5) / devicePixelRatio;
      });
    });
  },
};

const timeAxisFadePlugin = {
  id: "timeAxisFade",
  beforeDatasetsDraw(chart) {
    const scale = chart.scales && chart.scales.x;
    const area = chart.chartArea;
    if (!scale || !area) return;
    const ticks = chartTimeTicks(chart, scale);
    if (!ticks.length) return;
    const ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = "#171a28";
    ctx.lineWidth = 1;
    ctx.setLineDash([]);
    ticks.forEach((tick) => {
      const x = Math.round(tick.x) + 0.5;
      if (x < area.left || x > area.right) return;
      ctx.beginPath();
      ctx.moveTo(x, area.top);
      ctx.lineTo(x, area.bottom);
      ctx.stroke();
    });
    ctx.restore();
  },
  afterDraw(chart) {
    const scale = chart.scales && chart.scales.x;
    const area = chart.chartArea;
    if (!scale || !area) return;
    const ticks = chartTimeTicks(chart, scale);
    if (!ticks.length) return;
    chart.$drawnTimeTicks = ticks;
    const font = scale.options.ticks.font || {};
    const ctx = chart.ctx;
    ctx.save();
    ctx.fillStyle = "#85819c";
    ctx.font = `${font.size || 9}px ${font.family || "IBM Plex Mono"}`;
    ctx.textBaseline = "top";
    const leftLabel = formatChartTimeValue(scale.min, chart);
    const rightLabel = formatChartTimeValue(scale.max, chart);
    const leftEndpointRight = area.left + 2 + ctx.measureText(leftLabel).width;
    const rightEndpointLeft = area.right - 2 - ctx.measureText(rightLabel).width;
    const tickHalfWidth = ticks.reduce(
      (halfWidth, tick) => Math.max(halfWidth, ctx.measureText(tick.label).width / 2),
      0,
    );
    const leftClear = leftEndpointRight + TIME_LABEL_EDGE_GAP_PX + tickHalfWidth;
    const rightClear = rightEndpointLeft - TIME_LABEL_EDGE_GAP_PX - tickHalfWidth;
    ctx.textAlign = "center";
    ticks.forEach((tick) => {
      const x = Math.round(tick.x);
      const leftAlpha = Math.max(
        0,
        Math.min(1, (x - leftClear) / TIME_LABEL_EDGE_FADE_PX),
      );
      const rightAlpha = Math.max(
        0,
        Math.min(1, (rightClear - x) / TIME_LABEL_EDGE_FADE_PX),
      );
      const alpha = Math.min(leftAlpha, rightAlpha);
      if (alpha <= 0.01) return;
      ctx.globalAlpha = alpha;
      ctx.fillText(tick.label, x, area.bottom + 7);
    });
    ctx.globalAlpha = 1;
    ctx.textAlign = "left";
    ctx.fillText(leftLabel, area.left + 2, area.bottom + 7);
    ctx.textAlign = "right";
    ctx.fillText(rightLabel, area.right - 2, area.bottom + 7);
    ctx.restore();
  },
};

const pointerValuePlugin = {
  id: "pointerValue",
  afterDatasetsDraw(chart) {
    const pointer = chart.$pointerPosition;
    const xScale = chart.scales && chart.scales.x;
    const yScale = chart.scales && chart.scales.y;
    const area = chart.chartArea;
    if (!pointer || !pointer.inside || !xScale || !yScale || !area) return;
    const value = interpolateScaleValueFromTicks(yScale, pointer.y);
    if (!Number.isFinite(value)) return;
    const ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = "rgba(205, 202, 220, 0.28)";
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(area.left, pointer.y);
    ctx.lineTo(area.right, pointer.y);
    ctx.stroke();
    const rawTarget = xScale.getValueForPixel(pointer.x);
    const target = snapChartTimeToFrame(chart, rawTarget);
    const targetX = chart.$timeAxis === false ? xScale.getPixelForValue(target) : pointer.x;
    ctx.beginPath();
    ctx.moveTo(targetX, area.top);
    ctx.lineTo(targetX, area.bottom);
    ctx.stroke();
    ctx.setLineDash([]);
    const tolerance = Number(chart.$tooltipToleranceS) || CHART_TOOLTIP_TOLERANCE_S;
    const actualCutoff = chartActualCutoff(chart);
    const actualEligible = rawTarget <= actualCutoff + 1e-6;
    const fixedFrames = chart.$timeAxis === false;
    (chart.data.datasets || []).forEach((dataset) => {
      if (!dataset || !Array.isArray(dataset.data) || !dataset.data.length) return;
      if (!isDatasetLegendVisible(dataset)) return;
      const label = String(dataset.label || "");
      const isPrediction = !!dataset.$prediction || label.endsWith(" · pred");
      if (!isPrediction && !actualEligible) return;
      const points = isPrediction ? dataset.data : (dataset.$raw || dataset.data);
      let point = fixedFrames
        ? nearestChartPoint(
          points,
          target,
          isPrediction ? Math.max(tolerance * 2, 0.1) : Number.POSITIVE_INFINITY,
          points.length,
        )
        : interpolatedChartPoint(
          points,
          target,
          points.length,
          tolerance * (isPrediction ? 2 : 1),
        );
      if (!point && !isPrediction && !fixedFrames) point = lastChartPointAtOrBefore(points, target);
      if (!point || !Number.isFinite(Number(point.y))) return;
      const pointY = yScale.getPixelForValue(Number(point.y));
      if (!Number.isFinite(pointY)) return;
      ctx.save();
      ctx.globalAlpha = isPrediction ? 0.78 : 1;
      ctx.beginPath();
      ctx.arc(targetX, pointY, 3, 0, Math.PI * 2);
      ctx.fillStyle = "#0a0b14";
      ctx.fill();
      ctx.strokeStyle = dataset.borderColor || "#d7d4e6";
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.restore();
    });
    const label = formatTooltipValue(value);
    ctx.font = "9px IBM Plex Mono";
    const labelWidth = ctx.measureText(label).width + 8;
    const labelHeight = 14;
    const labelX = area.left + 4;
    const labelY = Math.max(area.top + 2, Math.min(pointer.y - labelHeight / 2, area.bottom - labelHeight - 2));
    ctx.fillStyle = "rgba(10, 11, 20, 0.92)";
    ctx.fillRect(labelX, labelY, labelWidth, labelHeight);
    ctx.strokeStyle = "rgba(112, 108, 140, 0.72)";
    ctx.strokeRect(labelX + 0.5, labelY + 0.5, labelWidth - 1, labelHeight - 1);
    ctx.fillStyle = "#d7d4e6";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(label, labelX + 4, labelY + labelHeight / 2 + 0.5);
    ctx.restore();
  },
};

function mkChart(id) {
  const canvas = $(id);
  const panel = canvas && canvas.closest(".chart-wrap");
  const unavailable = (message) => {
    if (!panel) return null;
    panel.classList.add("chart-unavailable");
    const fallback = document.createElement("p");
    fallback.className = "chart-fallback";
    fallback.setAttribute("role", "status");
    fallback.textContent = message;
    canvas.insertAdjacentElement("afterend", fallback);
    return null;
  };
  if (!canvas) return null;
  if (typeof window.Chart !== "function") return unavailable("Charts unavailable — live controls are still active.");
  try {
    const chart = new window.Chart(canvas.getContext("2d"), {
    type: "line",
    data: { labels: [], datasets: [] },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      clip: 0,
      layout: { padding: { bottom: CHART_TIME_AXIS_PADDING } },
      elements: {
        point: { radius: 0, hoverRadius: 0, hoverBorderWidth: 0 },
        line: {
          borderWidth: 1,
          borderCapStyle: "butt",
          borderJoinStyle: "bevel",
          tension: 0,
        },
      },
      interaction: { mode: "nearest", axis: "x", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { enabled: false },
      },
      scales: {
        x: { display: false },
        y: {
          min: -CHART_Y_LIMIT,
          max: CHART_Y_LIMIT,
          ticks: { color: "#6a6780", font: { size: 9, family: "IBM Plex Mono" } },
          grid: { color: "#171a28" },
          border: { color: "#2c3148" },
        },
      },
    },
    plugins: [
      predictionGapPlugin,
      modeMarkerPlugin,
      timeAxisFadePlugin,
      pixelAlignedLinePlugin,
      pointerValuePlugin,
      chartHoverTooltipPlugin,
    ],
    });
    const cursor = document.createElement("span");
    cursor.className = "chart-replay-cursor";
    cursor.hidden = true;
    canvas.parentElement.appendChild(cursor);
    chart.$replayCursorElement = cursor;
    const tooltip = document.createElement("div");
    tooltip.className = "chart-hover-tooltip";
    tooltip.hidden = true;
    canvas.parentElement.appendChild(tooltip);
    chart.$hoverTooltipElement = tooltip;
    chart.$tooltipVisible = true;
    canvas.addEventListener("pointerdown", (event) => {
      if (event.button !== 1) return;
      event.preventDefault();
      chart.$tooltipVisible = !chart.$tooltipVisible;
      if (!chart.$tooltipVisible) {
        hideChartHoverTooltip(chart);
        return;
      }
      const xScale = chart.scales && chart.scales.x;
      const pointer = chart.$pointerPosition;
      if (xScale && pointer && pointer.inside) {
        scheduleChartHoverTooltip(
          chart,
          xScale.getValueForPixel(pointer.x),
          pointer.x,
          pointer.y,
        );
      }
    });
    if (typeof ResizeObserver === "function") {
      chart.$replayResizeObserver = new ResizeObserver(() => {
        requestAnimationFrame(() => positionReplayChartCursor(chart));
      });
      chart.$replayResizeObserver.observe(canvas);
    }
    return chart;
  } catch (err) {
    return unavailable(`Charts unavailable: ${err.message || err}`);
  }
}

const stateChart = mkChart("chart-state");
const actionChart = mkChart("chart-action");

const ROLLOUT_WINDOW_S = 20;
const ROLLOUT_FUTURE_MAX_S = 8;
const ROLLOUT_FUTURE_RATIO = 0.25;
let actionLegendNames = [...JOINT_FALLBACK];
let actionLegendSignature = "";

function legendItemVisible(group, key) {
  if (group === "joints") return chartLegendVisibility.joints.get(key) !== false;
  return chartLegendVisibility[key] !== false;
}

function setLegendItemVisible(group, key, visible) {
  if (group === "joints") chartLegendVisibility.joints.set(key, visible);
  else chartLegendVisibility[key] = visible;
}

function isDatasetLegendVisible(dataset) {
  const label = String(dataset && dataset.label || "");
  const isPrediction = !!dataset.$prediction || label.endsWith(" · pred");
  const name = label.replace(/ · pred$/, "");
  if (isPrediction && chartLegendVisibility.prediction === false) return false;
  if (!isPrediction && chartLegendVisibility.command === false) return false;
  return chartLegendVisibility.joints.get(name) !== false;
}

function applyChartLegendVisibilityToChart(chart) {
  if (!chart) return;
  (chart.data.datasets || []).forEach((dataset, index) => {
    const visible = isDatasetLegendVisible(dataset);
    const meta = chart.getDatasetMeta(index);
    if (meta) meta.hidden = !visible;
  });
  chart.$predictionGapsVisible = chartLegendVisibility.gap !== false;
  chart.$modeMarkersVisible = chartLegendVisibility.mode !== false;
  chart.$showNowLine = showLiveNowLine();
}

function applyChartLegendVisibility() {
  [stateChart, actionChart].forEach((chart) => {
    if (!chart) return;
    applyChartLegendVisibilityToChart(chart);
    chart.update("none");
    const pointer = chart.$pointerPosition;
    const xScale = chart.scales && chart.scales.x;
    if (pointer && pointer.inside && xScale) {
      scheduleChartHoverTooltip(
        chart,
        snapChartTimeToFrame(chart, xScale.getValueForPixel(pointer.x)),
        pointer.x,
        pointer.y,
      );
    }
  });
}

function appendLegendItem(section, className, label, color = "", visibility = null) {
  const item = document.createElement("div");
  item.className = "chart-legend-item";
  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.className = "chart-legend-toggle";
  toggle.checked = visibility ? legendItemVisible(visibility.group, visibility.key) : true;
  toggle.setAttribute("aria-label", `Show ${label}`);
  if (visibility) {
    toggle.addEventListener("change", () => {
      setLegendItemVisible(visibility.group, visibility.key, toggle.checked);
      applyChartLegendVisibility();
    });
  }
  const swatch = document.createElement("span");
  swatch.className = className;
  if (color) swatch.style.backgroundColor = color;
  const text = document.createElement("span");
  text.className = "chart-legend-label";
  text.textContent = label;
  text.title = label;
  item.append(toggle, swatch, text);
  section.appendChild(item);
}

function bindChartTimeBasisSelect(select) {
  select.addEventListener("change", () => {
    if (replayActive) return;
    chartTimeBasis = select.value;
    [stateChart, actionChart].forEach((chart) => {
      if (!chart) return;
      chart.$timeBasis = chartTimeBasis;
      const now = Number.isFinite(Number(chart.$nowTime)) ? Number(chart.$nowTime) : liveTimestamp(last);
      chart.options.scales.x = liveTimeAxis(now);
      hideChartHoverTooltip(chart);
      chart.update("none");
    });
    updateLegendTimeInfo();
  });
}

function bindChartScaleSelect(select) {
  select.addEventListener("change", () => {
    if (replayActive) return;
    const nextScale = Number(select.value);
    if (!CHART_SCALE_OPTIONS.some((option) => option.seconds === nextScale)) return;
    chartScaleSeconds = nextScale;
    try { localStorage.setItem(CHART_SCALE_STORAGE_KEY, String(chartScaleSeconds)); } catch { /* ignore */ }
    [stateChart, actionChart].forEach((chart) => {
      if (!chart) return;
      chart.$scaleSeconds = chartScaleSeconds;
      const now = Number.isFinite(Number(chart.$nowTime)) ? Number(chart.$nowTime) : liveTimestamp(last);
      refreshLiveChartSeries(chart, now, true);
      chart.options.scales.x = liveTimeAxis(now);
      hideChartHoverTooltip(chart);
      chart.update("none");
    });
    updateLegendTimeInfo();
  });
}

function updateLegendTimeInfo() {
  const unitSelect = $("chart-time-basis");
  if (unitSelect) {
    unitSelect.disabled = replayActive;
    unitSelect.value = replayActive ? "start" : chartTimeBasis;
  }
  const scaleSelect = $("chart-scale");
  if (scaleSelect) {
    scaleSelect.disabled = replayActive;
    scaleSelect.value = String(chartScaleSeconds);
  }
}

function renderActionLegend(names = actionLegendNames) {
  const legend = $("action-legend");
  if (!legend) return;
  const seriesNames = names && names.length ? names : jointNames();
  const rolloutMode = !replayActive && currentControlMode() === "rollout";
  const signature = [
    seriesNames.join("|"),
    replayActive ? "replay" : "live",
    rolloutMode ? "rollout" : "other",
  ].join("::");
  if (signature === actionLegendSignature) return;
  actionLegendSignature = signature;
  const colorIndex = new Map(seriesNames.map((name, index) => [name, index]));
  const canonical = jointNames().filter((name) => colorIndex.has(name));
  const extras = seriesNames.filter((name) => !canonical.includes(name));
  const displayNames = [...canonical, ...extras].reverse();
  legend.replaceChildren();

  const series = document.createElement("section");
  series.className = "chart-legend-section";
  const seriesTitle = document.createElement("div");
  seriesTitle.className = "chart-legend-title";
  seriesTitle.textContent = "Joint colors";
  series.appendChild(seriesTitle);
  displayNames.forEach((name) => {
    const index = colorIndex.get(name) || 0;
    appendLegendItem(
      series,
      "chart-legend-color",
      name,
      PAL[index % PAL.length],
      { group: "joints", key: name },
    );
  });

  const lines = document.createElement("section");
  lines.className = "chart-legend-section";
  const linesTitle = document.createElement("div");
  linesTitle.className = "chart-legend-title";
  linesTitle.textContent = "Line style";
  lines.appendChild(linesTitle);
  appendLegendItem(lines, "chart-legend-line", "command", "", { group: "style", key: "command" });
  appendLegendItem(lines, "chart-legend-line dashed", "prediction", "", { group: "style", key: "prediction" });
  appendLegendItem(lines, "chart-legend-band", "prediction gap", "", { group: "style", key: "gap" });
  if (!replayActive) {
    appendLegendItem(lines, "chart-legend-line mode", "mode change", "", { group: "style", key: "mode" });
  }
  if (rolloutMode) {
    appendLegendItem(lines, "chart-legend-line now", "current time", "", { group: "style", key: "now" });
  }

  const time = document.createElement("section");
  time.className = "chart-legend-section";
  const timeTitle = document.createElement("div");
  timeTitle.className = "chart-legend-title";
  timeTitle.textContent = "Time";
  time.appendChild(timeTitle);
  const timeControl = document.createElement("label");
  timeControl.className = "chart-time-control";
  const timeLabel = document.createElement("span");
  timeLabel.className = "chart-time-unit";
  timeLabel.textContent = "unit";
  const select = document.createElement("select");
  select.id = "chart-time-basis";
  select.setAttribute("aria-label", "Chart time unit");
  [
    ["wall", "wall"],
    ["mode", "mode"],
    ["start", "start"],
  ].forEach(([value, label]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.appendChild(option);
  });
  bindChartTimeBasisSelect(select);
  timeControl.append(timeLabel, select);
  time.appendChild(timeControl);
  const scaleControl = document.createElement("label");
  scaleControl.className = "chart-time-control";
  const scaleLabel = document.createElement("span");
  scaleLabel.className = "chart-time-unit";
  scaleLabel.textContent = "scale";
  const scaleSelect = document.createElement("select");
  scaleSelect.id = "chart-scale";
  scaleSelect.setAttribute("aria-label", "Chart time scale");
  CHART_SCALE_OPTIONS.forEach(({ seconds, label }) => {
    const option = document.createElement("option");
    option.value = String(seconds);
    option.textContent = label;
    scaleSelect.appendChild(option);
  });
  bindChartScaleSelect(scaleSelect);
  scaleControl.append(scaleLabel, scaleSelect);
  time.appendChild(scaleControl);
  legend.append(series, lines, time);
  updateLegendTimeInfo();
}

function currentControlMode() {
  return (last && (last.display_mode || last.mode)) || "";
}

function liveTimestamp(status) {
  const timestamp = Number(status && status.ts);
  return Number.isFinite(timestamp)
    ? timestamp
    : WALL_CLOCK_OFFSET_S + performance.now() / 1000;
}

function showLiveNowLine() {
  return !replayActive
    && currentControlMode() === "rollout"
    && chartLegendVisibility.now !== false;
}

function rolloutFutureWindowS(now) {
  if (currentControlMode() !== "rollout") return 0;
  return Math.min(
    ROLLOUT_FUTURE_MAX_S,
    Math.max(1, chartScaleSeconds * ROLLOUT_FUTURE_RATIO),
  );
}

function liveTimeWindow(now) {
  const future = rolloutFutureWindowS(now);
  const padding = future > 0
    ? future
    : Math.min(LIVE_RIGHT_PADDING_S, Math.max(0.05, chartScaleSeconds * 0.02));
  return { min: now - chartScaleSeconds, max: now + padding };
}

function liveTimeScale(showTicks = true) {
  return {
    type: "linear",
    display: showTicks,
    min: 0,
    ticks: {
      display: false,
      font: { size: 9, family: "IBM Plex Mono" },
      maxTicksLimit: 7,
    },
    grid: { display: false },
    border: { color: "#2c3148" },
  };
}

function liveTimeAxis(now) {
  const window = liveTimeWindow(now);
  return {
    ...liveTimeScale(true),
    min: window.min,
    max: window.max,
  };
}

function syncLiveChartChrome(now) {
  const timestamp = Number(now);
  if (!Number.isFinite(timestamp)) return;
  const latestMarker = liveModeMarkers.length ? liveModeMarkers[liveModeMarkers.length - 1] : null;
  liveModeMarkers = liveModeMarkers.filter(
    (marker) => marker === latestMarker || marker.x >= timestamp - LIVE_MODE_HISTORY_S,
  );
  [stateChart, actionChart].forEach((chart) => {
    if (!chart || chart.$timeAxis !== true) return;
    if (Number.isFinite(Number(chart.$historyStart))) chart.$historyEnd = timestamp;
    chart.$modeMarkers = liveModeMarkers;
    chart.$nowTime = timestamp;
    chart.$showNowLine = showLiveNowLine();
    chart.options.scales.x = liveTimeAxis(timestamp);
    scheduleChartUpdate(chart);
  });
  renderActionLegend();
  updateLegendTimeInfo(timestamp);
}

function recordModeTransition(mode, now) {
  const timestamp = Number(now);
  const nextMode = String(mode || "");
  if (!nextMode || !Number.isFinite(timestamp)) return;
  if (lastDisplayMode && nextMode !== lastDisplayMode) {
    liveModeMarkers.push({ x: timestamp, mode: nextMode });
    const latest = liveModeMarkers[liveModeMarkers.length - 1];
    liveModeMarkers = liveModeMarkers
      .filter((marker) => marker === latest || marker.x >= timestamp - LIVE_MODE_HISTORY_S)
      .slice(-200);
  }
  lastDisplayMode = nextMode;
}

function initializeLiveChartAxes() {
  const now = liveTimestamp(last);
  [stateChart, actionChart].forEach((chart) => {
    if (!chart) return;
    chart.$timeAxis = true;
    chart.$live = [];
    chart.$predictions = [];
    chart.$predictionGaps = [];
    chart.$modeMarkers = [];
    chart.$nowTime = now;
    chart.$showNowLine = false;
    chart.$timeBasis = chartTimeBasis;
    chart.$historyStart = Number.NaN;
    chart.$historyEnd = Number.NaN;
    chart.$tooltipToleranceS = CHART_TOOLTIP_TOLERANCE_S;
    chart.$scaleSeconds = chartScaleSeconds;
    chart.$lastAnimationNow = Number.NaN;
    chart.$pendingAnimationNow = Number.NaN;
    chart.$lastRenderedNow = Number.NaN;
    chart.options.scales.x = liveTimeAxis(now);
  });
}

initializeLiveChartAxes();
renderActionLegend();
if (typeof requestAnimationFrame === "function") {
  chartAnimationFrame = requestAnimationFrame(animateLiveCharts);
}

// Live charts own `$live` (measured values) and `$predictions` (dashed overlays)
// so Chart.js never has to guess which dataset is which.
function syncChartDatasets(chart) {
  const next = (chart.$live || []).concat(chart.$predictions || []);
  const current = chart.data.datasets;
  const unchanged = current.length === next.length && next.every((ds, i) => current[i] === ds);
  if (!unchanged) chart.data.datasets = next;
  applyChartLegendVisibilityToChart(chart);
}

function pushChart(chart, scalars, timeS = null) {
  if (!chart) return;
  const keys = Object.keys(scalars);
  if (chart === actionChart && keys.length) {
    actionLegendNames = keys;
    renderActionLegend(actionLegendNames);
  }
  const now = Number.isFinite(Number(timeS)) ? Number(timeS) : liveTimestamp(last);
  if (chart.$timeAxis !== true) {
    chart.$timeAxis = true;
    chart.data.labels = [];
    chart.$live = [];
    chart.$predictions = [];
    chart.$predictionGaps = [];
    chart.$modeMarkers = [];
    chart.$historyStart = Number.NaN;
    chart.$historyEnd = Number.NaN;
    chart.data.datasets = [];
    syncChartDatasets(chart);
  }
  chart.$historyStart = Number.isFinite(Number(chart.$historyStart)) ? Number(chart.$historyStart) : now;
  chart.$historyEnd = now;
  chart.$showNowLine = showLiveNowLine();
  chart.$modeMarkers = liveModeMarkers;
  const live = chart.$live || (chart.$live = []);
  while (live.length < keys.length) {
    live.push({
      label: "",
      data: [],
      $raw: [],
      borderColor: PAL[live.length % PAL.length],
      borderWidth: 1,
      borderCapStyle: "butt",
      borderJoinStyle: "bevel",
      pointRadius: 0,
      pointHoverRadius: 0,
      tension: 0,
      clip: 0,
    });
  }
  live.forEach((ds, i) => { if (keys[i]) ds.label = keys[i]; });
  chart.$nowTime = now;
  chart.options.scales.x = liveTimeAxis(now);
  keys.forEach((k, i) => {
    const ds = live[i];
    const raw = ds.$raw || (ds.$raw = []);
    const value = Number(scalars[k]);
    const last = raw[raw.length - 1];
    if (last && Math.abs(Number(last.x) - now) < 1e-6) {
      last.y = value;
    } else {
      raw.push({ x: now, y: value });
    }
    if (raw.length > HISTORY_CAPACITY + 512) {
      raw.splice(0, raw.length - HISTORY_CAPACITY);
    }
  });
  syncChartDatasets(chart);
  scheduleChartUpdate(chart);
}

function rolloutPredictionList() {
  if (!Array.isArray(vizState.rolloutPredictions)) vizState.rolloutPredictions = [];
  return vizState.rolloutPredictions;
}

function recordRolloutPrediction(prediction, now, taskElapsed = now) {
  const actions = prediction && Array.isArray(prediction.actions) ? prediction.actions : [];
  const start = Number(prediction && prediction.t_s);
  if (!actions.length || !Number.isFinite(start)) return;
  const step = Number(prediction.step_s) > 0 ? Number(prediction.step_s) : 1 / 30;
  const elapsed = Number(taskElapsed);
  const publishedAfterStatus = Number.isFinite(elapsed) ? Math.max(0, start - elapsed) : 0;
  const chartStart = now + publishedAfterStatus;
  const key = String(prediction.id || `${start.toFixed(3)}#${actions.length}`);
  const list = rolloutPredictionList();
  if (list.some((chunk) => chunk.key === key)) return;
  const points = [];
  actions.forEach((action, index) => {
    const joints = action && action.joints ? action.joints : action;
    if (!joints || typeof joints !== "object") return;
    points.push({ x: chartStart + (index + 1) * step, joints });
  });
  if (!points.length) return;
  const end = points[points.length - 1].x;
  // A newer chunk replaces the older predictions inside the window it covers.
  const kept = [];
  list.forEach((chunk) => {
    const surviving = chunk.points.filter((point) => point.x < chartStart || point.x > end);
    if (surviving.length) kept.push({ ...chunk, points: surviving });
  });
  kept.push({
    key,
    start: chartStart,
    end,
    step,
    points,
    strategy: prediction.strategy || "",
    degraded: !!prediction.degraded,
    latency_ms: Number(prediction.latency_ms) || 0,
  });
  const horizon = now - ROLLOUT_WINDOW_S;
  vizState.rolloutPredictions = kept
    .filter((chunk) => chunk.points[chunk.points.length - 1].x > horizon)
    .slice(-40);
}

function rolloutOverlaySeries(now) {
  const chunks = [...rolloutPredictionList()].sort((a, b) => a.start - b.start);
  const series = new Map();
  chunks.forEach((chunk) => {
    chunk.points.forEach((point) => {
      Object.entries(point.joints || {}).forEach(([name, value]) => {
        const y = Number(value);
        if (!Number.isFinite(y)) return;
        if (!series.has(name)) series.set(name, { points: [], gaps: [] });
        series.get(name).points.push({ x: point.x, y, step: chunk.step });
      });
    });
  });
  series.forEach((entry) => {
    const ordered = entry.points
      .filter((point) => point.x >= now - ROLLOUT_WINDOW_S)
      .sort((a, b) => a.x - b.x);
    const merged = [];
    let previous = null;
    ordered.forEach((point) => {
      if (previous && point.x - previous.x > 2.5 * Math.max(previous.step || 0, point.step || 0)) {
        merged.push({ x: (previous.x + point.x) / 2, y: null });
        entry.gaps.push({ start: previous.x, end: point.x });
      }
      merged.push(point);
      previous = point;
    });
    // A horizon that lapsed without a fresh chunk is a break too.
    if (previous && Number.isFinite(now) && now - previous.x > 2.5 * (previous.step || 0)) {
      entry.gaps.push({ start: previous.x, end: now });
    }
    entry.points = merged;
  });
  return series;
}

function applyRolloutOverlay(chart, now) {
  if (!chart || !chart.$timeAxis) return;
  const colorByName = new Map((chart.$live || []).map((ds) => [ds.label, ds.borderColor]));
  const names = jointNames();
  const datasets = [];
  const gapByTime = new Map();
  rolloutOverlaySeries(now).forEach((entry, name) => {
    entry.gaps.forEach((gap) => {
      gapByTime.set(`${gap.start}:${gap.end}`, gap);
    });
    if (!entry.points.length) return;
    const color = colorByName.get(name) || PAL[Math.max(0, names.indexOf(name)) % PAL.length];
    datasets.push({
      label: `${name} · pred`,
      data: entry.points,
      borderColor: color,
      borderWidth: 1,
      borderCapStyle: "butt",
      borderJoinStyle: "bevel",
      borderDash: [4, 3],
      pointRadius: 0,
      pointHoverRadius: 0,
      tension: 0,
      spanGaps: false,
      clip: 0,
      $prediction: true,
    });
  });
  chart.$predictionGaps = [...gapByTime.values()];
  chart.$predictions = datasets;
  syncChartDatasets(chart);
  const window = liveTimeWindow(now);
  chart.$nowTime = now;
  chart.$showNowLine = showLiveNowLine();
  chart.$modeMarkers = liveModeMarkers;
  chart.$historyEnd = now;
  chart.options.scales.x = {
    ...liveTimeScale(true),
    min: window.min,
    max: window.max,
  };
  scheduleChartUpdate(chart);
}

function renderRolloutOverlays(now) {
  applyRolloutOverlay(stateChart, now);
  applyRolloutOverlay(actionChart, now);
}

function clearRolloutPredictions() {
  const hasOverlay = (chart) => !!(
    chart
    && ((chart.$predictions || []).length || (chart.$predictionGaps || []).length)
  );
  const hasChunks = rolloutPredictionList().length > 0;
  if (!hasChunks && !hasOverlay(stateChart) && !hasOverlay(actionChart)) return;
  vizState.rolloutPredictions = [];
  [stateChart, actionChart].forEach((chart) => {
    if (!hasOverlay(chart)) return;
    chart.$predictions = [];
    chart.$predictionGaps = [];
    syncChartDatasets(chart);
    scheduleChartUpdate(chart);
  });
}

function limitsFor(name) {
  const lim = (meta.limits || {})[name];
  if (lim) return [lim.min, lim.max];
  return name === "gripper" ? [0, 100] : [-180, 180];
}

function flushLive() {
  liveTimer = null;
  const joints = pendingLive;
  pendingLive = {};
  if (!Object.keys(joints).length) return;
  api("/api/joints", { joints, live: true, duration_s: 0 }).catch(toastError);
}

function queueLive(name, value) {
  exitReplayForControl();
  followSliders = taskFollowing();
  if (followSliders) return;
  pendingLive[name] = value;
  if (liveTimer) return;
  liveTimer = setTimeout(flushLive, 40);
}

function taskFollowing() {
  const mode = (last && (last.display_mode || last.mode)) || "";
  return mode === "teleop" || mode === "record" || mode === "rollout" || (replayActive && armReplay);
}

function ensureJointRows(names) {
  const root = $("joint-rows");
  if (root.dataset.ready === "1") return;
  root.innerHTML = "";
  // Gripper at the top of the panel, base (shoulder_pan) at the bottom.
  [...names].reverse().forEach((name) => {
    const [lo, hi] = limitsFor(name);
    const row = document.createElement("div");
    row.className = "j-row";
    row.innerHTML = `
      <span class="j-name" title="${name}">${name}
        <div class="j-cur" data-cur="${name}">cur —</div>
      </span>
      <input type="range" min="${lo}" max="${hi}" step="0.1" value="0" data-joint="${name}"/>
      <input class="j-val" type="number" min="${lo}" max="${hi}" step="0.1" value="0" data-val="${name}"/>`;
    const slider = row.querySelector("input[type='range']");
    const num = row.querySelector("input[type='number']");
    const applyValue = (raw) => {
      const [minV, maxV] = limitsFor(name);
      let value = Number(raw);
      if (Number.isNaN(value)) return;
      value = Math.min(maxV, Math.max(minV, value));
      targets[name] = value;
      slider.value = String(value);
      num.value = fmt(value);
      queueLive(name, value);
    };
    slider.addEventListener("input", () => applyValue(slider.value));
    num.addEventListener("change", () => applyValue(num.value));
    num.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        applyValue(num.value);
        num.blur();
      }
    });
    root.appendChild(row);
  });
  root.dataset.ready = "1";
}

function updateJoints(joints) {
  const names = meta.joints.length ? meta.joints : JOINT_FALLBACK;
  ensureJointRows(names);
  const active = document.activeElement;
  followSliders = taskFollowing();
  names.forEach((name) => {
    const cur = joints[name];
    const el = document.querySelector(`[data-cur="${name}"]`);
    if (el) el.textContent = `cur ${fmt(cur)}`;
    const slider = document.querySelector(`input[data-joint="${name}"]`);
    const val = document.querySelector(`[data-val="${name}"]`);
    const row = slider && slider.closest(".j-row");
    const editing = active && (active.dataset.joint === name || active.dataset.val === name);
    if (cur != null && !editing && (followSliders || targets[name] == null)) {
      targets[name] = cur;
      if (slider) slider.value = String(cur);
      if (val) val.value = fmt(cur);
    }
    const shown = slider ? Number(slider.value) : targets[name];
    const tol = MATCH_TOL[name] ?? 2;
    const matched = cur != null && shown != null && Math.abs(Number(shown) - Number(cur)) <= tol;
    if (row) {
      row.classList.toggle("matched", matched);
      row.classList.toggle("unmatched", !matched && cur != null);
    }
  });
}

function setCamGrid(n) {
  const cols = n <= 1 ? 1 : n <= 2 ? 2 : n <= 4 ? 2 : 3;
  const rows = Math.ceil(Math.max(n, 1) / cols);
  $("cameras").style.setProperty("--cc", cols);
  $("cameras").style.setProperty("--cr", rows);
}

const camCards = {};

function addCamCard(id, label, src, metaText) {
  if (camCards[id]) {
    const el = camCards[id].querySelector("[data-cam-meta]");
    if (el && metaText) el.textContent = metaText;
    const title = camCards[id].querySelector("[data-cam-title]");
    if (title && label) title.textContent = label;
    return camCards[id];
  }
  const host = $("cameras");
  const card = document.createElement("article");
  card.className = "cam-card";
  card.innerHTML = `
    <div class="cam-lbl"><span data-cam-title="${id}">${label}</span><span data-cam-meta="${id}">${metaText || "…"}</span></div>
    <div class="no-sig">No signal</div>
    <img alt="${label}"/>`;
  const img = card.querySelector("img");
  img.dataset.mjpegSrc = src;
  img.addEventListener("load", () => {
    card.classList.add("has-sig");
    img.classList.add("live");
    syncSnapshotButton();
    renderDebugPanel();
  });
  host.appendChild(card);
  camCards[id] = card;
  observeMjpeg(img);
  setCamGrid(Object.keys(camCards).length || 1);
  return card;
}

function syncCamCards(ids) {
  Object.keys(camCards).forEach((id) => {
    if (!ids.includes(id)) {
      if (mjpegObserver) {
        const img = camCards[id].querySelector("img[data-mjpeg-src]");
        if (img) mjpegObserver.unobserve(mjpegObservedTarget(img));
      }
      camCards[id].remove();
      delete camCards[id];
    }
  });
  setCamGrid(Object.keys(camCards).length || 1);
}

function renderMainCameras(list) {
  const streaming = (list || []).filter((c) => c.enabled && c.show_main);
  const ids = streaming.map((c) => String(c.name));
  streaming.forEach((cam) => {
    const metaText = cam.remote
      ? `${cam.fps || 0} fps · Blender${cam.connected ? "" : " · offline"}`
      : `${cam.fps} fps · :${cam.port}`;
    addCamCard(
      String(cam.name),
      cam.label || `cam ${cam.name}`,
      `${BASE}/camera/${encodeURIComponent(cam.name)}`,
      metaText,
    );
  });
  syncCamCards(ids);
  const empty = $("camera-empty");
  if (empty) empty.classList.toggle("hidden", streaming.length > 0);
  $("cameras").dataset.empty = streaming.length ? "0" : "1";
}

let camMenuKey = "";

function renderCamMenu(list) {
  camMenu = list || [];
  const root = $("cam-rows");
  if (!root) return;
  const key = camMenu.map((c) => `${c.name}:${c.label}:${c.enabled}:${c.show_main}:${c.feed_robot}:${c.streaming}:${c.width}x${c.height}:${c.port}:${c.connected}:${c.remote}:${c.url}:${c.error}`).join("|");
  if (key === camMenuKey) return;
  camMenuKey = key;
  const ae = document.activeElement;
  const focused = ae && root.contains(ae) && ["INPUT", "TEXTAREA", "SELECT"].includes(ae.tagName);
  if (focused) return;
  if (mjpegObserver) {
    root.querySelectorAll("img[data-mjpeg-src]").forEach((img) => mjpegObserver.unobserve(mjpegObservedTarget(img)));
  }
  root.innerHTML = "";
  if (!camMenu.length) {
    root.innerHTML = `<p class="bus-hint">No devices. Rescan after plugging in a camera.</p>`;
    return;
  }
  camMenu.forEach((cam) => {
    const card = document.createElement("div");
    card.className = "cam-dev";
    const name = String(cam.name);
    const shown = cam.label || `cam ${name}`;
    const remote = Boolean(cam.remote);
    const active = remote ? Boolean(cam.connected) : Boolean(cam.streaming);
    const statusText = remote
      ? (cam.connected ? "Blender live" : "Blender offline")
      : (cam.streaming ? "stream on" : "local only");
    const localControls = remote ? "" : `
      <div class="pair">
        <label>Width <input type="number" data-w="${name}" value="${cam.width}" min="16" step="1"/></label>
        <label>Height <input type="number" data-h="${name}" value="${cam.height}" min="16" step="1"/></label>
      </div>
      <label>Network port
        <input type="number" data-port="${name}" value="${cam.port}" min="1" max="65535"/>
      </label>
      <label class="check"><input type="checkbox" data-af="${name}" ${cam.autofocus ? "checked" : ""}/> Autofocus</label>
      <label>Focus
        <input type="range" data-focus="${name}" min="${cam.focus_min ?? 0}" max="${cam.focus_max ?? 255}" step="1" value="${cam.focus ?? 0}" ${cam.autofocus ? "disabled" : ""}/>
      </label>`;
    const sourceStatus = remote
      ? `<p class="hw-status ${cam.connected ? "on" : ""}">${cam.connected ? "connected" : "waiting"} · ${cam.url || "no URL"}${cam.error ? ` · ${cam.error}` : ""}</p>`
      : `<p class="hw-status ${cam.streaming ? "on" : ""}">${cam.streaming ? `http://127.0.0.1:${cam.port}/video` : "network stream off"}</p>`;
    const actions = remote ? "" : `
      <div class="row-actions">
        <button type="button" data-apply="${name}">Apply size</button>
        <button type="button" data-stream="${name}">${cam.streaming ? "Stop stream" : "Open network stream"}</button>
      </div>`;
    card.innerHTML = `
      <header>
        <h3>${shown}</h3>
        <span class="${active ? "on" : "off"}">${statusText}</span>
      </header>
      <img class="mini" alt="preview ${shown}" data-mjpeg-src="${BASE}/camera/${encodeURIComponent(name)}"/>
      <label>Name
        <input type="text" data-label="${name}" value="${shown}" placeholder="front / side"/>
      </label>
      ${localControls}
      <div class="checks">
        <label class="check"><input type="checkbox" data-enabled="${name}" ${cam.enabled ? "checked" : ""}/> Enable</label>
        <label class="check"><input type="checkbox" data-main="${name}" ${cam.show_main ? "checked" : ""} ${cam.enabled ? "" : "disabled"}/> Main view</label>
        <label class="check"><input type="checkbox" data-robot="${name}" ${cam.feed_robot ? "checked" : ""} ${cam.enabled && cam.show_main ? "" : "disabled"}/> Robot input</label>
      </div>
      ${sourceStatus}
      ${actions}`;
    root.appendChild(card);
  });
  root.querySelectorAll("img[data-mjpeg-src]").forEach(observeMjpeg);
}

async function applyCamSize(name) {
  const width = Number(document.querySelector(`[data-w="${name}"]`).value);
  const height = Number(document.querySelector(`[data-h="${name}"]`).value);
  await api(`/api/cameras/${encodeURIComponent(name)}/resolution`, { width, height });
}

async function toggleCamStream(name) {
  const cam = camMenu.find((c) => String(c.name) === String(name));
  const port = Number(document.querySelector(`[data-port="${name}"]`).value);
  await api(`/api/cameras/${encodeURIComponent(name)}/stream`, {
    enable: !(cam && cam.streaming),
    port,
  });
}

$("cam-rows").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button");
  if (!btn) return;
  try {
    if (btn.dataset.apply) await applyCamSize(btn.dataset.apply);
    if (btn.dataset.stream) {
      await toggleCamStream(btn.dataset.stream);
      camMenuKey = "";
      btn.blur();
    }
  } catch (err) { toastError(err); }
});
$("cam-rows").addEventListener("change", async (ev) => {
  const el = ev.target;
  try {
    if (el.dataset.label) {
      await api(`/api/cameras/${encodeURIComponent(el.dataset.label)}/label`, { label: el.value });
    } else if (el.dataset.enabled) {
      await api(`/api/cameras/${encodeURIComponent(el.dataset.enabled)}/flags`, { enabled: el.checked });
    } else if (el.dataset.main) {
      await api(`/api/cameras/${encodeURIComponent(el.dataset.main)}/flags`, { show_main: el.checked });
    } else if (el.dataset.robot) {
      await api(`/api/cameras/${encodeURIComponent(el.dataset.robot)}/flags`, { feed_robot: el.checked });
    } else if (el.dataset.af) {
      await api(`/api/cameras/${encodeURIComponent(el.dataset.af)}/focus`, { autofocus: el.checked });
      const slider = document.querySelector(`[data-focus="${el.dataset.af}"]`);
      if (slider) slider.disabled = el.checked;
    } else {
      return;
    }
    camMenuKey = "";
  } catch (err) { toastError(err); }
});
$("cam-rows").addEventListener("input", (ev) => {
  const el = ev.target;
  if (!el.dataset.focus) return;
  clearTimeout(el._focusTimer);
  el._focusTimer = setTimeout(() => {
    api(`/api/cameras/${encodeURIComponent(el.dataset.focus)}/focus`, { focus: Number(el.value) }).catch(toastError);
  }, 80);
});

let lastLogKey = "";
function logIsBeingSelected() {
  const ol = $("log");
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !ol) return false;
  return ol.contains(sel.anchorNode) || ol.contains(sel.focusNode);
}
function renderLogs(logs) {
  const ol = $("log");
  if (!ol) return;
  const key = (logs || []).map((e) => `${e.t}|${e.level}|${e.message}`).join("\n");
  if (key === lastLogKey) return;
  if (logIsBeingSelected()) return;
  lastLogKey = key;
  ol.innerHTML = "";
  (logs || []).slice().reverse().forEach((entry) => {
    const li = document.createElement("li");
    if (entry.level === "error") li.className = "error";
    li.textContent = `${entry.t || ""}  ${entry.message}`;
    ol.appendChild(li);
  });
}

async function refreshSessions() {
  try {
    const items = await fetch(BASE + "/api/sessions").then((r) => r.json());
    const ol = $("sessions");
    ol.innerHTML = "";
    items.slice(0, 12).forEach((s) => {
      const li = document.createElement("li");
      const id = s.session_id || s.id || "";
      const tail = (s.log_tail || []).slice(-1)[0] || `${s.kind || "session"}  ${s.frames ?? 0}f`;
      li.textContent = `${id}  ${tail}`;
      li.title = (s.log_tail || []).join("\n");
      ol.appendChild(li);
    });
  } catch { /* ignore */ }
}

function applyStatus(d) {
  last = d;
  const backendMode = d.display_mode || d.mode || "offline";
  const pending = d.task && d.task.pending;
  if (episodeSource && (pending || ["loading", "jog", "teleop", "record", "rollout"].includes(backendMode))) {
    closeEpisodeSelection();
  }
  const mode = backendMode;
  const pill = $("mode-pill");
  pill.textContent = replayActive ? "REPLAY" : mode;
  pill.className = `pill ${replayActive ? "replay" : mode}`;
  $("fps").textContent = `${Number(d.fps || 0).toFixed(1)} Hz`;

  const robot = d.robot || {};
  const leader = d.leader || {};
  busLive = !!robot.connected;
  $("joint-panel").classList.toggle("bus-live", busLive);

  syncDevicePowerButtons(robot, leader);
  const envEl = $("st-env");
  if (envEl) {
    const label = d.runtime_label || (meta && meta.runtime_label) || "";
    envEl.textContent = label ? `env ${label}` : "env —";
    const cudaOk = d.runtime ? d.runtime.cuda : (meta.runtime && meta.runtime.cuda);
    envEl.classList.toggle("warn", cudaOk === false);
  }
  setTaskButton("btn-hdr-teleop", mode === "teleop" || pending === "teleop_start", "teleop");
  setTaskButton("btn-hdr-record", mode === "record" || pending === "record_start", "record");
  setTaskButton("btn-hdr-rollout", mode === "rollout" || pending === "rollout_start", "rollout");
  syncStopButton(mode, pending);
  ["btn-hdr-teleop", "btn-hdr-record", "btn-hdr-rollout", "btn-hdr-capture", "btn-hdr-relax"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    const on = pending && (
      (id === "btn-hdr-teleop" && pending === "teleop_start")
      || (id === "btn-hdr-record" && pending === "record_start")
      || (id === "btn-hdr-rollout" && pending === "rollout_start")
      || (id === "btn-hdr-capture" && pending === "capture_start")
      || (id === "btn-hdr-relax" && pending === "preset")
    );
    el.classList.toggle("pending", !!on);
  });
  const cap = $("btn-hdr-capture");
  capturing = !!(d.task && d.task.recording);
  if (cap) cap.classList.toggle("rec-on", capturing && mode !== "record");
  autoRecord = !!(d.task && d.task.auto_record);
  const autoBtn = $("btn-hdr-auto");
  if (autoBtn) autoBtn.classList.toggle("auto-on", autoRecord);
  if (d.task && (d.task.video_id || d.task.session_id)) {
    selectedVideoId = d.task.video_id || d.task.session_id;
  }
  syncSnapshotButton();
  updateResumeTargetUi();
  setHwStatus("arm-status", robot);
  setHwStatus("leader-status", leader);
  const hwKey = `${!!robot.connected}:${robot.port || ""}:${!!leader.connected}:${leader.port || ""}`;
  if (hwKey !== lastHwKey) {
    lastHwKey = hwKey;
    refreshPorts();
  }
  updateTaskInfo();

  const task = d.task || {};
  $("st-elapsed").textContent = fmtTime(task.elapsed_s);
  if (replayActive) {
    $("st-episode").textContent = `replay  ep ${vizState.episode}`;
  } else if (task.resetting && task.reset_time_s) {
    $("st-episode").textContent = `reset  ${fmtTime(task.episode_elapsed_s)} / ${fmtTime(task.reset_time_s)}`;
  } else if (task.episode_time_s) {
    $("st-episode").textContent = `#${task.episode_index}  ${fmtTime(task.episode_elapsed_s)} / ${fmtTime(task.episode_time_s)}`;
  } else {
    $("st-episode").textContent = task.recording ? `#${task.episode_index}` : "—";
  }

  const hold = $("chk-hold");
  if (document.activeElement !== hold) hold.checked = !!d.hold;

  renderMainCameras(d.cameras || []);
  renderCamMenu(d.cameras || []);

  // Live charts share one absolute-time history. Mode changes are annotations,
  // not reasons to discard the curves already on screen.
  const rolloutLive = !replayActive && mode === "rollout";
  const taskElapsedS = Number(task.elapsed_s) || 0;
  const liveNowS = liveTimestamp(d);
  if (!replayActive) recordModeTransition(mode, liveNowS);
  if (d.joints && Object.keys(d.joints).length) {
    updateJoints(d.joints);
    if (!replayActive) pushChart(stateChart, d.joints, liveNowS);
  }
  if (!replayActive && d.action && Object.keys(d.action).length) {
    pushChart(actionChart, d.action, liveNowS);
  }
  if (rolloutLive) {
    const chartNow = Number(actionChart && actionChart.$nowTime);
    recordRolloutPrediction(
      d.prediction,
      Number.isFinite(chartNow) ? chartNow : liveNowS - 1 / LIVE_FRAME_RATE,
      taskElapsedS,
    );
    renderRolloutOverlays(liveNowS);
  } else {
    clearRolloutPredictions();
  }
  if (!replayActive) syncLiveChartChrome(liveNowS);
  if (armReplay && (!robot.connected || mode === "estop")) {
    armReplay = false;
    syncArmToggle();
  }
  if (d.logs) renderLogs(d.logs);
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}${BASE}/ws`);
  ws.onopen = () => { $("ws-dot").className = "dot on"; };
  ws.onclose = () => {
    $("ws-dot").className = "dot err";
    setTimeout(connectWs, 1500);
  };
  ws.onerror = () => { $("ws-dot").className = "dot err"; };
  ws.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      latestStatus = data;
      if (!document.hidden) applyStatus(data);
    } catch { /* ignore */ }
  };
}

document.addEventListener("visibilitychange", () => {
  pageVisible = !document.hidden;
  syncMjpegStreams();
  if (!pageVisible) {
    if (vizState.playing) {
      replayResumeOnVisible = true;
      pauseVizVideos();
    }
    return;
  }
  if (latestStatus) applyStatus(latestStatus);
  if (replayResumeOnVisible && replayActive) playVizVideos();
  replayResumeOnVisible = false;
});

function bind(id, fn) {
  const el = $(id);
  if (!el) return;
  el.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    try { await fn(); } catch (err) { toastError(err); }
  });
}

let savedPresets = { record: {}, rollout: {}, pose: {}, debug: {}, hardware: {} };
const PRESET_SELECTION_KEY = "lerobot-monitor-preset-selection";
const PRESET_SCROLL_KEY = "lerobot-monitor-side-scroll";
const PRESET_KIND_BY_TAB = {
  joints: "pose",
  record: "record",
  rollout: "rollout",
  debug: "debug",
  hardware: "hardware",
};
const PRESET_LABELS = {
  pose: "Joints",
  record: "Record",
  rollout: "Rollout",
  debug: "Debug",
  hardware: "Hardware",
};
let activePresetKind = "pose";
let presetNameMode = "save";
const presetLoadState = { kind: "", name: "", forceSent: false };

function loadJsonStorage(key) {
  try {
    const value = JSON.parse(localStorage.getItem(key) || "{}");
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch {
    return {};
  }
}

function saveJsonStorage(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ }
}

const presetSelections = loadJsonStorage(PRESET_SELECTION_KEY);
const presetScroll = loadJsonStorage(PRESET_SCROLL_KEY);

function selectedPresetName(kind = activePresetKind) {
  const group = savedPresets[kind] || {};
  const selected = String(presetSelections[kind] || "");
  if (selected && group[selected]) return selected;
  if (kind === "hardware" && group["Disconnected"]) return "Disconnected";
  return "";
}

function selectedPreset(kind = activePresetKind) {
  const name = selectedPresetName(kind);
  return name ? (savedPresets[kind] || {})[name] || null : null;
}

function isSystemPreset(kind = activePresetKind, name = selectedPresetName(kind)) {
  const preset = name ? (savedPresets[kind] || {})[name] : null;
  return Boolean(preset && preset.system);
}

function renderPresetToolbar() {
  const select = $("preset-select");
  if (!select) return;
  const group = savedPresets[activePresetKind] || {};
  const current = selectedPresetName();
  select.innerHTML = `<option value="">—</option>`;
  Object.keys(group).sort((left, right) => left.localeCompare(right)).forEach((name) => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    select.appendChild(option);
  });
  select.value = current;
  select.setAttribute("aria-label", `${PRESET_LABELS[activePresetKind]} preset`);
  const system = isSystemPreset();
  const hasSelection = Boolean(current);
  const loading = presetLoadState.kind === activePresetKind;
  for (const [id, disabled] of [
    ["btn-preset-load", !hasSelection && !loading],
    ["btn-preset-rename", !hasSelection || system],
    ["btn-preset-dup", !hasSelection],
    ["btn-preset-del", !hasSelection || system],
  ]) {
    if ($(id)) $(id).disabled = disabled;
  }
  const loadButton = $("btn-preset-load");
  if (loadButton) {
    loadButton.classList.toggle("loading", loading);
    loadButton.title = loading ? "Force load preset" : "Load preset";
    loadButton.setAttribute("aria-label", loading ? "Force load preset" : "Load preset");
  }
}

function selectPresetKind(kind) {
  activePresetKind = kind;
  renderPresetToolbar();
  const panel = document.querySelector(`[data-tab-panel="${kind}"]`);
  if (panel) {
    requestAnimationFrame(() => {
      panel.scrollTop = Number(presetScroll[kind] || 0);
    });
  }
}

function refreshPresetSelects() {
  renderPresetToolbar();
}

function setTaskButton(id, on, label) {
  const el = $(id);
  if (!el) return;
  const lbl = el.querySelector(".hdr-lbl");
  if (lbl) lbl.textContent = label;
  el.classList.toggle("task-on", on);
  el.setAttribute("aria-pressed", String(on));
}

function recordFields() {
  const resume = !!($("chk-rec-resume") && $("chk-rec-resume").checked);
  const actionFps = Number($("rec-action-fps").value) || 15;
  const videoFps = Number($("rec-video-fps").value) || actionFps;
  return {
    task: $("rec-task").value,
    repo_id: $("rec-repo").value,
    episode_time_s: Number($("rec-ep").value) || 20,
    reset_time_s: Number($("rec-reset").value) || 0,
    num_episodes: Number($("rec-num").value) || 50,
    action_fps: actionFps,
    video_fps: videoFps,
    fps: actionFps,
    format: ($("rec-format") && $("rec-format").value) || "mp4",
    root: ($("rec-root") && $("rec-root").value) || "",
    resume,
    video: !($("chk-rec-video") ) || $("chk-rec-video").checked,
    streaming_encoding: !($("chk-rec-stream-enc")) || $("chk-rec-stream-enc").checked,
    encoder_threads: Number($("rec-enc-threads") && $("rec-enc-threads").value) || 2,
    video_id: resume && selectedVideoId ? selectedVideoId : undefined,
    dataset_id: resume && selectedVideoId ? selectedVideoId : undefined,
    merge: true,
  };
}

function updateRecordDestinationUi() {
  const target = $("record-destination");
  if (!target) return;
  const rec = recordFields();
  const root = rec.root || (meta.recording && meta.recording.root) || meta.recording_root || "data/datasets";
  const separator = String(root).includes("\\") ? "\\" : "/";
  const join = (name) => `${String(root).replace(/[\\/]+$/, "")}${separator}${name}`;
  if (rec.resume) {
    target.textContent = rec.video_id
      ? `Destination: ${join(rec.video_id)} (continue)`
      : `Destination root: ${root} · choose a local video to continue`;
    target.classList.toggle("required", !rec.video_id);
    return;
  }
  const base = String(rec.repo_id || rec.task || "capture")
    .trim().replace(/[^a-zA-Z0-9._-]+/g, "_").replace(/^[._-]+|[._-]+$/g, "") || "dataset";
  target.textContent = `Destination: ${join(`${base}_<UTC timestamp>`)}`;
  target.classList.remove("required");
}

function browsedLocalVideoId() {
  return episodeSource && episodeSource.kind === "video" ? episodeSource.id : "";
}

function updateResumeTargetUi() {
  const target = $("resume-target");
  const text = $("resume-target-text");
  const use = $("btn-rec-use-video");
  const clear = $("btn-rec-clear-video");
  const resume = !!($("chk-rec-resume") && $("chk-rec-resume").checked);
  const browsedId = browsedLocalVideoId();
  const targetRow = selectedVideoId ? videosCache.find((row) => row.id === selectedVideoId) : null;
  const exists = !!targetRow;
  if (text) {
    const targetActionFps = targetRow && (targetRow.action_fps || targetRow.fps);
    const targetVideoFps = targetRow && (targetRow.video_fps || targetRow.fps);
    const targetFormat = targetRow && (targetRow.format || targetRow.video_format);
    const targetMeta = [
      targetActionFps ? `${targetActionFps} action Hz` : "",
      targetVideoFps ? `${targetVideoFps} video Hz` : "",
      targetFormat || "",
    ].filter(Boolean).join(" · ");
    text.textContent = exists
      ? `Resume target: ${selectedVideoId}${targetMeta ? ` · target ${targetMeta}` : ""}`
      : (selectedVideoId ? `Resume target unavailable: ${selectedVideoId}` : "Resume target: none");
  }
  if (target) {
    target.classList.toggle("ready", exists);
    target.classList.toggle("required", resume && !exists);
  }
  if (use) {
    use.disabled = !browsedId || browsedId === selectedVideoId;
    use.textContent = browsedId ? `Use ${browsedId}` : "Use browsed video";
  }
  if (clear) clear.disabled = !selectedVideoId;
  updateRecordDestinationUi();
}
function applyRecordFields(p) {
  if (!p) return;
  if (p.task != null) $("rec-task").value = p.task;
  if (p.repo_id != null) $("rec-repo").value = p.repo_id;
  if (p.episode_time_s != null) $("rec-ep").value = p.episode_time_s;
  if (p.reset_time_s != null) $("rec-reset").value = p.reset_time_s;
  if (p.num_episodes != null && $("rec-num")) $("rec-num").value = p.num_episodes;
  const actionFps = p.action_fps ?? p.fps;
  const videoFps = p.video_fps ?? p.fps ?? actionFps;
  if (actionFps != null && $("rec-action-fps")) $("rec-action-fps").value = actionFps;
  if (videoFps != null && $("rec-video-fps")) $("rec-video-fps").value = videoFps;
  if (p.format != null && $("rec-format")) $("rec-format").value = p.format;
  if (p.root != null && $("rec-root")) $("rec-root").value = p.root;
  if (p.resume != null && $("chk-rec-resume")) $("chk-rec-resume").checked = !!p.resume;
  if (p.video != null && $("chk-rec-video")) $("chk-rec-video").checked = !!p.video;
  if (p.streaming_encoding != null && $("chk-rec-stream-enc")) $("chk-rec-stream-enc").checked = !!p.streaming_encoding;
  if (p.encoder_threads != null && $("rec-enc-threads")) $("rec-enc-threads").value = p.encoder_threads;
  if (p.resume && p.video_id) selectedVideoId = p.video_id;
  else if (p.resume && p.dataset_id) selectedVideoId = p.dataset_id;
  updateResumeTargetUi();
}
function kvPairs(hostId = "roll-kv") {
  const extra = {};
  const host = $(hostId);
  if (!host) return extra;
  host.querySelectorAll(".kv-row").forEach((row) => {
    const key = row.querySelector(".kv-k").value.trim();
    const value = row.querySelector(".kv-v").value;
    if (key) extra[key] = value;
  });
  return extra;
}
function setKvPairs(extra, hostId = "roll-kv", onChange = persistUi) {
  const host = $(hostId);
  if (!host) return;
  host.innerHTML = "";
  const entries = Object.entries(extra || {});
  if (!entries.length) entries.push(["", ""]);
  entries.forEach(([key, value]) => addKvRow(key, value, hostId, onChange));
}
function addKvRow(key = "", value = "", hostId = "roll-kv", onChange = persistUi) {
  const host = $(hostId);
  if (!host) return;
  const row = document.createElement("div");
  row.className = "kv-row";
  const k = document.createElement("input");
  k.className = "kv-k";
  k.type = "text";
  k.placeholder = "key";
  k.value = key;
  const v = document.createElement("input");
  v.className = "kv-v";
  v.type = "text";
  v.placeholder = "value";
  v.value = value;
  const del = document.createElement("button");
  del.type = "button";
  del.className = "ghost kv-del";
  del.textContent = "×";
  k.addEventListener("input", onChange);
  v.addEventListener("input", onChange);
  del.addEventListener("click", () => {
    row.remove();
    if (!host.children.length) addKvRow("", "", hostId, onChange);
    onChange();
  });
  row.append(k, v, del);
  host.appendChild(row);
}
function rolloutFields() {
  return {
    policy_path: $("pol-path").value.trim(),
    task: $("pol-task").value,
    duration_s: Number($("pol-dur").value),
    device: $("pol-dev").value.trim() || "cuda",
    policy_fps: Number($("pol-fps") && $("pol-fps").value) || 15,
    auto_record: autoRecord,
    extra: kvPairs(),
  };
}
function applyRolloutFields(p) {
  if (!p) return;
  if (p.policy_path != null) $("pol-path").value = p.policy_path;
  if (p.task != null) $("pol-task").value = p.task;
  if (p.duration_s != null) $("pol-dur").value = p.duration_s;
  if (p.device != null) $("pol-dev").value = p.device;
  const policyFps = p.policy_fps ?? p.fps;
  if (policyFps != null && $("pol-fps")) $("pol-fps").value = policyFps;
  if (p.extra != null) setKvPairs(p.extra);
}

function serialPresetSpec(role) {
  const select = $(role === "arm" ? "arm-port" : "leader-port");
  const port = select ? select.value : "";
  if (!port) return null;
  const row = lastPorts.find((item) => item.port === port);
  const identity = row && row.identity
    ? { ...row.identity }
    : { kind: "serial", hwid: "", port };
  return { identity, port };
}

function cameraPresetIdentity(camera) {
  if (camera && camera.identity) return { ...camera.identity };
  if (camera && camera.remote) {
    return {
      kind: "remote",
      robot_id: String(camera.robot_id || ""),
      camera_id: String(camera.camera_id || ""),
      object_name: String(camera.object_name || ""),
    };
  }
  return {
    kind: "local",
    index: camera ? camera.index : null,
    name: String(camera && camera.name || ""),
  };
}

function cameraPresetKey(camera) {
  if (camera && camera.device_key) return String(camera.device_key);
  const identity = cameraPresetIdentity(camera);
  if (identity.kind === "remote") {
    return `remote:${identity.robot_id || "robot"}:${identity.camera_id || "camera"}`;
  }
  return `local:${identity.index ?? identity.name ?? "unknown"}`;
}

function hardwareFields() {
  const devices = {};
  const arm = serialPresetSpec("arm");
  const leader = serialPresetSpec("leader");
  if (arm) devices.arm = arm;
  if (leader) devices.leader = leader;
  const cameras = {};
  const rows = (last && Array.isArray(last.cameras) && last.cameras.length)
    ? last.cameras
    : camMenu;
  (rows || []).forEach((camera) => {
    const settings = {
      label: String(camera.label || ""),
      enabled: Boolean(camera.enabled),
      show_main: Boolean(camera.show_main),
      feed_robot: Boolean(camera.feed_robot),
    };
    if (!camera.remote) {
      settings.streaming = Boolean(camera.streaming);
      settings.port = Number(camera.port);
      settings.width = Number(camera.width);
      settings.height = Number(camera.height);
      settings.autofocus = Boolean(camera.autofocus);
      settings.focus = Number(camera.focus || 0);
    }
    cameras[cameraPresetKey(camera)] = {
      identity: cameraPresetIdentity(camera),
      settings,
    };
  });
  return { schema: 1, system: false, devices, cameras };
}

let uiTimer = null;
function persistUi() {
  updateRecordDestinationUi();
  clearTimeout(uiTimer);
  uiTimer = setTimeout(() => {
    api("/api/ui", {
      record: recordFields(),
      rollout: rolloutFields(),
      hold: $("chk-hold").checked,
      auto_record: autoRecord,
      selected_dataset: selectedDatasetId,
      selected_video: selectedVideoId,
      hardware: {
        arm_port: $("arm-port") ? $("arm-port").value : "",
        leader_port: $("leader-port") ? $("leader-port").value : "",
      },
    }, "PUT").catch(() => {});
    updateTaskInfo();
  }, 400);
}

function flag(name, value) {
  const s = String(value ?? "");
  if (s === "") return `--${name}=`;
  if (/[\s"=]/.test(s) || s.includes("{") || s.includes("'")) {
    return `--${name}="${s.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`;
  }
  return `--${name}=${s}`;
}

function portDescription(port) {
  if (!port) return "";
  const row = lastPorts.find((p) => p.port === port);
  return row ? row.description : "";
}

function setHwStatus(id, device) {
  const el = $(id);
  if (!el) return;
  const connected = !!(device && device.connected);
  const role = id.startsWith("arm") ? "arm" : "leader";
  setPortToggle(role, connected);
  el.className = `hw-status${connected ? " on" : ""}`;
  if (!connected) {
    el.textContent = "disconnected";
    return;
  }
  const desc = portDescription(device.port);
  const lines = [`connected · ${device.port || "?"}`];
  if (desc) lines.push(desc);
  if (device.id) lines.push(device.id);
  el.textContent = lines.join("\n");
}

function setPortToggle(role, connected) {
  const buttons = [
    $(`btn-${role}-toggle`),
    $(`btn-hdr-${role}-power`),
  ].filter(Boolean);
  const select = $(`${role}-port`);
  if (!connected) disconnectPending[role] = false;
  for (const button of buttons) {
    button.classList.toggle("on", connected);
    button.classList.toggle("pending-disconnect", disconnectPending[role]);
    button.setAttribute("aria-pressed", String(connected));
    const action = disconnectPending[role]
      ? `Force disconnect ${role}`
      : connected ? `Disconnect ${role}` : `Connect ${role}`;
    button.title = action;
    button.setAttribute("aria-label", action);
    button.disabled = !connected && !(select && select.value);
  }
}

function syncDevicePowerButtons(robot, leader) {
  const states = {
    arm: { device: robot || {}, port: $("arm-port") ? $("arm-port").value : "" },
    leader: { device: leader || {}, port: $("leader-port") ? $("leader-port").value : "" },
  };
  for (const [role, state] of Object.entries(states)) {
    const label = $(`st-${role === "arm" ? "robot" : "leader"}`);
    if (label) {
      label.textContent = state.device.connected
        ? String(state.device.port || "connected")
        : String(state.port || "No device");
    }
    setPortToggle(role, Boolean(state.device.connected));
  }
}

async function togglePortConnection(role) {
  const select = $(`${role}-port`);
  const endpointRole = role === "arm" ? "robot" : "leader";
  const device = role === "arm"
    ? ((last && last.robot) || {})
    : ((last && last.leader) || {});
  if (disconnectPending[role]) {
    return api("/api/hardware/force_disconnect", { role });
  }
  if (device.connected) {
    disconnectPending[role] = true;
    setPortToggle(role, true);
    try {
      return await api(`/api/${endpointRole}/disconnect`);
    } catch (err) {
      disconnectPending[role] = false;
      setPortToggle(role, true);
      throw err;
    }
  }
  const port = select ? select.value : "";
  if (!port) throw new Error(`select a ${role} device before connecting`);
  persistUi();
  return api(`/api/${endpointRole}/connect`, { port });
}

function camerasFlag() {
  const list = ((last && last.cameras) || []).filter((c) => c.enabled && c.feed_robot);
  if (!list.length) return "";
  const inner = list.map((c) => {
    const key = String(c.label || `cam${c.name}`).replace(/\s+/g, "_");
    const src = c.remote
      ? String(c.url)
      : c.streaming
        ? `http://localhost:${c.port}/video`
        : String(c.index ?? c.name);
    const fps = Math.round(Number(c.target_fps ?? c.fps ?? 25));
    return `${key}: {type: opencv, index_or_path: '${src}', width: ${c.width}, height: ${c.height}, fps: ${fps}}`;
  }).join(", ");
  return `{ ${inner} }`;
}

function extraFlags(extra) {
  return Object.entries(extra || {}).map(([key, value]) => flag(String(key).replace(/^--/, ""), value));
}

function updateTaskInfo() {
  const robot = (last && last.robot) || meta.robot || {};
  const leader = (last && last.leader) || meta.leader || {};
  const rec = recordFields();
  const roll = rolloutFields();
  const armPort = $("arm-port") ? $("arm-port").value : (robot.port || "");
  const leadPort = $("leader-port") ? $("leader-port").value : (leader.port || "");
  const actionFps = rec.action_fps || (meta.recording && (meta.recording.action_fps || meta.recording.fps)) || 15;
  const videoFps = rec.video_fps || (meta.recording && (meta.recording.video_fps || meta.recording.fps)) || actionFps;
  const cams = camerasFlag();
  const set = (id, lines) => {
    const el = $(id);
    if (el) el.textContent = lines.filter((line) => line != null).join("\n");
  };
  const robotFlags = [
    flag("robot.type", robot.type || "so101_follower"),
    flag("robot.port", armPort),
    flag("robot.id", robot.id || ""),
    flag("robot.use_degrees", robot.use_degrees != null ? robot.use_degrees : true),
  ];
  if (cams) robotFlags.push(flag("robot.cameras", cams));
  const teleopFlags = [
    flag("teleop.type", leader.type || "so101_leader"),
    flag("teleop.port", leadPort),
    flag("teleop.id", leader.id || ""),
  ];
  const rt = (last && last.runtime) || meta.runtime || {};
  const envLines = [
    `# python ${rt.python || ""}`,
    `# torch ${rt.torch || "missing"}  cuda=${rt.cuda === true}  ${rt.gpu || rt.cuda_built || ""}`,
    `# lerobot ${rt.lerobot_file || rt.lerobot_src || ""}`,
  ];
  set("info-hw", [
    ...envLines,
    "# in-process  equivalent CLI",
    "lerobot-calibrate",
    ...robotFlags,
    "",
    "lerobot-calibrate",
    ...teleopFlags,
  ]);
  set("info-teleop", [
    ...envLines,
    "# in-process  equivalent CLI",
    "lerobot-teleoperate",
    ...robotFlags,
    ...teleopFlags,
    flag("display_data", true),
  ]);
  set("info-record", [
    ...envLines,
    "# in-process  equivalent CLI",
    "lerobot-record",
    ...robotFlags,
    ...teleopFlags,
    flag("display_data", true),
    flag("resume", rec.resume),
    flag("dataset.repo_id", rec.repo_id || ""),
    flag("dataset.root", rec.root || (meta.recording && meta.recording.root) || ""),
    flag("dataset.single_task", rec.task || ""),
    flag("dataset.episode_time_s", rec.episode_time_s),
    flag("dataset.reset_time_s", rec.reset_time_s),
    flag("dataset.num_episodes", rec.num_episodes),
    flag("dataset.action_fps", actionFps),
    flag("dataset.video_fps", videoFps),
    flag("dataset.fps", actionFps),
    flag("dataset.video", rec.video),
    flag("dataset.streaming_encoding", rec.streaming_encoding),
    flag("dataset.encoder_threads", rec.encoder_threads),
  ]);
  set("info-roll", [
    ...envLines,
    "# direct lerobot-rollout CLI",
    "lerobot-rollout",
    flag("strategy.type", autoRecord ? "sentry" : "base"),
    flag("policy.path", roll.policy_path || ""),
    flag("device", roll.device),
    ...robotFlags,
    flag("task", roll.task || ""),
    flag("duration", roll.duration_s),
    flag("fps", roll.policy_fps),
    autoRecord ? flag("dataset.repo_id", rec.repo_id || "") : null,
    autoRecord ? flag("dataset.single_task", roll.task || rec.task || "") : null,
    autoRecord ? flag("dataset.fps", actionFps) : null,
    autoRecord ? flag("dataset.video", rec.video) : null,
    autoRecord ? flag("dataset.streaming_encoding", rec.streaming_encoding) : null,
    autoRecord ? flag("dataset.encoder_threads", rec.encoder_threads) : null,
    ...extraFlags(roll.extra),
  ]);
}
["rec-task", "rec-repo", "rec-ep", "rec-reset", "rec-num", "rec-action-fps", "rec-video-fps", "rec-format", "rec-root", "chk-rec-resume", "chk-rec-video", "chk-rec-stream-enc", "rec-enc-threads", "pol-path", "pol-task", "pol-dur", "pol-fps", "pol-dev", "arm-port", "leader-port"].forEach((id) => {
  const el = $(id);
  if (!el) return;
  el.addEventListener("change", persistUi);
  el.addEventListener("input", persistUi);
});
["arm-port", "leader-port"].forEach((id) => {
  const el = $(id);
  if (!el) return;
  el.addEventListener("change", () => {
    el.dataset.userSelected = "true";
    const role = id === "arm-port" ? "arm" : "leader";
    const device = role === "arm"
      ? ((last && last.robot) || {})
      : ((last && last.leader) || {});
    setPortToggle(role, Boolean(device.connected));
    syncDevicePowerButtons((last && last.robot) || {}, (last && last.leader) || {});
  });
});
if ($("pol-path")) {
  const input = $("pol-path");
  input.addEventListener("input", () => {
    openPolicyPicker();
    renderPolicyPickerMenu();
  });
  input.addEventListener("keydown", (event) => {
    const menu = $("pol-path-menu");
    const rows = menu ? [...menu.querySelectorAll(".policy-picker-option:not(:disabled)")] : [];
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (!menu || menu.classList.contains("hidden")) openPolicyPicker();
      setPolicyPickerActive(policyPickerActive + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (!menu || menu.classList.contains("hidden")) openPolicyPicker();
      setPolicyPickerActive(policyPickerActive <= 0 ? rows.length - 1 : policyPickerActive - 1);
    } else if (event.key === "Enter" && policyPickerActive >= 0 && rows[policyPickerActive]) {
      event.preventDefault();
      rows[policyPickerActive].click();
    } else if (event.key === "Escape") {
      closePolicyPicker();
    } else if (event.key === "Tab") {
      closePolicyPicker();
    }
  });
}
bind("btn-pol-path-menu", togglePolicyPicker);
document.addEventListener("pointerdown", (event) => {
  const picker = event.target && event.target.closest && event.target.closest(".policy-picker");
  if (!picker) closePolicyPicker();
});
window.addEventListener("resize", positionPolicyPickerMenu);
window.addEventListener("scroll", positionPolicyPickerMenu, true);
if ($("chk-rec-resume")) $("chk-rec-resume").addEventListener("change", updateResumeTargetUi);
bind("btn-rec-use-video", () => {
  const id = browsedLocalVideoId();
  if (!id) return;
  selectedVideoId = id;
  updateResumeTargetUi();
  persistUi();
});
bind("btn-rec-clear-video", () => {
  selectedVideoId = "";
  updateResumeTargetUi();
  persistUi();
});

function setSelectedPreset(kind, name) {
  if (name) presetSelections[kind] = name;
  else delete presetSelections[kind];
  saveJsonStorage(PRESET_SELECTION_KEY, presetSelections);
  if (kind === activePresetKind) renderPresetToolbar();
}

async function saveNamedPreset(kind, name, payload) {
  const key = (name || "").trim();
  if (!key) throw new Error("preset name is empty");
  const saved = { ...payload };
  delete saved.system;
  await api(`/api/presets/${kind}/${encodeURIComponent(key)}`, saved, "PUT");
  savedPresets[kind][key] = saved;
  setSelectedPreset(kind, key);
  refreshPresetSelects();
  return saved;
}

async function deleteNamedPreset(kind, name) {
  if (!name) return;
  await api(`/api/presets/${kind}/${encodeURIComponent(name)}`, undefined, "DELETE");
  delete savedPresets[kind][name];
  setSelectedPreset(kind, "");
  refreshPresetSelects();
}

function uniquePresetName(kind, name) {
  const group = savedPresets[kind] || {};
  let copy = `${name} copy`;
  let i = 2;
  while (group[copy]) copy = `${name} copy ${i++}`;
  return copy;
}
async function duplicateNamedPreset(kind, name) {
  const src = (savedPresets[kind] || {})[name];
  if (!src || !name) throw new Error("select a preset to duplicate");
  const copy = uniquePresetName(kind, name);
  const payload = JSON.parse(JSON.stringify(src));
  delete payload.system;
  await saveNamedPreset(kind, copy, payload);
}

function presetPayload(kind) {
  if (kind === "pose") return { ...targets };
  if (kind === "record") return recordFields();
  if (kind === "rollout") return rolloutFields();
  if (kind === "debug") return debugFields();
  if (kind === "hardware") return hardwareFields();
  throw new Error(`unknown preset kind: ${kind}`);
}

function applyPresetPayload(kind, payload) {
  if (!payload) return;
  if (kind === "pose") {
    applyPoseToSliders(payload);
    return;
  }
  if (kind === "record") {
    applyRecordFields(payload);
    return;
  }
  if (kind === "rollout") {
    applyRolloutFields(payload);
    return;
  }
  if (kind === "debug") applyDebugFields(payload);
}

async function loadSelectedPreset() {
  const kind = activePresetKind;
  const name = selectedPresetName(kind);
  const payload = selectedPreset(kind);
  if (!name || !payload) throw new Error("select a preset to load");
  if (presetLoadState.kind === kind) {
    const loadingName = presetLoadState.name;
    if (kind === "hardware" && loadingName === name && !presetLoadState.forceSent) {
      presetLoadState.forceSent = true;
      renderPresetToolbar();
      localLog(`hardware preset "${loadingName}": force load requested`);
      await api("/api/hardware/apply", { name: loadingName, force: true });
    }
    return;
  }

  presetLoadState.kind = kind;
  presetLoadState.name = name;
  presetLoadState.forceSent = false;
  renderPresetToolbar();
  try {
    if (kind === "hardware") {
      if (replayActive || episodeSource) {
        throw new Error("exit replay before loading a hardware preset");
      }
      const result = await api("/api/hardware/apply", { name });
      const devices = payload.devices && typeof payload.devices === "object" ? payload.devices : {};
      for (const role of ["arm", "leader"]) {
        const select = $(`${role}-port`);
        if (!select) continue;
        select.value = String((devices[role] && devices[role].port) || "");
        select.dataset.userSelected = "true";
      }
      persistUi();
      await refreshPorts();
      localLog(
        `hardware preset "${name}": ${result.summary.success} ok, `
        + `${result.summary.skipped} skipped, ${result.summary.failed} failed`,
        result.complete ? "" : "error",
      );
      return;
    }
    applyPresetPayload(kind, payload);
  } finally {
    if (presetLoadState.kind === kind && presetLoadState.name === name) {
      presetLoadState.kind = "";
      presetLoadState.name = "";
      presetLoadState.forceSent = false;
      renderPresetToolbar();
    }
  }
}

function openPresetNamePopover(mode) {
  const popover = $("preset-name-popover");
  const input = $("preset-name-input");
  if (!popover || !input) return;
  const current = selectedPresetName();
  presetNameMode = mode;
  $("preset-name-label").textContent = mode === "rename" ? "Rename preset" : "Save preset as";
  input.value = mode === "rename"
    ? current
    : (current ? `${current} copy` : `${PRESET_LABELS[activePresetKind]} preset`);
  $("preset-name-error").textContent = "";
  popover.classList.remove("hidden");
  input.focus();
  input.select();
}

function closePresetNamePopover() {
  const popover = $("preset-name-popover");
  if (popover) popover.classList.add("hidden");
  const error = $("preset-name-error");
  if (error) error.textContent = "";
}

async function confirmPresetName() {
  const input = $("preset-name-input");
  const error = $("preset-name-error");
  const nextName = (input && input.value || "").trim();
  if (!nextName) {
    if (error) error.textContent = "Enter a preset name.";
    return;
  }
  const kind = activePresetKind;
  const current = selectedPresetName(kind);
  if (nextName !== current && (savedPresets[kind] || {})[nextName]) {
    if (error) error.textContent = "A preset with this name already exists.";
    return;
  }
  try {
    if (presetNameMode === "rename") {
      const saved = await api(
        `/api/presets/${kind}/${encodeURIComponent(current)}/rename`,
        { name: nextName },
        "POST",
      );
      delete savedPresets[kind][current];
      savedPresets[kind][nextName] = saved;
      setSelectedPreset(kind, nextName);
    } else {
      await saveNamedPreset(kind, nextName, presetPayload(kind));
    }
    closePresetNamePopover();
  } catch (err) {
    if (error) error.textContent = err.message || String(err);
  }
}

bind("btn-preset-load", loadSelectedPreset);
bind("btn-preset-save", async () => {
  const name = selectedPresetName();
  if (!name || isSystemPreset()) {
    openPresetNamePopover("save");
    return;
  }
  await saveNamedPreset(activePresetKind, name, presetPayload(activePresetKind));
});
bind("btn-preset-rename", () => openPresetNamePopover("rename"));
bind("btn-preset-dup", () => duplicateNamedPreset(activePresetKind, selectedPresetName()));
bind("btn-preset-del", () => deleteNamedPreset(activePresetKind, selectedPresetName()));
bind("btn-preset-name-confirm", confirmPresetName);
bind("btn-preset-name-cancel", closePresetNamePopover);
if ($("preset-select")) {
  $("preset-select").addEventListener("change", () => {
    setSelectedPreset(activePresetKind, $("preset-select").value);
  });
}
if ($("preset-name-input")) {
  $("preset-name-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      confirmPresetName();
    } else if (event.key === "Escape") {
      event.preventDefault();
      closePresetNamePopover();
    }
  });
}
document.addEventListener("pointerdown", (event) => {
  const popover = $("preset-name-popover");
  if (!popover || popover.classList.contains("hidden")) return;
  if (event.target.closest("#preset-name-popover, #btn-preset-save, #btn-preset-rename")) return;
  closePresetNamePopover();
});

function applyPoseToSliders(pose) {
  Object.assign(targets, pose);
  Object.entries(pose).forEach(([name, value]) => {
    const slider = document.querySelector(`input[data-joint="${name}"]`);
    const val = document.querySelector(`[data-val="${name}"]`);
    if (slider) slider.value = String(value);
    if (val) val.value = fmt(value);
  });
  updateJoints(last.joints || {});
}
bind("btn-apply", () => {
  exitReplayForControl();
  return api("/api/joints", { joints: { ...targets }, duration_s: 2.5 });
});
bind("btn-read-pose", async () => {
  const result = await api("/api/joints/read");
  applyPoseToSliders(result.joints || {});
});
bind("btn-read-leader", async () => {
  const result = await api("/api/joints/read_leader");
  applyPoseToSliders(result.joints || {});
});
$("chk-hold").addEventListener("change", async (e) => {
  try { await api("/api/hold", { enabled: e.target.checked }); persistUi(); }
  catch (err) { toastError(err); }
});

bind("btn-arm-toggle", () => togglePortConnection("arm"));
bind("btn-leader-toggle", () => togglePortConnection("leader"));
bind("btn-hdr-arm-power", () => togglePortConnection("arm"));
bind("btn-hdr-leader-power", () => togglePortConnection("leader"));
bind("btn-rec-next", () => api("/api/record/next"));
bind("btn-hdr-scan", () => runAction("btn-hdr-scan", "scan requested", async () => {
  await api("/api/scan");
  await refreshPorts();
  await refreshLibrary();
}));
bind("btn-hdr-relax", () => {
  exitReplayForControl();
  return runAction("btn-hdr-relax", "relax requested", () => api("/api/joints/preset", { name: "relax", duration_s: 2.5 }));
});
bind("btn-hdr-teleop", () => {
  const mode = (last && last.mode) || "";
  const pending = last && last.task && last.task.pending;
  if (mode === "teleop" || pending === "teleop_start") {
    localLog("teleop is active — use Stop");
    return;
  }
  exitReplayForControl();
  return runAction("btn-hdr-teleop", "teleop requested", () => api("/api/teleop/start", { auto_record: autoRecord, ...captureFields() }));
});
bind("btn-hdr-record", () => {
  const mode = (last && last.mode) || "";
  const pending = last && last.task && last.task.pending;
  if (mode === "record" || pending === "record_start") {
    localLog("record is active — use Stop");
    return;
  }
  exitReplayForControl();
  return runAction("btn-hdr-record", "record requested — writing video session", async () => {
    const fields = recordFields();
    if (fields.resume) {
      const targetExists = selectedVideoId && videosCache.some((row) => row.id === selectedVideoId);
      if (!targetExists) {
        updateResumeTargetUi();
        throw new Error("Continue recording requires an explicit local video target. Browse a video and choose Use browsed video.");
      }
      delete fields.action_fps;
      delete fields.video_fps;
      delete fields.fps;
      delete fields.format;
    } else {
      delete fields.video_id;
      delete fields.dataset_id;
    }
    await api("/api/record/start", fields);
    refreshLibrary();
  });
});
bind("btn-hdr-rollout", () => {
  const mode = (last && last.mode) || "";
  const pending = last && last.task && last.task.pending;
  if (mode === "rollout" || pending === "rollout_start") {
    localLog("rollout is active — use Stop");
    return;
  }
  exitReplayForControl();
  const path = ($("pol-path") && $("pol-path").value.trim()) || "(no policy)";
  return runAction("btn-hdr-rollout", `rollout requested — loading ${path}`, () => {
    const payload = { ...rolloutFields(), auto_record: autoRecord };
    if (autoRecord) {
      const recording = recordFields();
      payload.action_fps = recording.action_fps;
      payload.video_fps = recording.video_fps;
    }
    return api("/api/rollout/start", payload);
  });
});
bind("btn-hdr-capture", () => {
  if (capturing || (last && last.task && last.task.pending === "capture_start")) {
    localLog("capture is active — use Stop");
    return;
  }
  exitReplayForControl();
  return runAction("btn-hdr-capture", "video capture requested", async () => {
    await api("/api/capture/start", captureFields());
    refreshLibrary();
  });
});
bind("btn-hdr-auto", async () => {
  autoRecord = !autoRecord;
  if ($("btn-hdr-auto")) $("btn-hdr-auto").classList.toggle("auto-on", autoRecord);
  localLog(`auto-record ${autoRecord ? "on" : "off"}`);
  await api("/api/auto_record", { enabled: autoRecord });
  persistUi();
});
bind("btn-hdr-stop", () => requestStop());
bind("btn-hdr-resume", () => {
  localLog("resume torque requested");
  return api("/api/resume");
});
bind("btn-hdr-estop", () => {
  localLog("E-STOP requested", "error");
  return api("/api/estop");
});

function captureFields() {
  const rec = recordFields();
  const fields = {
    resume: rec.resume,
    task: rec.task,
    name: rec.repo_id || rec.task || "capture",
    repo_id: rec.repo_id,
    root: rec.root,
    video: rec.video,
    streaming_encoding: rec.streaming_encoding,
    encoder_threads: rec.encoder_threads,
    merge: true,
  };
  if (rec.resume) {
    if (rec.video_id) fields.video_id = rec.video_id;
    if (rec.dataset_id) fields.dataset_id = rec.dataset_id;
  } else {
    fields.action_fps = rec.action_fps;
    fields.video_fps = rec.video_fps;
    fields.fps = rec.action_fps;
    fields.format = rec.format;
  }
  return fields;
}
bind("btn-log-copy", async () => {
  const text = [...document.querySelectorAll("#log li")].map((li) => li.textContent).join("\n");
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
});

const PANEL_KEY = "lerobot-monitor-panels";
function loadPanelState() {
  try { return JSON.parse(localStorage.getItem(PANEL_KEY) || "{}"); } catch { return {}; }
}
function initPanels() {
  const state = loadPanelState();
  document.querySelectorAll("[data-panel]").forEach((panel) => {
    const btn = [...panel.children].find((child) => child.classList.contains("panel-toggle"));
    if (!btn) return;
    const id = panel.dataset.panel;
    const collapsed = !!state[id];
    panel.classList.toggle("collapsed", collapsed);
    btn.setAttribute("aria-expanded", String(!collapsed));
    btn.addEventListener("click", () => {
      const nowCollapsed = panel.classList.toggle("collapsed");
      btn.setAttribute("aria-expanded", String(!nowCollapsed));
      const next = loadPanelState();
      next[id] = nowCollapsed;
      localStorage.setItem(PANEL_KEY, JSON.stringify(next));
      if (id === "library") applyLayout(loadLayout());
    });
  });
}
initPanels();

function initTabList(tablistId, storageKey, fallback, onSelect) {
  const tablist = $(tablistId);
  if (!tablist) return;
  const tabs = [...tablist.querySelectorAll("[data-tab]")];
  const panels = tabs.map((tab) => $(tab.getAttribute("aria-controls"))).filter(Boolean);
  if (!tabs.length) return;

  let saved = "";
  try { saved = localStorage.getItem(storageKey) || ""; } catch { /* ignore */ }
  const initial = tabs.some((tab) => tab.dataset.tab === saved) ? saved : fallback;

  function selectTab(value, focus = false) {
    const selected = tabs.some((tab) => tab.dataset.tab === value) ? value : fallback;
    tabs.forEach((tab) => {
      const active = tab.dataset.tab === selected;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      if (active && focus) tab.focus();
    });
    panels.forEach((panel) => {
      panel.hidden = panel.dataset.tabPanel !== selected;
    });
    try { localStorage.setItem(storageKey, selected); } catch { /* ignore */ }
    if (onSelect) onSelect(selected);
  }

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => selectTab(tab.dataset.tab));
  });
  tablist.addEventListener("keydown", (event) => {
    const current = tabs.indexOf(event.target);
    if (current < 0) return;
    let next = current;
    if (event.key === "ArrowLeft") next = (current - 1 + tabs.length) % tabs.length;
    else if (event.key === "ArrowRight") next = (current + 1) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    selectTab(tabs[next].dataset.tab, true);
  });
  selectTab(initial);
}

if ($("episodes") && $("episodes").classList.contains("hidden")) {
  document.querySelector("main")?.classList.add("episodes-closed");
}

const LAYOUT_KEY = "lerobot-monitor-layout";
const LAYOUT_DEFAULT = { sideW: 360, bottomH: 320, logW: 480, libW: 320, epW: 280 };

function loadLayout() {
  try { return { ...LAYOUT_DEFAULT, ...JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}") }; }
  catch { return { ...LAYOUT_DEFAULT }; }
}
function applyLayout(layout) {
  const root = document.querySelector("main");
  const bottom = $("bottom");
  if (!root) return;
  const libCollapsed = $("library") && $("library").classList.contains("collapsed");
  root.style.setProperty("--lib-w", `${libCollapsed ? 44 : layout.libW}px`);
  root.style.setProperty("--ep-w", `${layout.epW}px`);
  root.style.setProperty("--side-w", `${layout.sideW}px`);
  root.style.setProperty("--bottom-h", `${layout.bottomH}px`);
  root.style.setProperty("--log-w", `${layout.logW}px`);
  if (bottom) bottom.style.setProperty("--log-w", `${layout.logW}px`);
}
function saveLayout(layout) {
  localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
}
function clamp(value, lo, hi) {
  return Math.round(Math.min(Math.max(value, lo), hi));
}
function initSplitters() {
  const layout = loadLayout();
  applyLayout(layout);
  const main = document.querySelector("main");
  const bottom = $("bottom");

  function drag(el, onMove) {
    if (!el) return;
    el.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      el.classList.add("dragging");
      el.setPointerCapture(ev.pointerId);
      const col = el.classList.contains("col");
      document.body.classList.add(col ? "dragging-col" : "dragging-row");
      const move = (e) => onMove(e);
      const up = () => {
        el.classList.remove("dragging");
        document.body.classList.remove("dragging-col", "dragging-row");
        el.removeEventListener("pointermove", move);
        el.removeEventListener("pointerup", up);
        saveLayout(layout);
      };
      el.addEventListener("pointermove", move);
      el.addEventListener("pointerup", up);
    });
  }

  drag($("split-lib"), (e) => {
    const box = main.getBoundingClientRect();
    layout.libW = clamp(e.clientX - box.left, 160, Math.max(160, box.width - layout.epW - layout.sideW - 32));
    applyLayout(layout);
  });
  drag($("split-ep"), (e) => {
    const box = main.getBoundingClientRect();
    const libCollapsed = $("library") && $("library").classList.contains("collapsed");
    const left = box.left + (libCollapsed ? 44 : layout.libW) + 8;
    layout.epW = clamp(e.clientX - left, 160, Math.max(160, box.width - layout.sideW - 360));
    applyLayout(layout);
  });
  drag($("split-v"), (e) => {
    const box = main.getBoundingClientRect();
    layout.sideW = clamp(box.right - e.clientX, 200, Math.max(200, box.width - layout.epW - 200));
    applyLayout(layout);
  });
  drag($("split-h"), (e) => {
    const box = main.getBoundingClientRect();
    layout.bottomH = clamp(box.bottom - e.clientY, 80, box.height - 80);
    applyLayout(layout);
  });
  drag($("split-log"), (e) => {
    const box = bottom.getBoundingClientRect();
    layout.logW = clamp(box.right - e.clientX, 160, box.width - 120);
    applyLayout(layout);
  });
}
initSplitters();

initTabList("library-tabs", "lerobot-monitor-library-tab", "videos", selectLibrarySearchKind);
initTabList("side-tabs", "lerobot-monitor-side-tab", "joints", (kind) => {
  selectPresetKind(PRESET_KIND_BY_TAB[kind] || "pose");
});
document.querySelectorAll(".panel.side-tab-panel").forEach((panel) => {
  let timer = null;
  panel.addEventListener("scroll", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      presetScroll[panel.dataset.tabPanel] = Math.round(panel.scrollTop);
      saveJsonStorage(PRESET_SCROLL_KEY, presetScroll);
    }, 100);
  });
});

if ($("roll-kv") && !$("roll-kv").children.length) addKvRow();
if ($("btn-kv-add")) bind("btn-kv-add", () => addKvRow());

const LIBRARY_EDIT_ICON = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4l10-10-4-4L4 16z"/><path d="M13.5 6.5l4 4"/></svg>`;

function librarySourceId(kind, row) {
  return kind === "video" ? String(row.id || "") : String(row.repo_id || row.id || "");
}

async function saveLibraryOverride(kind, sourceId, payload) {
  const saved = await api("/api/library", { kind, id: sourceId, ...payload }, "PUT");
  const cache = kind === "video" ? videosCache : datasetsCache;
  const row = cache.find((item) => librarySourceId(kind, item) === sourceId);
  if (row) Object.assign(row, saved);
  if (episodeSource && episodeSource.kind === kind && episodeSource.id === sourceId) {
    Object.assign(episodeSource, saved);
  }
  renderVideos();
  renderDatasets();
  renderEpisodes();
  return saved;
}

async function saveSnapshotNote(snapshotId, note) {
  const saved = await saveSnapshotFields(snapshotId, { note });
  if (snapshotActive && activeSnapshot && activeSnapshot.id === snapshotId && replayActive) {
    renderSnapshotHeader();
  }
  return saved;
}

function appendLibraryNote(li, kind, sourceId, row) {
  const noteButton = document.createElement("button");
  noteButton.type = "button";
  noteButton.className = `lib-note-button${row.note ? "" : " empty"}`;
  noteButton.textContent = row.note || "Add note";
  noteButton.title = row.note ? `Note: ${row.note}` : "Add note";
  noteButton.setAttribute("aria-label", row.note ? `Edit note: ${row.note}` : "Add note");
  const editor = document.createElement("div");
  editor.className = "lib-note-editor hidden";
  const input = document.createElement("input");
  input.type = "text";
  input.value = row.note || "";
  input.placeholder = "note";
  input.setAttribute("aria-label", "Library note");
  const actions = document.createElement("span");
  actions.className = "lib-note-actions";
  const save = document.createElement("button");
  save.type = "button";
  save.className = "ghost icon-btn";
  save.title = "Save note";
  save.setAttribute("aria-label", "Save note");
  save.innerHTML = LIBRARY_EDIT_ICON;
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "ghost";
  cancel.textContent = "Cancel";
  actions.append(save, cancel);
  editor.append(input, actions);
  noteButton.addEventListener("click", () => {
    noteButton.classList.add("hidden");
    editor.classList.remove("hidden");
    input.focus();
    input.select();
  });
  const closeEditor = () => {
    editor.classList.add("hidden");
    noteButton.classList.remove("hidden");
  };
  const commit = async () => {
    save.disabled = true;
    cancel.disabled = true;
    input.disabled = true;
    try {
      if (kind === "snapshot") await saveSnapshotNote(sourceId, input.value.trim());
      else if (kind === "model") {
        await api(`/api/models/${encodeURIComponent(sourceId)}`, { note: input.value.trim() }, "PUT");
        await refreshLibrarySection("models");
      }
      else await saveLibraryOverride(kind, sourceId, { note: input.value.trim() });
    } catch (err) {
      toastError(err);
      closeEditor();
    } finally {
      save.disabled = false;
      cancel.disabled = false;
      input.disabled = false;
    }
  };
  save.addEventListener("click", commit);
  cancel.addEventListener("click", closeEditor);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      commit();
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeEditor();
    }
  });
  li.append(noteButton, editor);
}

function libraryTitle(kind, row) {
  if (kind === "dataset") {
    return String(row.title || row.repo_id || row.id || "");
  }
  return String(row.name || row.title || row.id || "");
}

function filterLibraryRows(kind, rows) {
  const query = String(librarySearch[kind] || "").trim().toLowerCase();
  if (!query) return rows;
  return rows.filter((row) => {
    const haystack = `${libraryTitle(kind, row)}\n${row.note || ""}`.toLowerCase();
    return haystack.includes(query);
  });
}

function libraryFilterMessage(kind) {
  return `No ${kind} title or note matches this search`;
}

function renderVideos() {
  const ol = $("vid-list");
  if (!ol) return;
  ol.innerHTML = "";
  if (!videosCache.length) {
    const state = libraryState.videos;
    const message = state.loading ? "Loading local videos…" : state.error ? `Could not load videos: ${state.error}` : "No local video found";
    ol.innerHTML = `<li class="library-message${state.error ? " error" : ""}">${message}</li>`;
    return;
  }
  const rows = filterLibraryRows("videos", videosCache);
  if (!rows.length) {
    ol.innerHTML = `<li class="library-message">${libraryFilterMessage("video")}</li>`;
    return;
  }
  rows.forEach((vid) => {
    const li = document.createElement("li");
    if (episodeSource && episodeSource.kind === "video" && episodeSource.id === vid.id) li.className = "sel";
    const n = (vid.episodes || []).length;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "lib-row-button";
    button.textContent = `${vid.name || vid.id}  ·  ${n} ep`;
    button.setAttribute("aria-pressed", String(li.classList.contains("sel")));
    li.title = vid.path || vid.id;
    button.addEventListener("click", () => selectVideo(vid.id));
    li.appendChild(button);
    appendLibraryNote(li, "video", String(vid.id), vid);
    ol.appendChild(li);
  });
}

function renderDatasets() {
  const ol = $("ds-list");
  if (!ol) return;
  ol.innerHTML = "";
  if (!datasetsCache.length) {
    const state = libraryState.datasets;
    const message = state.loading ? "Loading datasets…" : state.error ? `Could not load datasets: ${state.error}` : "No dataset found";
    ol.innerHTML = `<li class="library-message${state.error ? " error" : ""}">${message}</li>`;
    return;
  }
  const rows = filterLibraryRows("datasets", datasetsCache);
  if (!rows.length) {
    ol.innerHTML = `<li class="library-message">${libraryFilterMessage("dataset")}</li>`;
    return;
  }
  rows.forEach((ds) => {
    const li = document.createElement("li");
    const id = ds.repo_id || ds.id;
    if (episodeSource && episodeSource.kind === "dataset" && episodeSource.id === id) li.className = "sel";
    const n = ds.episodes != null ? `${ds.episodes} ep` : ds.source || "hf";
    const play = ds.playable ? "" : "  · no video";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "lib-row-button";
    button.textContent = `${id}  ·  ${n}${play}`;
    button.setAttribute("aria-pressed", String(li.classList.contains("sel")));
    li.title = ds.path || ds.repo_id || "";
    button.addEventListener("click", () => selectHfDataset(ds));
    li.appendChild(button);
    appendLibraryNote(li, "dataset", id, ds);
    ol.appendChild(li);
  });
}

const SNAPSHOT_ICONS = {
  edit: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4l10-10-4-4L4 16z"/><path d="M13.5 6.5l4 4"/></svg>`,
  duplicate: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11"/><path d="M5 15V4h11"/></svg>`,
  delete: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 13h10l1-13"/><path d="M9 7V4h6v3"/></svg>`,
};

function snapshotSourceLabel(snapshot) {
  if (snapshot.origin === "replay" && snapshot.source) {
    const parts = [snapshot.source.kind, snapshot.source.id].filter(Boolean);
    if (snapshot.source.episode != null) parts.push(`ep ${snapshot.source.episode}`);
    if (snapshot.source.elapsed_s != null) parts.push(`${Number(snapshot.source.elapsed_s).toFixed(2)}s`);
    return parts.join(" · ") || "replay";
  }
  return "hardware";
}

function snapshotCameraCount(snapshot) {
  const count = (snapshot.cameras || []).length;
  return count ? `${count} cam` : "no image";
}

function makeSnapshotIconButton(className, label, icon) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `ghost icon-btn ${className}`;
  button.title = label;
  button.setAttribute("aria-label", label);
  button.innerHTML = icon;
  return button;
}

function renderSnapshots() {
  const ol = $("snap-list");
  if (!ol) return;
  ol.innerHTML = "";
  if (!snapshotsCache.length) {
    const state = libraryState.snapshots;
    const message = state.loading
      ? "Loading snapshots…"
      : state.error ? `Could not load snapshots: ${state.error}` : "No snapshot yet";
    ol.innerHTML = `<li class="library-message${state.error ? " error" : ""}">${message}</li>`;
    return;
  }
  const rows = filterLibraryRows("snapshots", snapshotsCache);
  if (!rows.length) {
    ol.innerHTML = `<li class="library-message">${libraryFilterMessage("snapshot")}</li>`;
    return;
  }
  rows.forEach((snapshot) => {
    const li = document.createElement("li");
    if (snapshotActive && activeSnapshot && activeSnapshot.id === snapshot.id) li.className = "sel";
    const head = document.createElement("div");
    head.className = "snap-head";
    const open = document.createElement("button");
    open.type = "button";
    open.className = "lib-row-button";
    open.textContent = `${snapshot.name || snapshot.id}  ·  ${snapshotSourceLabel(snapshot)}  ·  ${snapshotCameraCount(snapshot)}`;
    open.setAttribute("aria-pressed", String(li.classList.contains("sel")));
    li.title = snapshot.description || snapshot.id;
    open.addEventListener("click", () => openSnapshot(snapshot.id));
    const tools = document.createElement("span");
    tools.className = "snap-tools";
    const edit = makeSnapshotIconButton("snap-edit", `Edit snapshot ${snapshot.id}`, SNAPSHOT_ICONS.edit);
    edit.addEventListener("click", (event) => {
      event.stopPropagation();
      openSnapshot(snapshot.id).then(() => openSnapshotEditor()).catch(toastError);
    });
    const duplicate = makeSnapshotIconButton("snap-dup", `Duplicate snapshot ${snapshot.id}`, SNAPSHOT_ICONS.duplicate);
    duplicate.addEventListener("click", (event) => {
      event.stopPropagation();
      duplicateSnapshot(snapshot.id);
    });
    const remove = makeSnapshotIconButton("snap-del", `Delete snapshot ${snapshot.id}`, SNAPSHOT_ICONS.delete);
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteSnapshot(snapshot.id);
    });
    tools.append(edit, duplicate, remove);
    head.append(open, tools);
    li.appendChild(head);
    appendLibraryNote(li, "snapshot", snapshot.id, snapshot);
    ol.appendChild(li);
  });
}

async function saveSnapshotFields(snapshotId, payload) {
  const saved = await api(`/api/snapshots/${encodeURIComponent(snapshotId)}`, payload, "PUT");
  const index = snapshotsCache.findIndex((row) => row.id === saved.id);
  if (index >= 0) snapshotsCache[index] = saved;
  if (activeSnapshot && activeSnapshot.id === saved.id) {
    activeSnapshot = saved;
    if (replayActive) renderSnapshotHeader();
  }
  renderSnapshots();
  return saved;
}

async function duplicateSnapshot(snapshotId) {
  const created = await api(`/api/snapshots/${encodeURIComponent(snapshotId)}/duplicate`);
  await refreshLibrarySection("snapshots");
  localLog(`snapshot duplicated: ${created.id}`);
}

async function deleteSnapshot(snapshotId) {
  if (!window.confirm(`Delete snapshot ${snapshotId}?`)) return;
  await api(`/api/snapshots/${encodeURIComponent(snapshotId)}`, undefined, "DELETE");
  if (snapshotActive && activeSnapshot && activeSnapshot.id === snapshotId) closeEpisodeSelection();
  await refreshLibrarySection("snapshots");
  localLog(`snapshot deleted: ${snapshotId}`);
}

function renderSnapshotHeader() {
  const title = $("replay-title");
  if (title) {
    const snapshot = activeSnapshot || {};
    const name = snapshot.name || snapshot.id || "snapshot";
    title.textContent = `${name} · ${snapshotSourceLabel(snapshot)}`;
    title.title = snapshot.description || name;
  }
}

function openSnapshotEditor() {
  const host = $("dbg-snap-editor");
  const snapshot = activeSnapshot;
  if (!host || !snapshot) return;
  host.classList.remove("hidden");
  host.innerHTML = "";
  const fields = {};
  [["name", "Name"], ["task", "Task"]].forEach(([key, label]) => {
    const row = document.createElement("label");
    row.textContent = label;
    const input = document.createElement("input");
    input.type = "text";
    input.value = snapshot[key] || "";
    input.setAttribute("aria-label", `Snapshot ${key}`);
    fields[key] = input;
    row.appendChild(input);
    host.appendChild(row);
  });
  [["note", "Note"], ["description", "Description"]].forEach(([key, label]) => {
    const row = document.createElement("label");
    row.textContent = label;
    const input = document.createElement("textarea");
    input.rows = key === "note" ? 2 : 3;
    input.value = snapshot[key] || "";
    input.setAttribute("aria-label", `Snapshot ${key}`);
    fields[key] = input;
    row.appendChild(input);
    host.appendChild(row);
  });
  const actions = document.createElement("div");
  actions.className = "row-actions";
  const save = document.createElement("button");
  save.type = "button";
  save.textContent = "Save";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "ghost";
  cancel.textContent = "Close";
  actions.append(save, cancel);
  host.appendChild(actions);
  const close = () => {
    host.classList.add("hidden");
    host.innerHTML = "";
  };
  cancel.addEventListener("click", close);
  save.addEventListener("click", async () => {
    save.disabled = true;
    cancel.disabled = true;
    try {
      const payload = {};
      Object.entries(fields).forEach(([key, input]) => { payload[key] = input.value.trim(); });
      await saveSnapshotFields(snapshot.id, payload);
      close();
    } catch (err) {
      toastError(err);
    } finally {
      save.disabled = false;
      cancel.disabled = false;
    }
  });
  fields.name.focus();
}

function snapshotCameraUrl(snapshotId, key) {
  return `${BASE}/api/snapshots/${encodeURIComponent(snapshotId)}/camera/${encodeURIComponent(key)}`;
}

function renderSnapshotCams(snapshot) {
  const host = $("viz-cams");
  if (!host) return;
  host.innerHTML = "";
  const cameras = snapshot.cameras || [];
  host.classList.toggle("multi", cameras.length > 1);
  host.classList.toggle("snapshot-view", true);
  if (!cameras.length) {
    renderReplayMessage("This snapshot has no camera image.");
    return;
  }
  cameras.forEach((camera) => {
    const wrap = document.createElement("div");
    wrap.className = "viz-cam snapshot-cam";
    const label = document.createElement("span");
    const size = camera.width && camera.height ? ` · ${camera.width}×${camera.height}` : "";
    label.textContent = `${camera.key}${size}`;
    const img = document.createElement("img");
    img.alt = `snapshot ${camera.key}`;
    img.addEventListener("load", () => {
      syncSnapshotButton();
      renderDebugPanel();
    });
    img.src = snapshotCameraUrl(snapshot.id, camera.key);
    wrap.append(label, img);
    host.appendChild(wrap);
  });
  setReplayStatus("Snapshot");
}

function snapshotJointHistory(joints, duration, includeFinal) {
  const names = Object.keys(joints || {});
  const times = includeFinal && duration > 0 ? [0, duration] : [0];
  const series = {};
  names.forEach((name) => {
    const value = Number(joints[name]);
    if (!Number.isFinite(value)) return;
    series[`obs.${name}`] = times.map(() => value);
  });
  return { t: times, series };
}

function renderSnapshotCharts(snapshot) {
  const overlay = chunkOverlayPoints();
  const duration = overlay.length ? overlay[overlay.length - 1].x : 0;
  if (duration > 0) vizState.duration = duration;
  const history = snapshotJointHistory(snapshot.joints || {}, duration, duration > 0);
  const hasState = Object.keys(history.series).length > 0;
  fillReplayChart(stateChart, hasState ? history.t : [], hasState ? history.series : {}, "obs.", [], 0);
  fillReplayChart(actionChart, [], {}, "act.", overlay, 0);
}

async function openSnapshot(snapshotId) {
  const snapshot = await fetchJson(`/api/snapshots/${encodeURIComponent(snapshotId)}`);
  if (replayActive) leaveReplay();
  previewRequestGeneration += 1;
  activeSnapshot = snapshot;
  snapshotActive = true;
  vizState.kind = "snapshot";
  vizState.id = snapshot.id;
  vizState.episode = 0;
  vizState.episodes = 1;
  vizState.duration = 0;
  vizState.elapsed = 0;
  vizState.times = [0];
  vizState.arm = { times: [], track: {} };
  vizState.previewMeta = "snapshot";
  vizState.previewReady = true;
  vizState.previewGeneration = previewRequestGeneration;
  vizState.snapshot = snapshot;
  clearVizChunk();
  // A snapshot has no episode list, so keep the episode panel out of the way.
  enterReplay({ showEpisodes: false });
  const stage = $("replay");
  if (stage) stage.classList.add("snapshot-mode");
  const seek = $("replay-seek");
  if (seek) {
    seek.disabled = true;
    seek.value = "0";
  }
  if ($("viz-t")) $("viz-t").value = "snapshot";
  renderSnapshotHeader();
  renderSnapshotCams(snapshot);
  renderSnapshotCharts(snapshot);
  renderSnapshots();
  renderDebugPanel();
  localLog(`snapshot opened: ${snapshot.id}`);
  return snapshot;
}

const SNAPSHOT_JPEG_QUALITY = 0.85;
const SNAPSHOT_MAX_CAMERAS = 12;

function mediaFrameDataUrl(media) {
  if (!media) return "";
  const width = Number(media.naturalWidth || media.videoWidth || 0);
  const height = Number(media.naturalHeight || media.videoHeight || 0);
  if (!width || !height) return "";
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  if (!context) return "";
  try {
    context.drawImage(media, 0, 0, width, height);
    return canvas.toDataURL("image/jpeg", SNAPSHOT_JPEG_QUALITY);
  } catch {
    return "";
  }
}

function cameraKeyFromLabel(label) {
  return String(label || "").split("·")[0].trim();
}

function replayCameraKeys() {
  return [...document.querySelectorAll("#viz-cams .viz-cam span")]
    .map((label) => cameraKeyFromLabel(label.textContent))
    .filter(Boolean);
}

function stageCameraFrames() {
  const frames = [];
  document.querySelectorAll("#viz-cams .viz-cam").forEach((wrap, index) => {
    const media = wrap.querySelector("video") || wrap.querySelector("img");
    const jpeg = mediaFrameDataUrl(media);
    if (!jpeg) return;
    const label = wrap.querySelector("span");
    frames.push({
      key: cameraKeyFromLabel(label && label.textContent) || `cam${index + 1}`,
      jpeg_base64: jpeg,
    });
  });
  return frames;
}

function hardwareCameraFrames() {
  const frames = [];
  ((last && last.cameras) || []).forEach((cam) => {
    if (!cam.enabled || !cam.show_main) return;
    const card = camCards[String(cam.name)];
    const jpeg = card ? mediaFrameDataUrl(card.querySelector("img")) : "";
    if (!jpeg) return;
    frames.push({ key: String(cam.label || cam.name), jpeg_base64: jpeg });
  });
  return frames;
}

function cameraFrameAvailable() {
  const media = [...document.querySelectorAll("#viz-cams .viz-cam")]
    .map((wrap) => wrap.querySelector("video") || wrap.querySelector("img"));
  return media.some((item) => Number(item && (item.naturalWidth || item.videoWidth)) > 0);
}

function hardwareCameraAvailable() {
  return Object.values(camCards).some((card) => {
    const img = card && card.querySelector("img");
    return Number(img && img.naturalWidth) > 0;
  });
}

function snapshotCaptureAvailable() {
  if (snapshotActive && activeSnapshot) {
    return Object.keys(activeSnapshot.joints || {}).length > 0 || cameraFrameAvailable();
  }
  if (replayActive && vizState.previewReady) {
    return !!sampleArmJoints(vizState.elapsed) || cameraFrameAvailable();
  }
  if (last && last.joints && Object.keys(last.joints).length) return true;
  return hardwareCameraAvailable();
}

function syncSnapshotButton() {
  const button = $("btn-hdr-snapshot");
  if (!button) return;
  const available = snapshotCaptureAvailable();
  button.disabled = snapshotCaptureBusy || !available;
  button.classList.toggle("pending", snapshotCaptureBusy);
  if (snapshotCaptureBusy) {
    button.title = "Saving snapshot…";
    return;
  }
  const origin = snapshotActive ? "snapshot" : replayActive ? "replay" : "hardware";
  button.title = available
    ? `Save the current joints and camera frames as a snapshot (${origin})`
    : "No joint state or camera frame available to snapshot";
}

function snapshotCapturePlan() {
  if (snapshotActive && activeSnapshot) {
    const joints = { ...(activeSnapshot.joints || {}) };
    const frames = stageCameraFrames();
    if (!Object.keys(joints).length && !frames.length) return null;
    return {
      origin: "replay",
      source: { kind: "snapshot", id: activeSnapshot.id },
      task: activeSnapshot.task || "",
      joints,
      frames,
      name: `snapshot-${activeSnapshot.id}`,
    };
  }
  if (replayActive && vizState.previewReady && vizState.kind) {
    const elapsed = Math.max(0, Number(vizState.elapsed) || 0);
    const joints = sampleArmJoints(elapsed) || {};
    const frames = stageCameraFrames();
    if (!Object.keys(joints).length && !frames.length) return null;
    return {
      origin: "replay",
      source: {
        kind: vizState.kind,
        id: vizState.id,
        episode: vizState.episode,
        elapsed_s: Number(elapsed.toFixed(3)),
      },
      task: vizState.task || "",
      joints,
      frames,
      name: `replay-${vizState.kind}-ep${vizState.episode}-${elapsed.toFixed(2)}s`,
    };
  }
  const joints = { ...((last && last.joints) || {}) };
  const frames = hardwareCameraFrames();
  if (!Object.keys(joints).length && !frames.length) return null;
  return { origin: "hardware", source: null, task: "", joints, frames, name: "hardware" };
}

async function captureSnapshot() {
  if (snapshotCaptureBusy) return;
  const plan = snapshotCapturePlan();
  if (!plan) throw new Error("no joint state or camera frame to snapshot");
  snapshotCaptureBusy = true;
  syncSnapshotButton();
  try {
    const created = await api("/api/snapshots", {
      name: plan.name,
      task: plan.task,
      origin: plan.origin,
      source: plan.source,
      joints: plan.joints,
      cameras: plan.frames.slice(0, SNAPSHOT_MAX_CAMERAS),
    });
    await refreshLibrarySection("snapshots");
    localLog(`snapshot saved: ${created.id} · ${Object.keys(plan.joints).length} joints · ${plan.frames.length} cam`);
  } finally {
    snapshotCaptureBusy = false;
    syncSnapshotButton();
  }
}

function debugSourceInfo() {
  if (snapshotActive && activeSnapshot) {
    return {
      label: `snapshot ${activeSnapshot.id}`,
      source: { kind: "snapshot", id: activeSnapshot.id },
      task: activeSnapshot.task || "",
      joints: { ...(activeSnapshot.joints || {}) },
      cameras: (activeSnapshot.cameras || []).map((camera) => camera.key),
      overlayStart: 0,
    };
  }
  if (replayActive && vizState.previewReady && vizState.kind) {
    const elapsed = Math.max(0, Number(vizState.elapsed) || 0);
    const joints = sampleArmJoints(elapsed);
    if (!joints) return null;
    return {
      label: `${vizState.kind} ${vizState.id} · ep ${vizState.episode} · ${elapsed.toFixed(2)}s`,
      source: {
        kind: vizState.kind,
        id: vizState.id,
        episode: vizState.episode,
        elapsed_s: Number(elapsed.toFixed(3)),
      },
      task: vizState.task || "",
      joints,
      cameras: replayCameraKeys(),
      overlayStart: elapsed,
    };
  }
  return null;
}

function debugFields() {
  return {
    policy_path: ($("dbg-path") ? $("dbg-path").value : "").trim(),
    task: $("dbg-task") ? $("dbg-task").value : "",
    device: ($("dbg-dev") ? $("dbg-dev").value : "").trim() || "cuda",
    extra: kvPairs("dbg-kv"),
    chunk_size: Number($("dbg-chunk") ? $("dbg-chunk").value : 0) || 16,
    fps: Number($("dbg-fps") ? $("dbg-fps").value : 0) || 30,
    camera_map: debugCameraMap(),
  };
}

function applyDebugFields(preset) {
  if (!preset) return;
  if (preset.policy_path != null && $("dbg-path")) $("dbg-path").value = preset.policy_path;
  if (preset.task != null && $("dbg-task")) $("dbg-task").value = preset.task;
  if (preset.device != null && $("dbg-dev")) $("dbg-dev").value = preset.device;
  if (preset.chunk_size != null && $("dbg-chunk")) $("dbg-chunk").value = preset.chunk_size;
  if (preset.fps != null && $("dbg-fps")) $("dbg-fps").value = preset.fps;
  if (preset.extra != null) setKvPairs(preset.extra, "dbg-kv", () => {});
  if (preset.camera_map != null) applyDebugCameraMap(preset.camera_map);
  const select = $("dbg-policy");
  if (select && preset.policy_path) select.value = preset.policy_path;
  renderDebugPanel();
}

function debugCameraRows() {
  const host = $("dbg-cam-map");
  if (!host) return [];
  return [...host.querySelectorAll(".kv-row")].map((row) => ({
    row,
    enabled: Boolean(row.querySelector(".cam-on") && row.querySelector(".cam-on").checked),
    key: row.querySelector(".kv-k").value.trim(),
    suffix: row.querySelector(".kv-v").value.trim(),
  }));
}

function debugCameraMap() {
  const map = {};
  debugCameraRows().forEach((entry) => {
    if (!entry.enabled || !entry.key) return;
    map[entry.key] = entry.suffix || `observation.images.${entry.key}`;
  });
  return map;
}

function addDebugCameraRow(key = "", suffix = "", enabled = true) {
  const host = $("dbg-cam-map");
  if (!host) return null;
  const row = document.createElement("div");
  row.className = "kv-row cam-row";
  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.className = "cam-on";
  toggle.checked = enabled;
  toggle.title = enabled ? "Camera is fed to the model" : "Camera is not fed to the model";
  toggle.setAttribute("aria-label", "Use camera in inference");
  const k = document.createElement("input");
  k.className = "kv-k";
  k.type = "text";
  k.placeholder = "camera key";
  k.value = key;
  const v = document.createElement("input");
  v.className = "kv-v";
  v.type = "text";
  v.placeholder = "observation.images.<key>";
  v.value = suffix || (key ? `observation.images.${key}` : "");
  const del = document.createElement("button");
  del.type = "button";
  del.className = "ghost kv-del";
  del.textContent = "×";
  const refresh = () => {
    toggle.title = toggle.checked ? "Camera is fed to the model" : "Camera is not fed to the model";
    renderDebugPanel();
  };
  toggle.addEventListener("change", refresh);
  k.addEventListener("input", refresh);
  v.addEventListener("input", refresh);
  del.addEventListener("click", () => {
    row.remove();
    if (!host.children.length) addDebugCameraRow("", "", true);
    refresh();
  });
  row.append(toggle, k, v, del);
  host.appendChild(row);
  return row;
}

function setDebugCameraRows(entries) {
  const host = $("dbg-cam-map");
  if (!host) return;
  host.innerHTML = "";
  const rows = (entries || []).filter((entry) => entry && entry.key);
  rows.forEach((entry) => addDebugCameraRow(entry.key, entry.suffix || "", entry.enabled !== false));
  if (!rows.length) addDebugCameraRow("", "", true);
}

function applyDebugCameraMap(map) {
  const saved = map || {};
  const source = debugSourceInfo();
  const keys = [...new Set([...Object.keys(saved), ...((source && source.cameras) || [])])];
  setDebugCameraRows(keys.map((key) => ({
    key,
    suffix: saved[key] || `observation.images.${key}`,
    enabled: Object.prototype.hasOwnProperty.call(saved, key),
  })));
}

function ensureDebugCameraRows(keys) {
  const host = $("dbg-cam-map");
  if (!host) return;
  const known = new Set(
    [...host.querySelectorAll(".kv-k")].map((input) => input.value.trim()).filter(Boolean),
  );
  const rows = [...host.querySelectorAll(".kv-row")];
  const placeholder = rows.length === 1 && !rows[0].querySelector(".kv-k").value.trim() ? rows[0] : null;
  (keys || []).forEach((key) => {
    if (!key || known.has(key)) return;
    known.add(key);
    if (placeholder && !placeholder.querySelector(".kv-k").value.trim()) {
      placeholder.querySelector(".kv-k").value = key;
      placeholder.querySelector(".kv-v").value = `observation.images.${key}`;
      if (placeholder.querySelector(".cam-on")) placeholder.querySelector(".cam-on").checked = true;
      return;
    }
    addDebugCameraRow(key, `observation.images.${key}`, true);
  });
  if (!host.children.length) addDebugCameraRow("", "", true);
}

function setDebugStatus(message, isError = false) {
  const status = $("dbg-status");
  if (!status) return;
  status.className = `hw-status${isError ? " error" : ""}`;
  status.textContent = message;
}

function syncDebugActions() {
  const send = $("btn-dbg-send");
  if (!send) return;
  const actions = vizState.chunk && Array.isArray(vizState.chunk.actions) ? vizState.chunk.actions : [];
  const robotConnected = !!(last.robot && last.robot.connected);
  send.disabled = !actions.length || !robotConnected;
  send.title = robotConnected ? "" : "Connect the follower to send actions";
}

function renderDebugPanel() {
  const source = debugSourceInfo();
  const hasCamera = cameraFrameAvailable();
  const taskInput = $("dbg-task");
  if (source && source.task && taskInput && !taskInput.value.trim()) taskInput.value = source.task;
  if (source) ensureDebugCameraRows(source.cameras);
  const selectedCameras = Object.keys(debugCameraMap());
  const label = $("dbg-source");
  if (label) {
    if (!source) label.textContent = "source: open an episode or snapshot";
    else if (!hasCamera) label.textContent = `source: ${source.label} · waiting for a camera frame`;
    else label.textContent = `source: ${source.label} · ${Object.keys(source.joints).length} joints · ${selectedCameras.length}/${source.cameras.length} cam`;
  }
  const run = $("btn-dbg-run");
  if (run) {
    run.disabled = !source || !hasCamera || !selectedCameras.length;
    run.title = selectedCameras.length ? "" : "Select at least one camera input";
  }
  syncDebugActions();
}

function renderChunkOverlay() {
  if (snapshotActive && activeSnapshot) renderSnapshotCharts(activeSnapshot);
  else if (replayActive) renderVizChartsFromState();
}

function buildDebugReference(start, chunkSize, fps) {
  const series = vizState.series || {};
  const track = {};
  jointNames().forEach((name) => {
    const values = series[`act.${name}`];
    if (Array.isArray(values) && values.length) {
      track[name] = values.map((value) => (value == null ? Number.NaN : Number(value)));
    }
  });
  const times = (vizState.times || []).map(Number);
  if (!times.length || !Object.keys(track).length) return [];
  const reference = [];
  for (let index = 0; index < chunkSize; index += 1) {
    const at = start + (index + 1) / fps;
    if (at > times[times.length - 1]) break;
    const sample = sampleJointSeries(times, track, at);
    if (!sample) return [];
    reference.push(sample);
  }
  return reference;
}

function formatChunkEvaluation(evaluation) {
  if (!evaluation) return "";
  const parts = [`score ${Number(evaluation.score).toFixed(1)}`];
  parts.push(`MAE ${Number(evaluation.mae).toFixed(4)}`);
  parts.push(`RMSE ${Number(evaluation.rmse).toFixed(4)}`);
  parts.push(`DTW ${Number(evaluation.dtw).toFixed(4)}`);
  return ` · ${parts.join(" · ")}`;
}

function clearChunkEvaluation() {
  const host = $("dbg-eval");
  if (!host) return;
  host.classList.add("hidden");
  host.replaceChildren();
}

function renderChunkEvaluation(evaluation) {
  const host = $("dbg-eval");
  if (!host || !evaluation) {
    clearChunkEvaluation();
    return;
  }
  const metric = (label, value, className = "") => {
    const item = document.createElement("div");
    item.className = className || "debug-eval-metric";
    const name = document.createElement("span");
    name.className = "debug-eval-label";
    name.textContent = label;
    const number = document.createElement("span");
    number.className = "debug-eval-value";
    number.textContent = Number(value).toFixed(4);
    item.append(name, number);
    return item;
  };
  const score = metric("Chunk score", evaluation.score, "debug-eval-score");
  score.querySelector(".debug-eval-value").textContent = `${Number(evaluation.score).toFixed(1)} / 100`;
  const note = document.createElement("p");
  note.className = "debug-eval-note";
  const coverage = Math.max(0, Math.min(1, Number(evaluation.coverage) || 0));
  note.textContent = `${evaluation.steps}/${evaluation.predicted_steps} steps scored · ${(coverage * 100).toFixed(0)}% reference coverage`;
  host.replaceChildren(
    score,
    metric("MAE", evaluation.mae),
    metric("RMSE", evaluation.rmse),
    metric("DTW", evaluation.dtw),
    note,
  );
  host.classList.remove("hidden");
}

async function runDebugInference() {
  const source = debugSourceInfo();
  if (!source) throw new Error("open an episode or snapshot first");
  const fields = debugFields();
  if (!fields.policy_path) throw new Error("policy path is required");
  const enabledKeys = new Set(Object.keys(fields.camera_map));
  if (!enabledKeys.size) throw new Error("select at least one camera input");
  const frames = stageCameraFrames().filter((frame) => enabledKeys.has(frame.key));
  if (!frames.length) throw new Error("no frame available for the selected cameras");
  clearVizChunk();
  const run = $("btn-dbg-run");
  if (run) run.disabled = true;
  clearChunkEvaluation();
  setDebugStatus("running inference…");
  try {
    const reference = buildDebugReference(source.overlayStart, fields.chunk_size, fields.fps);
    const result = await api("/api/debug/infer", {
      ...fields,
      source: source.source,
      joints: source.joints,
      cameras: frames.slice(0, SNAPSHOT_MAX_CAMERAS),
      reference,
    });
    vizState.chunk = { ...result, start: source.overlayStart };
    syncDebugActions();
    renderChunkOverlay();
    const warnings = (result.warnings || []).join(" · ");
    const referenceNote = reference.length
      ? formatChunkEvaluation(result.evaluation)
      : snapshotActive
        ? " · no reference command in a snapshot"
        : " · no reference command for this window";
    renderChunkEvaluation(reference.length ? result.evaluation : null);
    setDebugStatus(
      `${result.strategy}${result.degraded ? " · degraded" : ""} · ${result.actions.length} steps · ${Number(result.latency_ms).toFixed(0)} ms${referenceNote}${warnings ? ` · ${warnings}` : ""}`,
    );
    localLog(`debug inference: ${result.strategy} · ${result.actions.length} steps · ${Number(result.latency_ms).toFixed(0)} ms`);
  } catch (err) {
    clearChunkEvaluation();
    setDebugStatus(err.message || String(err), true);
    throw err;
  } finally {
    renderDebugPanel();
  }
}

async function sendDebugFirstStep() {
  const actions = vizState.chunk && vizState.chunk.actions;
  if (!actions || !actions.length) throw new Error("run inference first");
  await api("/api/joints", { joints: actions[0].joints, duration_s: 0, live: true });
  localLog("debug chunk: sent first step to the robot");
}

bind("btn-hdr-snapshot", captureSnapshot);
bind("btn-dbg-run", runDebugInference);
bind("btn-dbg-send", sendDebugFirstStep);
bind("btn-dbg-kv-add", () => addKvRow("", "", "dbg-kv", () => {}));
if ($("dbg-policy")) {
  $("dbg-policy").addEventListener("change", () => {
    if ($("dbg-policy").value && $("dbg-path")) $("dbg-path").value = $("dbg-policy").value;
  });
}
if ($("dbg-kv") && !$("dbg-kv").children.length) addKvRow("", "", "dbg-kv", () => {});
if ($("dbg-cam-map") && !$("dbg-cam-map").children.length) addDebugCameraRow("", "", true);

const EPISODE_ICONS = {
  play: `<svg viewBox="0 0 24 24" aria-hidden="true"><polygon points="8 5 19 12 8 19 8 5" fill="currentColor" stroke="none"/></svg>`,
  edit: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4l10-10-4-4L4 16z"/><path d="M13.5 6.5l4 4"/></svg>`,
  delete: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 13h10l1-13"/><path d="M9 7V4h6v3"/></svg>`,
  drag: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="6" r="1.3" fill="currentColor" stroke="none"/><circle cx="15" cy="6" r="1.3" fill="currentColor" stroke="none"/><circle cx="9" cy="12" r="1.3" fill="currentColor" stroke="none"/><circle cx="15" cy="12" r="1.3" fill="currentColor" stroke="none"/><circle cx="9" cy="18" r="1.3" fill="currentColor" stroke="none"/><circle cx="15" cy="18" r="1.3" fill="currentColor" stroke="none"/></svg>`,
};

function makeEpisodeIconButton(className, label, icon) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `ghost icon-btn ${className}`;
  button.title = label;
  button.setAttribute("aria-label", label);
  button.innerHTML = icon;
  return button;
}

async function reorderEpisodes(videoId, order) {
  if (episodeMutationPending) return;
  const replayingThisVideo = replayActive && vizState.kind === "video" && vizState.id === videoId;
  const previousEpisode = replayingThisVideo ? vizState.episode : null;
  const wasPlaying = replayingThisVideo && vizState.playing;
  if (replayingThisVideo) {
    pauseVizVideos();
    vizState.episode = -1;
    renderEpisodes();
  }
  setEpisodeMutationPending(true);
  try {
    const result = await api(`/api/videos/${encodeURIComponent(videoId)}/episodes/reorder`, { order });
    await refreshLibrarySection("videos");
    await loadEpisodeList("video", videoId);
    if (replayingThisVideo) {
      const mapped = result.episode_index_map && result.episode_index_map[String(previousEpisode)];
      if (mapped == null) leaveReplay();
      else await loadPreview("video", videoId, Number(mapped), wasPlaying);
    }
  } catch (err) {
    if (replayingThisVideo && previousEpisode != null) {
      vizState.episode = previousEpisode;
      renderEpisodes();
      if (wasPlaying) playVizVideos();
    }
    toastError(err);
  } finally {
    setEpisodeMutationPending(false);
  }
}

function makeEpisodeDragHandle(handle, li, list, videoId) {
  let dragState = null;

  const finishDrag = async () => {
    if (!dragState) return;
    const initialOrder = dragState.initialOrder;
    dragState = null;
    li.classList.remove("dragging");
    list.querySelectorAll(".ep-row.drag-over").forEach((row) => row.classList.remove("drag-over"));
    handle.removeEventListener("pointermove", onMove);
    handle.removeEventListener("pointerup", finishDrag);
    handle.removeEventListener("pointercancel", finishDrag);
    const order = [...list.querySelectorAll("li[data-episode-index]")].map((row) => Number(row.dataset.episodeIndex));
    if (order.join(",") !== initialOrder.join(",")) await reorderEpisodes(videoId, order);
  };

  const onMove = (event) => {
    if (!dragState) return;
    const target = document.elementFromPoint(event.clientX, event.clientY)?.closest("li[data-episode-index]");
    list.querySelectorAll(".ep-row.drag-over").forEach((row) => row.classList.remove("drag-over"));
    if (!target || target === li || !list.contains(target)) return;
    const rect = target.getBoundingClientRect();
    const insertBefore = event.clientY < rect.top + rect.height / 2;
    target.classList.add("drag-over");
    list.insertBefore(li, insertBefore ? target : target.nextSibling);
  };

  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || dragState) return;
    event.preventDefault();
    event.stopPropagation();
    dragState = {
      initialOrder: [...list.querySelectorAll("li[data-episode-index]")].map((row) => Number(row.dataset.episodeIndex)),
    };
    li.classList.add("dragging");
    handle.setPointerCapture(event.pointerId);
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", finishDrag);
    handle.addEventListener("pointercancel", finishDrag);
  });
  handle.addEventListener("click", (event) => event.stopPropagation());
  handle.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    const rows = [...list.querySelectorAll("li[data-episode-index]")];
    const from = rows.indexOf(li);
    moveEpisode(videoId, from, event.key === "ArrowUp" ? -1 : 1);
  });
}

function episodeTooltip(ep) {
  const parts = [ep.task, ep.note].filter((value) => value && String(value).trim());
  return parts.join("\n") || `episode ${ep.index}`;
}

function episodeDraftKey(source, index) {
  return source ? `${source.kind}:${source.id}:${index}` : "";
}

function setEpisodeMutationPending(pending) {
  episodeMutationPending = pending;
  renderEpisodes();
}

function makeEpisodeEditor(ep) {
  const source = episodeSource ? { ...episodeSource } : null;
  const draftKey = episodeDraftKey(source, ep.index);
  const draft = episodeDrafts.get(draftKey) || {
    name: ep.name || "",
    task: ep.task || "",
    note: ep.note || "",
  };
  const box = document.createElement("div");
  box.className = "ep-editor";
  const inputs = {};
  [["name", "Name", "e.g. grasp"], ["task", "Task", ""], ["note", "Note", ""]].forEach(([key, label, placeholder]) => {
    const row = document.createElement("label");
    row.textContent = label;
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = placeholder;
    input.value = draft[key] || "";
    input.disabled = episodeMutationPending;
    input.addEventListener("input", () => {
      draft[key] = input.value;
      episodeDrafts.set(draftKey, draft);
    });
    inputs[key] = input;
    row.appendChild(input);
    box.appendChild(row);
  });
  const actions = document.createElement("div");
  actions.className = "row-actions";
  const save = document.createElement("button");
  save.type = "button";
  save.textContent = "Save";
  save.disabled = episodeMutationPending;
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "ghost";
  cancel.textContent = "Cancel";
  cancel.disabled = episodeMutationPending;
  save.addEventListener("click", async (event) => {
    event.stopPropagation();
    if (episodeMutationPending) return;
    setEpisodeMutationPending(true);
    try {
      if (!source) throw new Error("episode source is no longer selected");
      const payload = { kind: source.kind, id: source.id, episode: ep.index };
      Object.entries(draft).forEach(([key, value]) => { payload[key] = String(value).trim(); });
      Object.assign(ep, await api("/api/episodes", payload, "PUT"));
      episodeDrafts.delete(draftKey);
      expandedEpisode = null;
    } catch (err) {
      toastError(err);
    } finally {
      setEpisodeMutationPending(false);
    }
  });
  cancel.addEventListener("click", (event) => {
    event.stopPropagation();
    episodeDrafts.delete(draftKey);
    expandedEpisode = null;
    renderEpisodes();
  });
  actions.append(save, cancel);
  box.appendChild(actions);
  box.addEventListener("click", (event) => event.stopPropagation());
  return box;
}

function toggleEpisodeEditor(index) {
  expandedEpisode = expandedEpisode === index ? null : index;
  renderEpisodes();
}

function openEpisodeDescriptionEditor() {
  if (!episodeSource || episodeMutationPending || $("ep-description-editor")) return;
  const description = $("ep-description");
  if (!description || description.hidden) return;
  const editor = document.createElement("div");
  editor.id = "ep-description-editor";
  editor.className = "ep-description-editor";
  const input = document.createElement("textarea");
  input.rows = 3;
  input.value = episodeSource.description || "";
  input.placeholder = "description";
  input.setAttribute("aria-label", "Library description");
  const actions = document.createElement("div");
  actions.className = "row-actions";
  const save = document.createElement("button");
  save.type = "button";
  save.textContent = "Save";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "ghost";
  cancel.textContent = "Cancel";
  actions.append(save, cancel);
  editor.append(input, actions);
  description.hidden = true;
  description.parentElement.appendChild(editor);
  input.focus();
  const close = () => {
    editor.remove();
    description.hidden = false;
  };
  save.addEventListener("click", async () => {
    save.disabled = true;
    cancel.disabled = true;
    input.disabled = true;
    try {
      await saveLibraryOverride(episodeSource.kind, episodeSource.id, { description: input.value.trim() });
      close();
      renderEpisodes();
    } catch (err) {
      toastError(err);
      close();
    }
  });
  cancel.addEventListener("click", close);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
    }
  });
}

function renderEpisodes() {
  const detail = $("vid-detail");
  const list = $("ep-list");
  if (!detail || !list) return;
  if (!episodeSource) {
    detail.classList.add("hidden");
    return;
  }
  detail.classList.remove("hidden");
  const sourceTitle = episodeSource.title || episodeSource.id;
  const subtitle = episodeSource.subtitle || "";
  const description = episodeSource.description || "";
  if ($("ep-title")) {
    $("ep-title").textContent = `${sourceTitle} · ${episodeRows.length} ep`;
    $("ep-title").title = sourceTitle;
  }
  if ($("ep-subtitle")) {
    $("ep-subtitle").textContent = subtitle;
    $("ep-subtitle").hidden = !subtitle;
    $("ep-subtitle").title = subtitle;
  }
  if ($("ep-description")) {
    const descriptionNode = $("ep-description");
    descriptionNode.textContent = description || "Add description";
    descriptionNode.hidden = false;
    descriptionNode.classList.toggle("empty", !description);
    descriptionNode.title = description || "Add description";
    descriptionNode.tabIndex = 0;
    descriptionNode.setAttribute("role", "button");
    descriptionNode.setAttribute("aria-label", description ? "Edit description" : "Add description");
  }
  list.innerHTML = "";
  list.setAttribute("aria-busy", String(episodeLoading));
  if (episodeLoading) {
    list.innerHTML = `<li class="library-message">Loading episodes…</li>`;
    return;
  }
  if (episodeError) {
    const item = document.createElement("li");
    item.className = "library-message error";
    item.textContent = `Could not load episodes: ${episodeError}`;
    list.appendChild(item);
    return;
  }
  if (!episodeRows.length) {
    list.innerHTML = `<li class="empty">no episode found</li>`;
    return;
  }
  episodeRows.forEach((ep) => {
    const source = { ...episodeSource };
    const li = document.createElement("li");
    li.className = "ep-row";
    li.dataset.episodeIndex = String(ep.index);
    const isReplaying = replayActive
      && vizState.kind === episodeSource.kind
      && vizState.id === episodeSource.id
      && vizState.episode === ep.index;
    if (isReplaying) li.classList.add("sel");
    const head = document.createElement("div");
    head.className = "ep-head";
    const primary = document.createElement("button");
    primary.type = "button";
    primary.className = "ep-primary";
    primary.setAttribute("aria-label", `View episode ${ep.index}${ep.name || ep.task ? `: ${ep.name || ep.task}` : ""}`);
    const index = document.createElement("span");
    index.className = "ep-index";
    index.textContent = `ep ${ep.index}`;
    const label = document.createElement("span");
    label.className = "ep-label";
    label.textContent = ep.name || ep.task || "";
    label.title = episodeTooltip(ep);
    const tools = document.createElement("span");
    tools.className = "ep-icons";
    const edit = makeEpisodeIconButton("ep-edit", `Edit episode ${ep.index}`, EPISODE_ICONS.edit);
    edit.disabled = episodeMutationPending;
    edit.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleEpisodeEditor(ep.index);
    });
    tools.append(edit);
    if (source.kind === "video") {
      const remove = makeEpisodeIconButton("ep-del", `Delete episode ${ep.index}`, EPISODE_ICONS.delete);
      remove.disabled = episodeMutationPending;
      remove.addEventListener("click", (event) => {
        event.stopPropagation();
        deleteEpisode(source.id, ep.index);
      });
      const drag = makeEpisodeIconButton("ep-drag", `Drag episode ${ep.index} to reorder`, EPISODE_ICONS.drag);
      drag.disabled = episodeMutationPending;
      makeEpisodeDragHandle(drag, li, list, source.id);
      tools.append(remove, drag);
    }
    primary.append(index, label);
    primary.addEventListener("click", () => playEpisode(source.kind, source.id, ep.index));
    head.append(primary, tools);
    li.appendChild(head);
    if (expandedEpisode === ep.index) li.appendChild(makeEpisodeEditor(ep));
    list.appendChild(li);
  });
}

async function fetchJson(path) {
  const res = await fetch(BASE + path);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const error = new Error(data.detail || res.statusText || "request failed");
    error.status = res.status;
    throw error;
  }
  return data;
}

function normalizeEpisodeItems(episodes, fallbackPlayable) {
  return (episodes || []).map((ep, i) => {
    const hasVideo = Array.isArray(ep.videos)
      ? ep.videos.length > 0
      : (ep.has_video == null ? undefined : Boolean(ep.has_video));
    return {
      index: Number(ep.index == null ? i : ep.index),
      name: String(ep.name || ""),
      task: String(ep.task || ""),
      note: String(ep.note || ""),
      playable: ep.playable == null
        ? (hasVideo == null ? fallbackPlayable : hasVideo)
        : Boolean(ep.playable),
      has_video: hasVideo,
    };
  });
}

async function loadLegacyEpisodeList(kind, id) {
  if (kind === "video") {
    const row = await fetchJson(`/api/videos/${encodeURIComponent(id)}`);
    return {
      title: row.name || id,
      episodes: normalizeEpisodeItems(row.episodes || [], true),
    };
  }
  if (kind !== "dataset") throw new Error(`unknown episode source kind '${kind}'`);
  let row = datasetsCache.find((item) => (item.repo_id || item.id) === id);
  if (!row) {
    const rows = await fetchJson("/api/datasets");
    row = Array.isArray(rows) ? rows.find((item) => (item.repo_id || item.id) === id) : null;
  }
  if (!row) throw new Error(`unknown dataset '${id}'`);
  const count = Math.max(0, Number(row.episodes || 0));
  return {
    title: row.repo_id || id,
    episodes: Array.from({ length: count }, (_, index) => ({
      index,
      name: "",
      task: "",
      note: "",
      playable: Boolean(row.playable),
      has_video: Boolean(row.playable),
    })),
  };
}

async function loadEpisodeList(kind, id, title = id) {
  const generation = ++episodeRequestGeneration;
  const sameSource = !!episodeSource && episodeSource.kind === kind && episodeSource.id === id;
  if (!sameSource) {
    expandedEpisode = null;
    episodeDrafts.clear();
  }
  openEpisodeSelection();
  episodeSource = { kind, id, title, subtitle: "", description: "" };
  episodeRows = [];
  episodeLoading = true;
  episodeError = "";
  renderVideos();
  renderDatasets();
  renderEpisodes();
  let data;
  try {
    data = await fetchJson(`/api/episodes?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}`);
  } catch (err) {
    if (err.status !== 404 && err.status !== 405) {
      if (generation === episodeRequestGeneration) {
        episodeLoading = false;
        episodeError = err.message || String(err);
        renderEpisodes();
      }
      throw err;
    }
    try {
      data = await loadLegacyEpisodeList(kind, id);
    } catch (legacyError) {
      if (generation === episodeRequestGeneration) {
        episodeLoading = false;
        episodeError = legacyError.message || String(legacyError);
        renderEpisodes();
      }
      throw legacyError;
    }
  }
  if (generation !== episodeRequestGeneration) return false;
  const source = data.source || {};
  episodeSource = {
    kind,
    id,
    title: source.title || data.title || id,
    subtitle: source.subtitle || "",
    description: source.description || "",
  };
  episodeRows = normalizeEpisodeItems(data.episodes, Boolean(data.playable));
  episodeLoading = false;
  episodeError = "";
  episodeSignatureCache = episodeSignature();
  renderEpisodes();
  updateResumeTargetUi();
  return true;
}

function episodeSignature() {
  if (!episodeSource) return "";
  if (episodeSource.kind === "video") {
    const vid = videosCache.find((row) => row.id === episodeSource.id);
    if (!vid) return "";
    return (vid.episodes || [])
      .map((ep) => `${ep.index}:${ep.name || ""}:${ep.task || ""}:${ep.note || ""}`)
      .join("|");
  }
  const ds = datasetsCache.find((row) => (row.repo_id || row.id) === episodeSource.id);
  if (!ds) return "";
  return `${ds.episodes == null ? "" : ds.episodes}:${ds.playable ? 1 : 0}`;
}

const PLAY_ICON = `<polygon points="8 5 19 12 8 19 8 5" fill="currentColor" stroke="none"/>`;
const PAUSE_ICON = `<rect x="8" y="5" width="3" height="14" fill="currentColor" stroke="none"/><rect x="14" y="5" width="3" height="14" fill="currentColor" stroke="none"/>`;

const vizState = {
  kind: "",
  id: "",
  episode: 0,
  episodes: 0,
  duration: 0,
  elapsed: 0,
  playing: false,
  clockBaseElapsed: 0,
  clockStartedAt: 0,
  clockFrame: null,
  previewReady: false,
  previewGeneration: 0,
  title: "",
  task: "",
  previewMeta: "",
  times: [],
  arm: { times: [], track: {} },
  autoplay: false,
  snapshot: null,
  series: {},
  chunk: null,
  rolloutPredictions: [],
};

function clearVizChunk() {
  clearChunkEvaluation();
  if (!vizState.chunk) return;
  vizState.chunk = null;
  syncDebugActions();
  if (snapshotActive && activeSnapshot) renderSnapshotCharts(activeSnapshot);
  else if (replayActive) renderVizChartsFromState();
}

function jointNames() {
  return meta.joints && meta.joints.length ? meta.joints : JOINT_FALLBACK;
}

function syncReplayPlayState() {
  const playing = vizState.playing;
  const button = $("viz-play");
  if (button) {
    const svg = button.querySelector("svg");
    if (svg) svg.innerHTML = playing ? PAUSE_ICON : PLAY_ICON;
    const label = playing ? "Pause" : "Play";
    button.title = label;
    button.setAttribute("aria-label", label);
    button.classList.toggle("on", playing);
  }
}

function syncReplayAvailability() {
  const ready = replayActive
    && vizState.previewReady
    && vizState.previewGeneration === previewRequestGeneration;
  ["viz-play", "viz-restart", "viz-arm"].forEach((id) => {
    const control = $(id);
    if (control) control.disabled = !ready;
  });
  const seek = $("replay-seek");
  if (seek) seek.disabled = !ready;
}

function syncArmToggle() {
  const button = $("viz-arm");
  if (button) {
    button.classList.toggle("on", armReplay);
    button.setAttribute("aria-pressed", String(armReplay));
    button.title = armReplay ? "Replaying actions on robot (on)" : "Replay actions on robot (off)";
  }
  const label = $("viz-arm-label");
  if (label) label.textContent = armReplay ? "arm on" : "arm off";
}

function syncModeBadges() {
  if ($("replay-badge")) $("replay-badge").classList.toggle("hidden", snapshotActive);
  if ($("snapshot-badge")) $("snapshot-badge").classList.toggle("hidden", !snapshotActive);
  if ($("btn-snap-edit")) $("btn-snap-edit").classList.toggle("hidden", !snapshotActive);
}

function enterReplay({ showEpisodes = true } = {}) {
  setEpisodePanelVisible(showEpisodes);
  replayActive = true;
  replaySeekId = null;
  replayScrubPointerId = null;
  const stage = $("replay");
  if (stage) stage.classList.remove("hidden");
  const host = $("cameras");
  if (host) host.classList.add("replaying");
  const viz = $("viz");
  if (viz) viz.classList.remove("hidden");
  document.querySelectorAll(".chart-wrap").forEach((el) => el.classList.add("replay"));
  const seek = $("replay-seek");
  if (seek) {
    seek.disabled = !vizState.previewReady;
    seek.value = "0";
  }
  syncModeBadges();
  const transport = document.querySelector(".replay-transport");
  if (transport) transport.classList.toggle("hidden", snapshotActive);
  const armToggle = $("viz-arm");
  if (armToggle) armToggle.classList.toggle("hidden", snapshotActive);
  if ($("chart-action-title")) $("chart-action-title").textContent = "Control state";
  syncArmToggle();
  syncReplayPlayState();
  syncReplayAvailability();
  renderEpisodes();
  syncSnapshotButton();
  renderDebugPanel();
}

function leaveReplay() {
  if (!replayActive) return;
  previewRequestGeneration += 1;
  replayActive = false;
  replaySeekId = null;
  replayScrubPointerId = null;
  replayScrubWasPlaying = false;
  armReplay = false;
  stopArmReplayLoop();
  stopReplayClock();
  vizState.playing = false;
  vizState.previewReady = false;
  vizState.elapsed = 0;
  clearVizChunk();
  snapshotActive = false;
  activeSnapshot = null;
  vizState.snapshot = null;
  const debugEditor = $("dbg-snap-editor");
  if (debugEditor) {
    debugEditor.classList.add("hidden");
    debugEditor.innerHTML = "";
  }
  vizVideos().forEach((video) => video.pause());
  const host = $("viz-cams");
  if (host) {
    host.innerHTML = "";
    host.classList.remove("snapshot-view");
  }
  const stage = $("replay");
  if (stage) {
    stage.classList.add("hidden");
    stage.classList.remove("snapshot-mode");
  }
  syncModeBadges();
  const cams = $("cameras");
  if (cams) cams.classList.remove("replaying");
  document.querySelectorAll(".chart-wrap").forEach((el) => el.classList.remove("replay"));
  const seek = $("replay-seek");
  if (seek) {
    seek.disabled = true;
    seek.value = "0";
  }
  if ($("viz")) $("viz").classList.add("hidden");
  if ($("viz-t")) $("viz-t").value = "0.00s / 0.00s";
  if ($("chart-action-title")) $("chart-action-title").textContent = "Commanded action";
  resetReplayCharts();
  syncArmToggle();
  syncReplayPlayState();
  syncReplayAvailability();
  renderEpisodes();
  renderSnapshots();
  renderDebugPanel();
  syncSnapshotButton();
  localLog("replay stopped");
}

function setEpisodePanelVisible(visible) {
  episodeSelectionClosed = !visible;
  const episodes = $("episodes");
  const splitter = $("split-ep");
  if (episodes) episodes.classList.toggle("hidden", !visible);
  if (splitter) splitter.classList.toggle("hidden", !visible);
  const main = document.querySelector("main");
  if (main) main.classList.toggle("episodes-closed", !visible);
}

function openEpisodeSelection() {
  setEpisodePanelVisible(true);
}

function closeEpisodeSelection() {
  episodeRequestGeneration += 1;
  previewRequestGeneration += 1;
  leaveReplay();
  armReplay = false;
  stopArmReplayLoop();
  episodeSource = null;
  episodeRows = [];
  expandedEpisode = null;
  episodeLoading = false;
  episodeError = "";
  episodeDrafts.clear();
  vizState.kind = "";
  vizState.id = "";
  vizState.episode = 0;
  vizState.episodes = 0;
  vizState.duration = 0;
  vizState.elapsed = 0;
  vizState.times = [];
  vizState.previewMeta = "";
  vizState.previewReady = false;
  vizState.previewGeneration = 0;
  vizState.arm = { times: [], track: {} };
  vizState.snapshot = null;
  clearVizChunk();
  snapshotActive = false;
  activeSnapshot = null;
  setEpisodePanelVisible(false);
  renderVideos();
  renderDatasets();
  renderSnapshots();
  renderEpisodes();
  updateResumeTargetUi();
  renderDebugPanel();
  syncSnapshotButton();
}

function exitReplay() {
  closeEpisodeSelection();
}

function syncReplayChart(chart, elapsed, duration) {
  if (!chart) return;
  const seriesEnd = Number(chart.$replaySeriesEnd) || 0;
  const domainMax = Math.max(1, Number(duration) || 0, seriesEnd);
  chart.$replayCursorTime = Math.min(domainMax, Math.max(0, Number(elapsed) || 0));
  const xOptions = chart.options.scales && chart.options.scales.x;
  if (!xOptions || xOptions.type !== "linear") return;
  if (xOptions.min !== 0 || xOptions.max !== domainMax) {
    xOptions.min = 0;
    xOptions.max = domainMax;
    chart.update("none");
  }
  positionReplayChartCursor(chart);
}

function updateReplayBar(elapsed, duration) {
  const frac = duration > 0 ? Math.min(1, Math.max(0, elapsed / duration)) : 0;
  const value = String(Math.round(frac * 1000));
  const seek = $("replay-seek");
  if (seek && replaySeekId !== "replay-seek") {
    seek.value = value;
    seek.setAttribute("aria-valuetext", `${elapsed.toFixed(2)} of ${duration.toFixed(2)} seconds`);
  }
  if ($("viz-t")) $("viz-t").value = `${elapsed.toFixed(2)}s / ${duration.toFixed(2)}s`;
  [stateChart, actionChart].forEach((chart) => syncReplayChart(chart, elapsed, duration));
}

function previewFileUrl(kind, id, episode, cam) {
  return `${BASE}/api/preview/file?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}&episode=${episode}&cam=${encodeURIComponent(cam)}&t=${Date.now()}`;
}

function vizVideos() {
  return [...document.querySelectorAll("#viz-cams video")];
}

function clipWindow(video) {
  const start = Number(video.dataset.start || 0);
  const declaredEnd = Number(video.dataset.end);
  const declaredDuration = Number(video.dataset.mediaDuration);
  const nativeEnd = Number.isFinite(video.duration) ? video.duration : 0;
  const end = Number.isFinite(declaredEnd) && declaredEnd > start
    ? declaredEnd
    : (Number.isFinite(declaredDuration) && declaredDuration > 0 ? start + declaredDuration : nativeEnd);
  const duration = Math.max(0, end - start);
  return { start, end: Math.max(start, end), duration };
}

function seriesDuration() {
  const times = vizState.times;
  return times.length ? Math.max(0, Number(times[times.length - 1])) : 0;
}

function cameraOffset(video) {
  const offset = Number(video.dataset.timelineOffset);
  return Number.isFinite(offset) ? Math.max(0, offset) : 0;
}

function replayDurationFromMedia() {
  return vizVideos().reduce((duration, video) => {
    const stream = clipWindow(video);
    return Math.max(duration, cameraOffset(video) + stream.duration);
  }, 0);
}

function refreshReplayDuration() {
  vizState.duration = Math.max(vizState.duration || 0, seriesDuration(), replayDurationFromMedia());
  return vizState.duration;
}

function stopReplayClock() {
  if (vizState.clockFrame != null) cancelAnimationFrame(vizState.clockFrame);
  vizState.clockFrame = null;
}

function setCameraTimelineState(video, waiting) {
  const wrap = video.closest(".viz-cam");
  if (!wrap) return;
  wrap.classList.toggle("timeline-waiting", waiting);
  const state = wrap.querySelector(".media-state");
  if (state && waiting) state.textContent = `Starts at ${cameraOffset(video).toFixed(2)}s`;
}

function syncVideoToElapsed(video, elapsed, shouldPlay) {
  if (video.dataset.failed === "true") return;
  const stream = clipWindow(video);
  const offset = cameraOffset(video);
  const localElapsed = elapsed - offset;
  const waiting = localElapsed < 0;
  setCameraTimelineState(video, waiting);
  if (waiting) {
    video.pause();
    if (video.readyState >= 1 && Math.abs(video.currentTime - stream.start) > 0.03) video.currentTime = stream.start;
    return;
  }
  if (video.readyState < 1 || stream.duration <= 0) return;
  const target = stream.start + Math.min(stream.duration, Math.max(0, localElapsed));
  if (Math.abs(video.currentTime - target) > 0.18) video.currentTime = target;
  const streamFinished = localElapsed >= stream.duration - 0.02;
  if (shouldPlay && !streamFinished) video.play().catch(() => {});
  else video.pause();
}

function syncReplayMedia(elapsed, shouldPlay = vizState.playing) {
  vizVideos().forEach((video) => syncVideoToElapsed(video, elapsed, shouldPlay));
}

function setReplayElapsed(elapsed) {
  const duration = refreshReplayDuration();
  vizState.elapsed = Math.min(duration, Math.max(0, Number(elapsed) || 0));
  vizState.clockBaseElapsed = vizState.elapsed;
  vizState.clockStartedAt = performance.now();
  clearVizChunk();
  syncReplayMedia(vizState.elapsed);
  updateReplayBar(vizState.elapsed, duration);
}

function tickReplayClock(now) {
  if (
    !vizState.playing
    || !replayActive
    || !vizState.previewReady
    || vizState.previewGeneration !== previewRequestGeneration
  ) return;
  const duration = refreshReplayDuration();
  const elapsed = Math.min(duration, vizState.clockBaseElapsed + (now - vizState.clockStartedAt) / 1000);
  vizState.elapsed = elapsed;
  syncReplayMedia(elapsed, true);
  updateReplayBar(elapsed, duration);
  if (duration <= 0 || elapsed >= duration - 0.001) {
    pauseVizVideos();
    return;
  }
  vizState.clockFrame = requestAnimationFrame(tickReplayClock);
}

function seekViz(elapsed) {
  setReplayElapsed(elapsed);
}

function seekReplayFraction(fraction) {
  seekViz(fraction * refreshReplayDuration());
}

function beginReplayScrub(id, pointerId = null) {
  if (replaySeekId) return replaySeekId === id && replayScrubPointerId === pointerId;
  replaySeekId = id;
  replayScrubPointerId = pointerId;
  replayScrubWasPlaying = vizState.playing;
  if (replayScrubWasPlaying) pauseVizVideos();
  return true;
}

function finishReplayScrub(id, pointerId = null) {
  if (!replaySeekId || (id && replaySeekId !== id)) return;
  if (replayScrubPointerId !== pointerId) return;
  replaySeekId = null;
  replayScrubPointerId = null;
  const shouldResume = replayScrubWasPlaying
    && replayActive
    && vizState.elapsed < refreshReplayDuration() - 0.001;
  replayScrubWasPlaying = false;
  if (shouldResume) playVizVideos();
}

function bindReplaySeek(id) {
  const seek = $(id);
  if (!seek) return;
  seek.addEventListener("pointerdown", (event) => {
    if (!beginReplayScrub(id, event.pointerId)) {
      event.preventDefault();
      return;
    }
    try {
      seek.setPointerCapture(event.pointerId);
    } catch {
      finishReplayScrub(id, event.pointerId);
    }
  });
  seek.addEventListener("input", () => {
    if (!replaySeekId && !beginReplayScrub(id)) return;
    if (replaySeekId !== id) return;
    seekReplayFraction(Number(seek.value) / 1000);
  });
  seek.addEventListener("change", () => finishReplayScrub(id));
  seek.addEventListener("pointerup", (event) => {
    if (seek.hasPointerCapture(event.pointerId)) seek.releasePointerCapture(event.pointerId);
    finishReplayScrub(id, event.pointerId);
  });
  seek.addEventListener("pointercancel", (event) => finishReplayScrub(id, event.pointerId));
  seek.addEventListener("lostpointercapture", (event) => finishReplayScrub(id, event.pointerId));
}

function seekReplayFromChart(chart, event) {
  const xScale = chart && chart.scales && chart.scales.x;
  const area = chart && chart.chartArea;
  const canvas = chart && chart.canvas;
  if (!xScale || !area || !canvas) return;
  updateChartHoverFromClient(chart, event);
  const rect = canvas.getBoundingClientRect();
  if (!rect.width) return;
  const canvasX = ((event.clientX - rect.left) * chart.width) / rect.width;
  const pixel = Math.min(area.right, Math.max(area.left, canvasX));
  seekViz(snapChartTimeToFrame(chart, xScale.getValueForPixel(pixel)));
}

function updateChartHoverFromClient(chart, event) {
  const canvas = chart && chart.canvas;
  const xScale = chart && chart.scales && chart.scales.x;
  const area = chart && chart.chartArea;
  if (!canvas || !xScale || !area) return;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height || !chart.width || !chart.height) return;
  const pointerX = ((event.clientX - rect.left) * chart.width) / rect.width;
  const pointerY = ((event.clientY - rect.top) * chart.height) / rect.height;
  if (
    pointerX < area.left
    || pointerX > area.right
    || pointerY < area.top
    || pointerY > area.bottom
  ) {
    chart.$pointerPosition = null;
    hideChartHoverTooltip(chart);
    if (chart.$timeAxis === false) chart.draw();
    return;
  }
  chart.$pointerPosition = { x: pointerX, y: pointerY, inside: true };
  const seconds = snapChartTimeToFrame(chart, xScale.getValueForPixel(pointerX));
  scheduleChartHoverTooltip(chart, seconds, pointerX, pointerY);
  if (chart.$timeAxis === false) chart.draw();
}

function stepReplayFrame(chart, direction) {
  const frames = Array.isArray(chart && chart.$frameTimes) ? chart.$frameTimes : [];
  if (!frames.length) return;
  const current = snapChartTimeToFrame(chart, vizState.elapsed);
  let low = 0;
  let high = frames.length - 1;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (Number(frames[middle]) < current) low = middle + 1;
    else high = middle;
  }
  const currentIndex = low;
  const nextIndex = Math.max(0, Math.min(frames.length - 1, currentIndex + direction));
  if (nextIndex === currentIndex) return;
  if (vizState.playing) pauseVizVideos();
  seekViz(Number(frames[nextIndex]));
}

function bindReplayChartWheel(chart) {
  if (!chart || !chart.canvas) return;
  const canvas = chart.canvas;
  let accumulatedDelta = 0;
  canvas.addEventListener("wheel", (event) => {
    if (!replayActive || !vizState.previewReady || chart.$timeAxis !== false) return;
    event.preventDefault();
    updateChartHoverFromClient(chart, event);
    const unit = event.deltaMode === 1 ? 16 : (event.deltaMode === 2 ? 100 : 1);
    const delta = Number(event.deltaY) * unit;
    if (!Number.isFinite(delta) || delta === 0) return;
    if (Math.sign(delta) !== Math.sign(accumulatedDelta)) accumulatedDelta = 0;
    accumulatedDelta += delta;
    const threshold = 100;
    if (Math.abs(accumulatedDelta) < threshold) return;
    const direction = accumulatedDelta < 0 ? -1 : 1;
    stepReplayFrame(chart, direction);
    accumulatedDelta = 0;
  }, { passive: false });
}

function bindReplayChartSeek(chart, id) {
  if (!chart || !chart.canvas) return;
  const canvas = chart.canvas;
  let pointerId = null;
  canvas.addEventListener("pointerdown", (event) => {
    if (pointerId != null || event.button !== 0 || !replayActive || !vizState.previewReady) return;
    if (!beginReplayScrub(id, event.pointerId)) return;
    try {
      canvas.setPointerCapture(event.pointerId);
    } catch {
      finishReplayScrub(id, event.pointerId);
      return;
    }
    pointerId = event.pointerId;
    seekReplayFromChart(chart, event);
    event.preventDefault();
  });
  canvas.addEventListener("pointermove", (event) => {
    if (pointerId !== event.pointerId || replaySeekId !== id) return;
    seekReplayFromChart(chart, event);
  });
  const finish = (event, seekFinal = true) => {
    if (pointerId !== event.pointerId) return;
    if (seekFinal) seekReplayFromChart(chart, event);
    if (canvas.hasPointerCapture(pointerId)) canvas.releasePointerCapture(pointerId);
    pointerId = null;
    finishReplayScrub(id, event.pointerId);
  };
  canvas.addEventListener("pointerup", finish);
  canvas.addEventListener("pointercancel", (event) => finish(event, false));
  canvas.addEventListener("lostpointercapture", (event) => {
    pointerId = null;
    finishReplayScrub(id, event.pointerId);
  });
}

function onVizTime(ev) {
  if (!vizState.playing || replaySeekId || ev.target.dataset.failed === "true") return;
  syncVideoToElapsed(ev.target, vizState.elapsed, true);
}

function setReplayStatus(message, isError = false) {
  const status = $("replay-status");
  if (!status) return;
  status.textContent = message;
  status.classList.toggle("error", isError);
}

function renderReplayMessage(message, isError = false) {
  const host = $("viz-cams");
  if (host) {
    host.innerHTML = "";
    const state = document.createElement("p");
    state.className = `media-state${isError ? " error" : ""}`;
    state.setAttribute("role", "status");
    state.textContent = message;
    host.appendChild(state);
  }
  setReplayStatus(message, isError);
}

function renderVizCams(cameras, generation) {
  const host = $("viz-cams");
  if (!host) return;
  host.innerHTML = "";
  host.classList.toggle("multi", (cameras || []).length > 1);
  if (!(cameras || []).length) {
    renderReplayMessage("No playable video for this episode; time-series data remains available.");
    vizState.duration = Math.max(vizState.duration, seriesDuration());
    updateReplayBar(0, vizState.duration);
    return;
  }
  (cameras || []).forEach((cam) => {
    const wrap = document.createElement("div");
    wrap.className = "viz-cam";
    wrap.innerHTML = `<span></span><p class="media-state" role="status">Loading video…</p><video class="media-loading" playsinline muted></video>`;
    const requestedFps = finitePositive(cam.requested_fps ?? cam.requested_video_fps);
    const encodedFps = finitePositive(cam.encoded_fps ?? cam.encoded_video_fps ?? cam.effective_encoding_fps);
    const encodedFrames = Number(cam.encoded_frames ?? cam.encoded_video_frames ?? cam.encoded_frame_count);
    const cameraMeta = [
      requestedFps ? `requested ${requestedFps} Hz` : "",
      encodedFps ? `encoded ${encodedFps} Hz` : "",
      Number.isFinite(encodedFrames) && encodedFrames >= 0 ? `${encodedFrames} encoded frames` : "",
    ].filter(Boolean).join(" · ");
    const cameraLabel = wrap.querySelector("span");
    cameraLabel.textContent = `${cam.name}${cameraMeta ? ` · ${cameraMeta}` : ""}`;
    cameraLabel.title = cameraLabel.textContent;
    const video = wrap.querySelector("video");
    video.dataset.start = String(cam.start || 0);
    video.dataset.end = cam.end != null ? String(cam.end) : "";
    video.dataset.timelineOffset = String(cam.timeline_offset_s || 0);
    const cameraDuration = cam.duration_s ?? cam.media_duration_s;
    video.dataset.mediaDuration = cameraDuration != null ? String(cameraDuration) : "";
    video.src = previewFileUrl(vizState.kind, vizState.id, vizState.episode, cam.name);
    video.addEventListener("loadedmetadata", () => {
      if (generation !== previewRequestGeneration) return;
      wrap.classList.add("media-ready");
      video.classList.remove("media-loading");
      setReplayStatus(vizState.previewMeta ? `Ready · ${vizState.previewMeta}` : "Ready");
      refreshReplayDuration();
      syncVideoToElapsed(video, vizState.elapsed, vizState.playing);
      updateReplayBar(vizState.elapsed, vizState.duration);
      syncSnapshotButton();
      renderDebugPanel();
      if (vizState.autoplay && !vizState.playing) playVizVideos();
    });
    video.addEventListener("timeupdate", onVizTime);
    video.addEventListener("ended", () => {
      if (generation !== previewRequestGeneration || !replayActive) return;
      syncVideoToElapsed(video, vizState.elapsed, false);
    });
    video.addEventListener("error", () => {
      if (generation !== previewRequestGeneration) return;
      const state = wrap.querySelector(".media-state");
      if (state) {
        state.textContent = "Video could not be loaded. Check that the recorded file still exists.";
        state.classList.add("error");
      }
      video.classList.add("media-loading");
      video.dataset.failed = "true";
      wrap.classList.remove("media-ready");
      setReplayStatus("Video load failed", true);
    });
    host.appendChild(wrap);
  });
}

function playVizVideos() {
  if (
    !replayActive
    || !vizState.previewReady
    || vizState.previewGeneration !== previewRequestGeneration
  ) return;
  const duration = refreshReplayDuration();
  if (duration <= 0) return;
  if (vizState.elapsed >= duration - 0.001) setReplayElapsed(0);
  vizState.autoplay = false;
  vizState.playing = true;
  vizState.clockBaseElapsed = vizState.elapsed;
  vizState.clockStartedAt = performance.now();
  stopReplayClock();
  syncReplayMedia(vizState.elapsed, true);
  vizState.clockFrame = requestAnimationFrame(tickReplayClock);
  syncReplayPlayState();
}

function pauseVizVideos() {
  if (vizState.playing) {
    vizState.elapsed = Math.min(
      refreshReplayDuration(),
      vizState.clockBaseElapsed + (performance.now() - vizState.clockStartedAt) / 1000,
    );
  }
  vizState.playing = false;
  vizState.autoplay = false;
  stopReplayClock();
  vizVideos().forEach((video) => video.pause());
  updateReplayBar(vizState.elapsed, refreshReplayDuration());
  syncReplayPlayState();
}

function toggleVizPlay() {
  if (!vizState.playing) playVizVideos();
  else pauseVizVideos();
}

function fillReplayChart(chart, times, series, prefix, overlay = [], overlayStart = 0) {
  if (!chart) return;
  // Replay charts own `chart.data.datasets` directly; drop the live-mode caches.
  chart.$timeAxis = false;
  chart.$live = [];
  chart.$predictions = [];
  chart.$predictionGaps = [];
  chart.$showNowLine = false;
  chart.$nowTime = Number.NaN;
  chart.$timeBasis = "start";
  chart.$historyStart = 0;
  chart.$tooltipToleranceS = CHART_TOOLTIP_TOLERANCE_S;
  chart.$scaleSeconds = chartScaleSeconds;
  const frameTimeSet = new Set(
    times.map(Number).filter((value) => Number.isFinite(value)),
  );
  (overlay || []).forEach((point) => {
    const value = overlayStart + (Number(point.x) || 0);
    if (Number.isFinite(value)) frameTimeSet.add(value);
  });
  const frameTimes = [...frameTimeSet].sort((left, right) => left - right);
  chart.$frameTimes = frameTimes.length
    ? frameTimes
    : (chart === actionChart && stateChart ? stateChart.$frameTimes || [] : []);
  const keys = Object.keys(series).filter((key) => key.startsWith(prefix));
  if (chart === actionChart) {
    actionLegendNames = keys.map((key) => key.slice(prefix.length));
    renderActionLegend(actionLegendNames);
  }
  const overlaySeries = new Map();
  (overlay || []).forEach((point) => {
    const x = overlayStart + (Number(point.x) || 0);
    Object.entries(point.joints || {}).forEach(([name, value]) => {
      const y = Number(value);
      if (!Number.isFinite(y)) return;
      if (!overlaySeries.has(name)) overlaySeries.set(name, []);
      overlaySeries.get(name).push({ x, y });
    });
  });
  const seriesIndex = new Map(keys.map((key, index) => [key.slice(prefix.length), index]));
  overlaySeries.forEach((_points, name) => {
    if (!seriesIndex.has(name)) seriesIndex.set(name, seriesIndex.size);
  });
  chart.data.labels = [];
  chart.data.datasets = [];
  keys.forEach((key) => {
    const name = key.slice(prefix.length);
    chart.data.datasets.push({
      label: name,
      data: times.map((time, index) => ({ x: Number(time), y: Number(series[key][index]) })),
      borderColor: PAL[seriesIndex.get(name) % PAL.length],
      borderWidth: 1,
      borderCapStyle: "butt",
      borderJoinStyle: "bevel",
      pointRadius: 0,
      pointHoverRadius: 0,
      tension: 0,
      clip: 0,
    });
  });
  overlaySeries.forEach((points, name) => {
    chart.data.datasets.push({
      label: `${name} · pred`,
      data: points,
      borderColor: PAL[seriesIndex.get(name) % PAL.length],
      borderWidth: 1,
      borderCapStyle: "butt",
      borderJoinStyle: "bevel",
      borderDash: [4, 3],
      pointRadius: 0,
      pointHoverRadius: 0,
      tension: 0,
      clip: 0,
      $prediction: true,
    });
  });
  const timesEnd = times.length ? Math.max(0, Number(times[times.length - 1]) || 0) : 0;
  const overlayEnd = (overlay || []).reduce((max, point) => Math.max(max, overlayStart + (Number(point.x) || 0)), 0);
  const seriesEnd = Math.max(timesEnd, overlayEnd);
  const domainMax = Math.max(1, vizState.duration || 0, seriesEnd);
  chart.$replaySeriesEnd = seriesEnd;
  chart.$replayCursorTime = Math.min(domainMax, Math.max(0, vizState.elapsed || 0));
  chart.$historyEnd = domainMax;
  chart.options.scales.x = {
    type: "linear",
    display: true,
    min: 0,
    max: domainMax,
    ticks: {
      display: false,
      font: { size: 9, family: "IBM Plex Mono" },
    },
    grid: { display: false },
    border: { color: "#2c3148" },
  };
  chart.options.plugins.tooltip = { enabled: false };
  chart.options.plugins.legend = { display: false };
  applyChartLegendVisibilityToChart(chart);
  hideChartHoverTooltip(chart);
  chart.update("none");
  positionReplayChartCursor(chart);
  updateLegendTimeInfo();
}

function resetReplayCharts() {
  [stateChart, actionChart].forEach((chart) => {
    if (!chart) return;
    chart.$timeAxis = true;
    chart.$live = [];
    chart.$predictions = [];
    chart.$predictionGaps = [];
    chart.$modeMarkers = [];
    chart.data.labels = [];
    chart.data.datasets = [];
    chart.options.plugins.legend.display = false;
    chart.$replayCursorTime = Number.NaN;
    chart.$showNowLine = false;
    chart.$nowTime = Number.NaN;
    chart.$timeBasis = chartTimeBasis;
    chart.$historyStart = Number.NaN;
    chart.$historyEnd = Number.NaN;
    chart.$tooltipToleranceS = CHART_TOOLTIP_TOLERANCE_S;
    chart.$scaleSeconds = chartScaleSeconds;
    chart.$frameTimes = [];
    chart.$replaySeriesEnd = 0;
    chart.options.scales.x = liveTimeAxis(liveTimestamp(last));
    chart.options.plugins.tooltip = { enabled: false };
    hideChartHoverTooltip(chart);
    chart.update("none");
    positionReplayChartCursor(chart);
  });
  liveModeMarkers = [];
  lastDisplayMode = "";
  updateLegendTimeInfo();
}

function renderVizChart(data) {
  const series = data.series || {};
  const times = (data.t || []).map(Number);
  vizState.series = series;
  vizState.times = times;
  fillReplayChart(stateChart, times, series, "obs.");
  fillReplayChart(actionChart, times, series, "act.", chunkOverlayPoints(), chunkOverlayStart());
}

function renderVizChartsFromState() {
  const times = vizState.times || [];
  const series = vizState.series || {};
  fillReplayChart(stateChart, times, series, "obs.");
  fillReplayChart(actionChart, times, series, "act.", chunkOverlayPoints(), chunkOverlayStart());
}

function chunkOverlayPoints() {
  const chunk = vizState.chunk;
  if (!chunk || !Array.isArray(chunk.actions)) return [];
  const fps = Number(chunk.fps) || 30;
  return chunk.actions.map((action, index) => ({
    x: Number(action.t_s) || (index + 1) / fps,
    joints: action.joints || {},
  }));
}

function chunkOverlayStart() {
  if (!vizState.chunk) return 0;
  return snapshotActive ? 0 : Number(vizState.chunk.start) || 0;
}

function armReplayTrack(data) {
  const series = data.series || {};
  const track = {};
  jointNames().forEach((name) => {
    const values = series[`act.${name}`];
    if (Array.isArray(values) && values.length) {
      track[name] = values.map((value) => value == null ? Number.NaN : Number(value));
    }
  });
  return { times: (data.t || []).map((value) => value == null ? Number.NaN : Number(value)), track };
}

const ARM_REPLAY_HZ = 20;

function stopArmReplayLoop() {
  if (!replayRobotTimer) return;
  clearInterval(replayRobotTimer);
  replayRobotTimer = null;
  replayRobotInFlight = false;
}

function sampleArmJoints(elapsed) {
  return sampleJointSeries(vizState.arm.times, vizState.arm.track, elapsed);
}

function sampleJointSeries(times, track, elapsed) {
  const names = Object.keys(track);
  if (!times.length || !names.length || !times.every(Number.isFinite)) return null;
  const lastIndex = times.length - 1;
  const at = Math.min(Math.max(elapsed, times[0]), times[lastIndex]);
  let low = 0;
  let high = lastIndex;
  while (high - low > 1) {
    const mid = Math.floor((low + high) / 2);
    if (times[mid] <= at) low = mid;
    else high = mid;
  }
  const span = times[high] - times[low];
  const weight = span > 0 ? (at - times[low]) / span : 0;
  const joints = {};
  for (const name of names) {
    const values = track[name];
    const a = values[Math.min(low, values.length - 1)];
    const b = values[Math.min(high, values.length - 1)];
    if (!Number.isFinite(a) || !Number.isFinite(b)) return null;
    joints[name] = a + (b - a) * weight;
  }
  return Object.keys(joints).length ? joints : null;
}

function pushArmReplayFrame() {
  if (
    !armReplay
    || !replayActive
    || replayRobotInFlight
    || !vizState.previewReady
    || vizState.previewGeneration !== previewRequestGeneration
  ) return;
  if (!vizState.playing) return;
  const robot = (last && last.robot) || {};
  const displayMode = (last && last.display_mode) || "";
  const rawMode = (last && last.mode) || "";
  const pending = last && last.task && last.task.pending;
  if (!robot.connected || pending || !["idle", "hold"].includes(displayMode) || (rawMode && rawMode !== "idle")) return;
  const joints = sampleArmJoints(vizState.elapsed);
  if (!joints || Object.values(joints).some((value) => !Number.isFinite(value))) return;
  replayRobotInFlight = true;
  api("/api/joints", { joints, duration_s: 0, live: true })
    .catch(toastError)
    .finally(() => { replayRobotInFlight = false; });
}

function toggleArmReplay() {
  if (armReplay) {
    armReplay = false;
    stopArmReplayLoop();
    syncArmToggle();
    return;
  }
  if (!vizState.previewReady || vizState.previewGeneration !== previewRequestGeneration) return;
  const robot = (last && last.robot) || {};
  if (!robot.connected) {
    localLog("connect the arm before replaying actions", "error");
    return;
  }
  const displayMode = (last && last.display_mode) || "";
  const pending = last && last.task && last.task.pending;
  if (pending || !["idle", "hold"].includes(displayMode)) {
    localLog("arm replay is available only while the backend is idle or holding", "error");
    return;
  }
  if (!Object.keys(vizState.arm.track).length) {
    localLog("this episode has no action series", "error");
    return;
  }
  armReplay = true;
  if (!replayRobotTimer) replayRobotTimer = setInterval(pushArmReplayFrame, 1000 / ARM_REPLAY_HZ);
  syncArmToggle();
}

function finitePositive(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : 0;
}

function previewDuration(data, cameras) {
  let duration = Math.max(finitePositive(data.duration_s ?? data.episode_duration_s), seriesDuration());
  (cameras || []).forEach((camera) => {
    const offset = finitePositive(camera.timeline_offset_s);
    const declared = finitePositive(camera.duration_s ?? camera.media_duration_s);
    const start = Number(camera.start || 0);
    const end = Number(camera.end);
    const clip = declared || (Number.isFinite(end) && end > start ? end - start : 0);
    duration = Math.max(duration, offset + clip);
  });
  return duration;
}

function previewMetadataLabel(data) {
  const parts = [];
  const requestedVideoFps = finitePositive(data.requested_video_fps);
  const encodedVideoFps = finitePositive(data.encoded_video_fps || data.effective_encoding_fps);
  const requestedActionFps = finitePositive(data.requested_action_fps);
  const encodedFrames = Number(data.encoded_video_frames ?? data.encoded_frame_count ?? data.encoded_frames);
  if (requestedActionFps) parts.push(`action requested ${requestedActionFps} Hz`);
  if (requestedVideoFps) parts.push(`video requested ${requestedVideoFps} Hz`);
  if (encodedVideoFps) parts.push(`encoded ${encodedVideoFps} Hz`);
  if (Number.isFinite(encodedFrames) && encodedFrames >= 0) parts.push(`${encodedFrames} encoded frames`);
  return parts.join(" · ");
}

async function loadPreview(kind, id, episode, autoplay = false) {
  const generation = ++previewRequestGeneration;
  stopReplayClock();
  vizVideos().forEach((video) => video.pause());
  armReplay = false;
  stopArmReplayLoop();
  vizState.playing = false;
  vizState.autoplay = false;
  vizState.previewReady = false;
  vizState.previewGeneration = generation;
  vizState.duration = 0;
  vizState.elapsed = 0;
  vizState.times = [];
  vizState.arm = { times: [], track: {} };
  vizState.previewMeta = "";
  vizState.task = "";
  clearVizChunk();
  enterReplay();
  resetReplayCharts();
  syncArmToggle();
  syncReplayPlayState();
  syncReplayAvailability();
  if ($("replay-title")) $("replay-title").textContent = `${id} · ep ${episode}`;
  renderReplayMessage("Loading episode preview…");
  let res;
  try {
    res = await fetch(`${BASE}/api/preview?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}&episode=${episode}`);
  } catch (err) {
    if (generation === previewRequestGeneration) renderReplayMessage(`Could not load preview: ${err.message || err}`, true);
    throw err;
  }
  const data = await res.json().catch(() => ({}));
  if (generation !== previewRequestGeneration) return false;
  if (!res.ok) {
    const message = data.detail || "preview failed";
    renderReplayMessage(`Could not load preview: ${message}`, true);
    throw new Error(message);
  }
  vizState.kind = kind;
  vizState.id = id;
  vizState.episode = episode;
  vizState.episodes = Number(data.episodes || 0);
  vizState.title = data.title || id;
  vizState.times = (data.t || []).map(Number);
  const cameras = (data.cameras || []).filter((cam) => cam.name !== "merged");
  vizState.duration = previewDuration(data, cameras);
  vizState.elapsed = 0;
  vizState.previewMeta = previewMetadataLabel(data);
  vizState.arm = armReplayTrack(data);
  vizState.task = data.task || "";
  vizState.autoplay = !!autoplay;
  vizState.previewReady = true;
  vizState.previewGeneration = generation;
  syncReplayAvailability();
  renderEpisodes();
  const label = vizState.title;
  const episodeName = data.episode_name ? ` · ${data.episode_name}` : "";
  if ($("viz-title")) {
    $("viz-title").textContent = `${label}\nep ${episode}${episodeName}`;
    $("viz-title").title = `${label} · ep ${episode}${episodeName}`;
  }
  if ($("replay-title")) {
    $("replay-title").textContent = `${label} · ep ${episode}${episodeName}`;
    $("replay-title").title = `${label} · ep ${episode}${episodeName}`;
  }
  if ($("viz-ep")) $("viz-ep").value = String(episode);
  const lastEp = Math.max(0, vizState.episodes - 1);
  if ($("viz-ep-max")) $("viz-ep-max").textContent = `/ ${lastEp}`;
  updateReplayBar(0, vizState.duration);
  renderVizChart(data);
  renderVizCams(cameras, generation);
  renderDebugPanel();
  syncSnapshotButton();
  if (autoplay) playVizVideos();
  if (autoplay && !cameras.length) localLog("episode has no video; replaying series timeline only");
  return true;
}

function stepPreview(delta) {
  if (!vizState.id) return;
  const lastEp = Math.max(0, vizState.episodes - 1);
  const next = Math.min(lastEp, Math.max(0, vizState.episode + delta));
  const wasPlaying = vizState.playing;
  loadPreview(vizState.kind, vizState.id, next, wasPlaying).catch(toastError);
}

async function selectVideo(id) {
  if (replayActive) leaveReplay();
  previewRequestGeneration += 1;
  const row = videosCache.find((item) => item.id === id);
  await loadEpisodeList("video", id, (row && (row.name || row.id)) || id).catch(toastError);
}

async function selectHfDataset(ds) {
  selectedDatasetId = ds.repo_id || ds.id;
  if (replayActive) leaveReplay();
  previewRequestGeneration += 1;
  persistUi();
  await loadEpisodeList("dataset", selectedDatasetId, selectedDatasetId).catch(toastError);
}

async function playEpisode(kind, id, episode) {
  try {
    if (!episodeSource || episodeSource.kind !== kind || episodeSource.id !== id) {
      const loaded = await loadEpisodeList(kind, id);
      if (!loaded) return;
    }
    await loadPreview(kind, id, episode, true);
  } catch (err) {
    toastError(err);
  }
}

function restoreEpisodeSelection() {
  if (episodeSelectionClosed || episodeSource || vizState.id) return;
  const dataset = datasetsCache.find((row) => (row.repo_id || row.id) === selectedDatasetId);
  if (dataset) loadEpisodeList("dataset", dataset.repo_id || dataset.id).catch(() => {});
}

if ($("viz-prev")) bind("viz-prev", () => stepPreview(-1));
if ($("viz-next")) bind("viz-next", () => stepPreview(1));
if ($("viz-play")) bind("viz-play", toggleVizPlay);
if ($("viz-restart")) bind("viz-restart", () => seekViz(0));
if ($("viz-arm")) bind("viz-arm", toggleArmReplay);
if ($("viz-exit")) bind("viz-exit", () => exitReplay());
if ($("viz-ep")) {
  $("viz-ep").addEventListener("change", () => {
    if (!vizState.id) return;
    const lastEp = Math.max(0, vizState.episodes - 1);
    const ep = Math.min(lastEp, Math.max(0, Number($("viz-ep").value) || 0));
    const wasPlaying = vizState.playing;
    loadPreview(vizState.kind, vizState.id, ep, wasPlaying).catch(toastError);
  });
}
if ($("btn-ep-close")) bind("btn-ep-close", closeEpisodeSelection);
if ($("ep-description")) {
  $("ep-description").addEventListener("click", openEpisodeDescriptionEditor);
  $("ep-description").addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openEpisodeDescriptionEditor();
    }
  });
}
bindReplaySeek("replay-seek");
bindReplayChartSeek(stateChart, "chart-state");
bindReplayChartSeek(actionChart, "chart-action");
bindReplayChartWheel(actionChart);

async function moveEpisode(videoId, from, delta) {
  if (episodeMutationPending) return;
  const order = episodeRows.map((e) => e.index);
  const to = from + delta;
  if (to < 0 || to >= order.length) return;
  const tmp = order[from];
  order[from] = order[to];
  order[to] = tmp;
  await reorderEpisodes(videoId, order);
}

async function deleteEpisode(videoId, index) {
  if (episodeMutationPending) return;
  if (!window.confirm(`Delete episode ${index}?`)) return;
  const replayingThisVideo = replayActive && vizState.kind === "video" && vizState.id === videoId;
  const previousEpisode = replayingThisVideo ? vizState.episode : null;
  const deletingActive = replayingThisVideo && previousEpisode === index;
  const wasPlaying = replayingThisVideo && vizState.playing;
  if (deletingActive) leaveReplay();
  else if (replayingThisVideo) {
    pauseVizVideos();
    vizState.episode = -1;
    renderEpisodes();
  }
  try {
    setEpisodeMutationPending(true);
    const result = await api(`/api/videos/${encodeURIComponent(videoId)}/episodes/${index}`, undefined, "DELETE");
    await refreshLibrarySection("videos");
    await loadEpisodeList("video", videoId);
    if (replayingThisVideo && !deletingActive) {
      const mapped = result.episode_index_map && result.episode_index_map[String(previousEpisode)];
      if (mapped == null) leaveReplay();
      else await loadPreview("video", videoId, Number(mapped), wasPlaying);
    }
  } catch (err) {
    if (replayingThisVideo && !deletingActive && previousEpisode != null) {
      vizState.episode = previousEpisode;
      renderEpisodes();
      if (wasPlaying) playVizVideos();
    }
    toastError(err);
  }
  finally { setEpisodeMutationPending(false); }
}

function renderModels() {
  const ol = $("md-list");
  if (!ol) return;
  ol.innerHTML = "";
  renderPolicyPickerMenu();
  const select = $("dbg-policy");
  const selected = select ? select.value : "";
  if (select) select.innerHTML = `<option value="">—</option>`;
  if (!modelsCache.length) {
    const state = libraryState.models;
    const message = state.loading ? "Loading policies…" : state.error ? `Could not load policies: ${state.error}` : "No local policy found";
    ol.innerHTML = `<li class="library-message${state.error ? " error" : ""}">${message}</li>`;
    return;
  }
  const visibleModels = filterLibraryRows("models", modelsCache);
  const visibleIds = new Set(visibleModels.map((model) => model.id));
  if (!visibleModels.length) {
    ol.innerHTML = `<li class="library-message">${libraryFilterMessage("model")}</li>`;
  }
  modelsCache.forEach((m) => {
    if (select) {
      const source = m.source === "hub" ? "hf cache" : m.source || "local";
      const parts = [m.name, m.policy_type, source].filter(Boolean);
      const option = document.createElement("option");
      option.value = m.path;
      option.textContent = parts.join("  ·  ");
      option.disabled = !m.path;
      select.appendChild(option);
    }
    if (!visibleIds.has(m.id)) return;
    const li = document.createElement("li");
    const source = m.source === "hub" ? "hf cache" : m.source || "local";
    const parts = [m.name, m.policy_type, source].filter(Boolean);
    const label = parts.join("  ·  ");
    const head = document.createElement("div");
    head.className = "snap-head";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "lib-row-button";
    button.textContent = label;
    li.title = m.path || m.name;
    button.addEventListener("click", () => {
      if (!m.path) {
        localLog(`model ${m.name} has no weights yet — run Update first`, "error");
        return;
      }
      if ($("pol-path")) $("pol-path").value = m.path;
      if ($("dbg-path")) $("dbg-path").value = m.path;
      persistUi();
    });
    head.appendChild(button);
    if (m.managed) {
      const tools = document.createElement("span");
      tools.className = "snap-tools";
      const edit = makeSnapshotIconButton("md-edit", `Edit model ${m.id}`, LIBRARY_EDIT_ICON);
      edit.addEventListener("click", (event) => {
        event.stopPropagation();
        openModelEditor(li, m);
      });
      const update = makeSnapshotIconButton("md-update", `Update weights for ${m.id}`, MODEL_REFRESH_ICON);
      update.addEventListener("click", (event) => {
        event.stopPropagation();
        updateModel(m.id);
      });
      const remove = makeSnapshotIconButton("md-del", `Remove model ${m.id}`, SNAPSHOT_ICONS.delete);
      remove.addEventListener("click", (event) => {
        event.stopPropagation();
        deleteModel(m.id);
      });
      tools.append(edit, update, remove);
      head.appendChild(tools);
    }
    li.appendChild(head);
    if (m.managed) {
      const meta = document.createElement("p");
      meta.className = `model-meta${m.missing ? " warn" : ""}`;
      const remote = m.remote || m.path || "no remote address";
      meta.textContent = m.missing ? `${remote} · weights missing` : remote;
      meta.title = m.path || remote;
      li.appendChild(meta);
      appendLibraryNote(li, "model", m.id, m);
    }
    ol.appendChild(li);
  });
  if (select && selected) select.value = selected;
}

let policyPickerActive = -1;

function policyPickerModels(query = "") {
  const text = String(query || "").trim().toLowerCase();
  return modelsCache.filter((model) => {
    if (!model.path) return false;
    if (!text) return true;
    return [model.name, model.path, model.policy_type, model.source]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
      .includes(text);
  });
}

function positionPolicyPickerMenu() {
  const input = $("pol-path");
  const menu = $("pol-path-menu");
  if (!input || !menu || menu.classList.contains("hidden")) return;
  const rect = input.getBoundingClientRect();
  const width = Math.min(rect.width, window.innerWidth - 16);
  const height = Math.min(menu.scrollHeight || 240, 240);
  const left = Math.min(Math.max(8, rect.left), Math.max(8, window.innerWidth - width - 8));
  let top = rect.bottom + 4;
  if (top + height > window.innerHeight - 8) top = Math.max(8, rect.top - height - 4);
  Object.assign(menu.style, {
    left: `${left}px`,
    top: `${top}px`,
    width: `${width}px`,
    maxHeight: "240px",
  });
}

function closePolicyPicker() {
  const input = $("pol-path");
  const menu = $("pol-path-menu");
  if (!menu || menu.classList.contains("hidden")) return;
  menu.classList.add("hidden");
  policyPickerActive = -1;
  if (input) input.setAttribute("aria-expanded", "false");
}

function setPolicyPickerActive(index) {
  const menu = $("pol-path-menu");
  if (!menu) return;
  const rows = [...menu.querySelectorAll(".policy-picker-option:not(:disabled)")];
  if (!rows.length) {
    policyPickerActive = -1;
    return;
  }
  policyPickerActive = Math.max(0, Math.min(index, rows.length - 1));
  rows.forEach((row, rowIndex) => {
    const active = rowIndex === policyPickerActive;
    row.classList.toggle("active", active);
    row.setAttribute("aria-selected", String(active));
    if (active) row.scrollIntoView({ block: "nearest" });
  });
}

function choosePolicyPickerModel(model) {
  const input = $("pol-path");
  if (!input || !model || !model.path) return;
  input.value = model.path;
  closePolicyPicker();
  persistUi();
}

function renderPolicyPickerMenu() {
  const menu = $("pol-path-menu");
  const input = $("pol-path");
  if (!menu || !input) return;
  const selectedPath = input.value.trim();
  const query = modelsCache.some((model) => model.path === selectedPath) ? "" : selectedPath;
  const rows = policyPickerModels(query);
  menu.innerHTML = "";
  policyPickerActive = -1;
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "policy-picker-empty";
    empty.textContent = "No cached policy matches";
    menu.appendChild(empty);
  } else {
    rows.forEach((model, index) => {
      const option = document.createElement("button");
      option.type = "button";
      option.className = "policy-picker-option";
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", "false");
      option.dataset.index = String(index);
      const title = document.createElement("span");
      title.className = "policy-picker-title";
      title.textContent = model.name || model.path;
      const detail = document.createElement("span");
      detail.className = "policy-picker-detail";
      const meta = document.createElement("span");
      meta.className = "policy-picker-meta";
      const source = model.source === "hub" ? "hf cache" : model.source || "local";
      meta.textContent = [model.policy_type, source].filter(Boolean).join(" · ");
      const path = document.createElement("span");
      path.className = "policy-picker-path";
      path.textContent = model.path;
      detail.append(meta, path);
      option.append(title, detail);
      option.title = `${model.name || model.path}\n${model.path}`;
      option.addEventListener("click", () => choosePolicyPickerModel(model));
      option.addEventListener("mousemove", () => setPolicyPickerActive(index));
      menu.appendChild(option);
    });
  }
  positionPolicyPickerMenu();
}

function openPolicyPicker() {
  const input = $("pol-path");
  const menu = $("pol-path-menu");
  if (!input || !menu) return;
  menu.classList.remove("hidden");
  input.setAttribute("aria-expanded", "true");
  renderPolicyPickerMenu();
}

function togglePolicyPicker() {
  const menu = $("pol-path-menu");
  if (menu && !menu.classList.contains("hidden")) closePolicyPicker();
  else openPolicyPicker();
}

const MODEL_REFRESH_ICON = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.3-5.6"/><path d="M20 4v4h-4"/></svg>`;

function modelLibraryStatus(message, isError = false) {
  const status = $("md-status");
  if (!status) return;
  status.className = `library-status${isError ? " error" : ""}`;
  status.textContent = message;
}

function modelHubStatus(message, isError = false) {
  const status = $("md-hub-status");
  if (!status) return;
  status.className = `library-status${isError ? " error" : ""}`;
  status.textContent = message;
}

function openModelEditor(li, model) {
  const existing = li.querySelector(".model-editor");
  if (existing) {
    existing.remove();
    return;
  }
  const editor = document.createElement("div");
  editor.className = "model-editor";
  const name = document.createElement("input");
  name.type = "text";
  name.value = model.name || "";
  name.placeholder = "display name";
  name.setAttribute("aria-label", "Model name");
  const remote = document.createElement("input");
  remote.type = "text";
  remote.value = model.remote || "";
  remote.placeholder = "org/name, URL, or local path";
  remote.setAttribute("aria-label", "Model remote address");
  const revision = document.createElement("input");
  revision.type = "text";
  revision.value = model.revision || "";
  revision.placeholder = "revision / branch (optional)";
  revision.setAttribute("aria-label", "Model revision");
  const actions = document.createElement("div");
  actions.className = "row-actions";
  const save = document.createElement("button");
  save.type = "button";
  save.textContent = "Save";
  const update = document.createElement("button");
  update.type = "button";
  update.className = "ghost";
  update.textContent = "Update";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "ghost";
  cancel.textContent = "Cancel";
  save.addEventListener("click", async () => {
    try {
      const saved = await api(`/api/models/${encodeURIComponent(model.id)}`, {
        name: name.value,
        remote: remote.value,
        revision: revision.value,
      }, "PUT");
      modelLibraryStatus(saved.warning ? `Saved with warning: ${saved.warning}` : `Saved ${saved.name}`, !!saved.warning);
      await refreshLibrarySection("models");
    } catch (err) {
      modelLibraryStatus(err.message || String(err), true);
    }
  });
  update.addEventListener("click", () => updateModel(model.id));
  cancel.addEventListener("click", () => editor.remove());
  actions.append(save, update, cancel);
  editor.append(name, remote, revision, actions);
  li.appendChild(editor);
}

async function addModelFromRemote(remote, name = "", download = true) {
  const address = String(remote || "").trim();
  if (!address) throw new Error("enter a model address first");
  modelLibraryStatus(`adding ${address}…`);
  try {
    const saved = await api("/api/models", { remote: address, name, download });
    modelLibraryStatus(saved.warning ? `Added ${saved.name} — ${saved.warning}` : `Added ${saved.name}`, !!saved.warning);
    localLog(`model added: ${saved.name} (${saved.remote || saved.path})`);
    await refreshLibrarySection("models");
    return saved;
  } catch (err) {
    modelLibraryStatus(err.message || String(err), true);
    throw err;
  }
}

async function updateModel(modelId) {
  modelLibraryStatus(`updating ${modelId}…`);
  try {
    const saved = await api(`/api/models/${encodeURIComponent(modelId)}/update`);
    modelLibraryStatus(`Updated ${saved.name}${saved.warning ? ` — ${saved.warning}` : ""}`, !!saved.warning);
    await refreshLibrarySection("models");
  } catch (err) {
    modelLibraryStatus(err.message || String(err), true);
  }
}

async function deleteModel(modelId) {
  if (!window.confirm(`Remove model ${modelId} from the library?`)) return;
  try {
    await api(`/api/models/${encodeURIComponent(modelId)}`, undefined, "DELETE");
    modelLibraryStatus(`Removed ${modelId}`);
    await refreshLibrarySection("models");
  } catch (err) {
    modelLibraryStatus(err.message || String(err), true);
  }
}

async function searchModelHub() {
  const query = $("md-search") ? $("md-search").value.trim() : "";
  const list = $("md-hub-list");
  if (!list) return;
  list.innerHTML = "";
  if (!query) {
    modelHubStatus("Type a query to search the Hugging Face Hub", true);
    return;
  }
  modelHubStatus(`searching “${query}”…`);
  try {
    const rows = await fetchJson(`/api/models/search?q=${encodeURIComponent(query)}`);
    if (!Array.isArray(rows) || !rows.length) {
      modelHubStatus("No matching model");
      return;
    }
    modelHubStatus(`${rows.length} result${rows.length === 1 ? "" : "s"}`);
    rows.forEach((row) => {
      const li = document.createElement("li");
      li.className = "hub-row";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "lib-row-button";
      button.textContent = `${row.repo_id}  ·  ${row.downloads} downloads`;
      button.addEventListener("click", () => {
        if ($("md-remote")) $("md-remote").value = row.repo_id;
        if ($("md-name")) $("md-name").value = row.repo_id;
        addModelFromRemote(row.repo_id, row.repo_id).catch(toastError);
      });
      li.appendChild(button);
      list.appendChild(li);
    });
  } catch (err) {
    modelHubStatus(err.message || String(err), true);
  }
}

function modelAddressFromManifest(payload, fallback = "") {
  if (typeof payload === "string") return payload.trim() || fallback;
  if (!payload || typeof payload !== "object") return fallback;
  const keys = ["remote", "repo_id", "model_id", "path", "_name_or_path", "name_or_path", "id", "name"];
  for (const key of keys) {
    const value = String(payload[key] || "").trim();
    if (value) return value;
  }
  return fallback;
}

async function addModelFromDroppedFile(file) {
  if (!file) return;
  const nativePath = String(file.path || "").trim();
  if (nativePath) {
    await addModelFromRemote(nativePath, file.name || nativePath);
    return;
  }
  const text = await file.text();
  let address = "";
  try {
    address = modelAddressFromManifest(JSON.parse(text));
  } catch {
    address = text.trim();
  }
  if (!address) {
    throw new Error("the dropped file has no remote, repo_id, or path field");
  }
  if ($("md-remote")) $("md-remote").value = address;
  const fallbackName = String(file.name || "").replace(/\.(json|txt)$/i, "");
  await addModelFromRemote(address, fallbackName);
}

function bindModelDropZone() {
  const zone = $("md-drop");
  if (!zone) return;
  ["dragenter", "dragover"].forEach((name) => {
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.add("drop-active");
    });
  });
  ["dragleave", "drop"].forEach((name) => {
    zone.addEventListener(name, () => zone.classList.remove("drop-active"));
  });
  zone.addEventListener("drop", async (event) => {
    event.preventDefault();
    const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
    if (!file) return;
    try {
      await addModelFromDroppedFile(file);
    } catch (err) {
      modelLibraryStatus(`drop failed: ${err.message || err}`, true);
    }
  });
  zone.addEventListener("keydown", (event) => {
    if (event.target !== zone) return;
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    const input = $("md-file-input");
    if (input) input.click();
  });
}

bind("btn-md-search", searchModelHub);
bind("btn-md-file", () => {
  const input = $("md-file-input");
  if (input) input.click();
});
bind("btn-md-add", () => {
  const remote = $("md-remote") ? $("md-remote").value : "";
  const name = $("md-name") ? $("md-name").value : "";
  addModelFromRemote(remote, name).catch(toastError);
});
if ($("md-search")) {
  $("md-search").addEventListener("keydown", (event) => {
    if (event.key === "Enter") searchModelHub();
  });
}
if ($("md-file-input")) {
  $("md-file-input").addEventListener("change", (event) => {
    const file = event.target.files && event.target.files[0];
    addModelFromDroppedFile(file).catch((err) => modelLibraryStatus(err.message || String(err), true));
    event.target.value = "";
  });
}
bindModelDropZone();

const LIBRARY_CONFIG = {
  videos: { path: "/api/videos", group: "lib-videos", list: "vid-list", status: "vid-status", button: "btn-vid-refresh", render: renderVideos },
  datasets: { path: "/api/datasets", group: "lib-datasets", list: "ds-list", status: "ds-status", button: "btn-ds-refresh", render: renderDatasets },
  snapshots: { path: "/api/snapshots", group: "lib-snapshots", list: "snap-list", status: "snap-status", button: "btn-snap-refresh", render: renderSnapshots },
  models: { path: "/api/models", group: "lib-models", list: "md-list", status: "md-status", button: "btn-md-refresh", render: renderModels },
};

function librarySearchPlaceholder(kind) {
  return kind === "models"
    ? "Filter local models by title or note"
    : "Search title or note";
}

function updateLibrarySearchUi() {
  const input = $("lib-search");
  const clear = $("btn-lib-search-clear");
  if (!input) return;
  input.value = librarySearch[activeLibrarySearchKind] || "";
  input.placeholder = librarySearchPlaceholder(activeLibrarySearchKind);
  if (clear) clear.hidden = !input.value;
}

function selectLibrarySearchKind(kind) {
  activeLibrarySearchKind = kind;
  updateLibrarySearchUi();
}

function bindLibrarySearchInput() {
  const input = $("lib-search");
  if (!input) return;
  input.addEventListener("input", () => {
    librarySearch[activeLibrarySearchKind] = input.value;
    saveJsonStorage(LIBRARY_SEARCH_KEY, librarySearch);
    updateLibrarySearchUi();
    LIBRARY_CONFIG[activeLibrarySearchKind].render();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !input.value) return;
    event.preventDefault();
    input.value = "";
    librarySearch[activeLibrarySearchKind] = "";
    saveJsonStorage(LIBRARY_SEARCH_KEY, librarySearch);
    updateLibrarySearchUi();
    LIBRARY_CONFIG[activeLibrarySearchKind].render();
  });
  bind("btn-lib-search-clear", () => {
    input.value = "";
    librarySearch[activeLibrarySearchKind] = "";
    saveJsonStorage(LIBRARY_SEARCH_KEY, librarySearch);
    updateLibrarySearchUi();
    LIBRARY_CONFIG[activeLibrarySearchKind].render();
  });
  updateLibrarySearchUi();
}

bindLibrarySearchInput();

function setLibrarySectionState(kind) {
  const config = LIBRARY_CONFIG[kind];
  const state = libraryState[kind];
  const list = $(config.list);
  const group = $(config.group);
  const status = $(config.status);
  const button = $(config.button);
  if (list) list.setAttribute("aria-busy", String(state.loading));
  if (group) group.setAttribute("aria-busy", String(state.loading));
  if (button) button.disabled = state.loading;
  if (status) {
    status.className = `library-status${state.error ? " error" : ""}`;
    status.textContent = state.loading ? "Refreshing…" : state.error ? `Refresh failed: ${state.error}` : "";
  }
  config.render();
}

async function refreshLibrarySection(kind) {
  const config = LIBRARY_CONFIG[kind];
  const state = libraryState[kind];
  const generation = ++state.generation;
  state.loading = true;
  state.error = "";
  setLibrarySectionState(kind);
  try {
    const rows = await fetchJson(config.path);
    if (!Array.isArray(rows)) throw new Error("invalid response");
    if (generation !== state.generation) return;
    if (kind === "videos") videosCache = rows;
    else if (kind === "datasets") datasetsCache = rows;
    else if (kind === "snapshots") snapshotsCache = rows;
    else modelsCache = rows;
  } catch (err) {
    if (generation === state.generation) state.error = err.message || String(err);
  } finally {
    if (generation === state.generation) {
      state.loading = false;
      setLibrarySectionState(kind);
      if (kind === "videos" || kind === "datasets") {
        syncEpisodeSource(kind);
        restoreEpisodeSelection();
        updateResumeTargetUi();
      }
    }
  }
}

async function refreshLibrary() {
  await Promise.all(Object.keys(LIBRARY_CONFIG).map(refreshLibrarySection));
}

function syncEpisodeSource(refreshedKind = "") {
  if (!episodeSource) return;
  if (refreshedKind && episodeSource.kind !== refreshedKind.replace(/s$/, "")) return;
  const signature = episodeSignature();
  const sourceExists = episodeSource.kind === "video"
    ? videosCache.some((row) => row.id === episodeSource.id)
    : datasetsCache.some((row) => (row.repo_id || row.id) === episodeSource.id);
  if (!sourceExists) {
    episodeSource = null;
    episodeRows = [];
    expandedEpisode = null;
    episodeSignatureCache = "";
    renderEpisodes();
  } else if (signature !== episodeSignatureCache) {
    episodeSignatureCache = signature;
    loadEpisodeList(episodeSource.kind, episodeSource.id).catch(toastError);
  }
}

bind("btn-vid-refresh", () => refreshLibrarySection("videos"));
bind("btn-ds-refresh", () => refreshLibrarySection("datasets"));
bind("btn-snap-refresh", () => refreshLibrarySection("snapshots"));
bind("btn-md-refresh", () => refreshLibrarySection("models"));

function fillPortSelect(id, ports, connectedPort, fallback) {
  const sel = $(id);
  if (!sel) return;
  if (document.activeElement === sel) return;
  const current = sel.value;
  const explicitSelection = sel.dataset.userSelected === "true";
  const seen = new Set();

  function addOption(parent, p) {
    if (!p || !p.port || seen.has(p.port)) return;
    seen.add(p.port);
    const opt = document.createElement("option");
    opt.value = p.port;
    opt.textContent = `${p.port} — ${p.description || p.port}`;
    parent.appendChild(opt);
  }

  sel.innerHTML = "";
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = "No device";
  sel.appendChild(empty);
  const likely = ports.filter((p) => p.likely);
  const other = ports.filter((p) => !p.likely);
  if (likely.length) {
    const group = document.createElement("optgroup");
    group.label = "likely arm / leader";
    likely.forEach((p) => addOption(group, p));
    sel.appendChild(group);
  }
  if (other.length) {
    const group = document.createElement("optgroup");
    group.label = "other serial";
    other.forEach((p) => addOption(group, p));
    sel.appendChild(group);
  }
  const preferred = connectedPort || (explicitSelection ? current : (fallback || ""));
  if (preferred && !seen.has(preferred)) {
    addOption(sel, { port: preferred, description: preferred });
  }
  if (preferred && [...sel.options].some((o) => o.value === preferred)) sel.value = preferred;
  else sel.value = "";
}

async function refreshPorts() {
  try {
    const ports = await fetch(BASE + "/api/ports").then((r) => r.json());
    lastPorts = Array.isArray(ports) ? ports : [];
    const robot = (last && last.robot) || {};
    const leader = (last && last.leader) || {};
    const uiHw = (meta.ui && meta.ui.hardware) || {};
    fillPortSelect(
      "arm-port",
      lastPorts,
      robot.connected ? robot.port : "",
      uiHw.arm_port || (meta.robot && meta.robot.port) || "",
    );
    fillPortSelect(
      "leader-port",
      lastPorts,
      leader.connected ? leader.port : "",
      uiHw.leader_port || (meta.leader && meta.leader.port) || "",
    );
    syncDevicePowerButtons(robot, leader);
  } catch { /* ignore */ }
}
refreshPorts();
setInterval(refreshPorts, 4000);

document.addEventListener("keydown", (e) => {
  if (["INPUT", "TEXTAREA"].includes(e.target.tagName)) return;
  if (e.key === "Escape" && (replayActive || episodeSource)) {
    closeEpisodeSelection();
    e.preventDefault();
  }
});

fetch(BASE + "/api/meta")
  .then((r) => r.json())
  .then((m) => {
    meta = m;
    ensureJointRows(meta.joints || JOINT_FALLBACK);
    savedPresets = {
      record: {},
      rollout: {},
      pose: {},
      debug: {},
      hardware: {},
      ...(m.saved_presets || {}),
    };
    if (!presetSelections.hardware && m.active_hardware_preset) {
      presetSelections.hardware = m.active_hardware_preset;
    }
    if (!presetSelections.hardware && savedPresets.hardware["Disconnected"]) {
      presetSelections.hardware = "Disconnected";
    }
    saveJsonStorage(PRESET_SELECTION_KEY, presetSelections);
    refreshPresetSelects();
    if (m.ui) {
      applyRecordFields(m.ui.record);
      applyRolloutFields(m.ui.rollout);
      if (m.ui.hold != null) $("chk-hold").checked = !!m.ui.hold;
      if (m.ui.auto_record != null) autoRecord = !!m.ui.auto_record;
      if (m.ui.selected_dataset) selectedDatasetId = m.ui.selected_dataset;
      if (m.ui.selected_video) selectedVideoId = m.ui.selected_video;
      if ($("btn-hdr-auto")) $("btn-hdr-auto").classList.toggle("auto-on", autoRecord);
      if (autoRecord) api("/api/auto_record", { enabled: true }).catch(() => {});
    }
    if (m.recording) {
      const actionFps = m.recording.action_fps || m.recording.fps;
      const videoFps = m.recording.video_fps || m.recording.fps || actionFps;
      const savedRecord = (m.ui && m.ui.record) || {};
      if (actionFps && savedRecord.action_fps == null && savedRecord.fps == null && $("rec-action-fps")) {
        $("rec-action-fps").value = actionFps;
      }
      if (videoFps && savedRecord.video_fps == null && savedRecord.fps == null && $("rec-video-fps")) {
        $("rec-video-fps").value = videoFps;
      }
      if (m.recording.root && $("rec-root") && !$("rec-root").value) $("rec-root").value = m.recording.root;
      if (m.recording.default_num_episodes && $("rec-num")) $("rec-num").value = m.recording.default_num_episodes;
    }
    refreshPorts();
    updateResumeTargetUi();
    updateTaskInfo();
    refreshLibrary();
  })
  .catch(() => ensureJointRows(JOINT_FALLBACK));

connectWs();
refreshSessions();
refreshLibrary();
setInterval(refreshSessions, 8000);
setInterval(refreshLibrary, 15000);
