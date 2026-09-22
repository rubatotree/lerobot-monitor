const JOINT_FALLBACK = [
  "shoulder_pan", "shoulder_lift", "elbow_flex",
  "wrist_flex", "wrist_roll", "gripper",
];

const PAL = ["#8b7cf7", "#6ea8ff", "#c084fc", "#5dba9a", "#e06b7a", "#7dd3fc"];
const HIST = 180;
const BASE = (document.documentElement.dataset.base || "/lerobot").replace(/\/$/, "");

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

const CHART_UPDATE_INTERVAL_MS = 150;
let chartUpdateTimer = null;
const pendingChartUpdates = new Set();

function flushChartUpdates() {
  chartUpdateTimer = null;
  const charts = [...pendingChartUpdates];
  pendingChartUpdates.clear();
  charts.forEach((chart) => chart.update("none"));
}

function scheduleChartUpdate(chart) {
  if (!chart || document.hidden) return;
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
function exitReplayForControl() {
  if (episodeSource || replayActive || armReplay) closeEpisodeSelection();
}

function requestStop() {
  actionBusy = false;
  document.querySelectorAll(".hdr-icon.pending").forEach((el) => el.classList.remove("pending"));
  if (replayActive || episodeSource) closeEpisodeSelection();
  else localLog("stop requested");
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
      elements: { point: { radius: 0 }, line: { borderWidth: 1.4 } },
      interaction: { mode: "nearest", axis: "x", intersect: false },
      plugins: {
        legend: { display: false },
      },
      scales: {
        x: { display: false },
        y: {
          ticks: { color: "#6a6780", font: { size: 9, family: "IBM Plex Mono" } },
          grid: { color: "#171a28" },
          border: { color: "#2c3148" },
        },
      },
    },
    });
    const cursor = document.createElement("span");
    cursor.className = "chart-replay-cursor";
    cursor.hidden = true;
    canvas.parentElement.appendChild(cursor);
    chart.$replayCursorElement = cursor;
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
const PREDICTION_BREAK_COLOR = "#ff5a6a";

function liveTimeScale() {
  return {
    type: "linear",
    display: false,
    min: 0,
    ticks: {
      color: "#85819c",
      font: { size: 9, family: "IBM Plex Mono" },
      callback: (value) => `${Number(value).toFixed(1)}s`,
    },
    grid: { color: "#171a28" },
    border: { color: "#2c3148" },
  };
}

// Live charts own `$live` (measured values) and `$predictions` (dashed overlays)
// so Chart.js never has to guess which dataset is which.
function syncChartDatasets(chart) {
  const next = (chart.$live || []).concat(chart.$predictions || []);
  const current = chart.data.datasets;
  const unchanged = current.length === next.length && next.every((ds, i) => current[i] === ds);
  if (!unchanged) chart.data.datasets = next;
}

function pushChart(chart, scalars, timeS = null) {
  if (!chart) return;
  const keys = Object.keys(scalars);
  const timeAxis = Number.isFinite(timeS);
  if (chart.$timeAxis !== timeAxis) {
    chart.$timeAxis = timeAxis;
    chart.data.labels = [];
    chart.$live = [];
    chart.$predictions = [];
    chart.options.scales.x = timeAxis ? liveTimeScale() : { display: false, type: "category" };
    syncChartDatasets(chart);
  }
  const live = chart.$live || (chart.$live = []);
  while (live.length < keys.length) {
    live.push({
      label: "",
      data: [],
      borderColor: PAL[live.length % PAL.length],
      tension: 0.2,
    });
  }
  live.forEach((ds, i) => { if (keys[i]) ds.label = keys[i]; });
  if (timeAxis) {
    keys.forEach((k, i) => {
      const ds = live[i];
      ds.data.push({ x: Number(timeS), y: Number(scalars[k]) });
      if (ds.data.length > HIST) ds.data.shift();
    });
  } else {
    chart.data.labels.push("");
    if (chart.data.labels.length > HIST) chart.data.labels.shift();
    keys.forEach((k, i) => {
      const ds = live[i];
      ds.data.push(scalars[k]);
      if (ds.data.length > HIST) ds.data.shift();
    });
  }
  syncChartDatasets(chart);
  scheduleChartUpdate(chart);
}

function rolloutPredictionList() {
  if (!Array.isArray(vizState.rolloutPredictions)) vizState.rolloutPredictions = [];
  return vizState.rolloutPredictions;
}

function recordRolloutPrediction(prediction, now) {
  const actions = prediction && Array.isArray(prediction.actions) ? prediction.actions : [];
  const start = Number(prediction && prediction.t_s);
  if (!actions.length || !Number.isFinite(start)) return;
  const step = Number(prediction.step_s) > 0 ? Number(prediction.step_s) : 1 / 30;
  const key = String(prediction.id || `${start.toFixed(3)}#${actions.length}`);
  const list = rolloutPredictionList();
  if (list.some((chunk) => chunk.key === key)) return;
  const points = [];
  actions.forEach((action, index) => {
    const joints = action && action.joints ? action.joints : action;
    if (!joints || typeof joints !== "object") return;
    points.push({ x: start + (index + 1) * step, joints });
  });
  if (!points.length) return;
  const end = points[points.length - 1].x;
  // A newer chunk replaces the older predictions inside the window it covers.
  const kept = [];
  list.forEach((chunk) => {
    const surviving = chunk.points.filter((point) => point.x < start || point.x > end);
    if (surviving.length) kept.push({ ...chunk, points: surviving });
  });
  kept.push({
    key,
    start,
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
        if (!series.has(name)) series.set(name, { points: [], breaks: [] });
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
        entry.breaks.push({ x: previous.x, y: previous.y });
        entry.breaks.push({ x: point.x, y: point.y });
      }
      merged.push(point);
      previous = point;
    });
    // A horizon that lapsed without a fresh chunk is a break too.
    if (previous && Number.isFinite(now) && now - previous.x > 2.5 * (previous.step || 0)) {
      entry.breaks.push({ x: previous.x, y: previous.y });
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
  let overlayEnd = 0;
  rolloutOverlaySeries(now).forEach((entry, name) => {
    if (!entry.points.length) return;
    const color = colorByName.get(name) || PAL[Math.max(0, names.indexOf(name)) % PAL.length];
    datasets.push({
      label: `${name} · pred`,
      data: entry.points,
      borderColor: color,
      borderWidth: 1.3,
      borderDash: [4, 3],
      pointRadius: 0,
      tension: 0.15,
      spanGaps: false,
      $prediction: true,
    });
    entry.points.forEach((point) => {
      if (Number.isFinite(point.y)) overlayEnd = Math.max(overlayEnd, point.x);
    });
    if (entry.breaks.length) {
      datasets.push({
        label: `${name} · prediction gap`,
        data: entry.breaks,
        borderColor: PREDICTION_BREAK_COLOR,
        backgroundColor: PREDICTION_BREAK_COLOR,
        borderWidth: 1.2,
        pointRadius: 3,
        pointStyle: "rectRot",
        showLine: false,
        $prediction: true,
      });
    }
  });
  chart.$predictions = datasets;
  syncChartDatasets(chart);
  chart.options.scales.x = {
    ...liveTimeScale(),
    min: Math.max(0, now - ROLLOUT_WINDOW_S),
    max: Math.max(now, overlayEnd) + 0.25,
  };
  scheduleChartUpdate(chart);
}

function renderRolloutOverlays(now) {
  applyRolloutOverlay(stateChart, now);
  applyRolloutOverlay(actionChart, now);
}

function clearRolloutPredictions() {
  const hasOverlay = (chart) => !!(chart && (chart.$predictions || []).length);
  const hasChunks = rolloutPredictionList().length > 0;
  if (!hasChunks && !hasOverlay(stateChart) && !hasOverlay(actionChart)) return;
  vizState.rolloutPredictions = [];
  [stateChart, actionChart].forEach((chart) => {
    if (!hasOverlay(chart)) return;
    chart.$predictions = [];
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
  const owner = d.owner || (d.robot && d.robot.connected ? mode : "free");
  const pill = $("mode-pill");
  pill.textContent = replayActive ? "REPLAY" : mode;
  pill.className = `pill ${replayActive ? "replay" : mode}`;
  $("fps").textContent = `${Number(d.fps || 0).toFixed(1)} Hz`;

  const robot = d.robot || {};
  const leader = d.leader || {};
  busLive = !!robot.connected;
  $("joint-panel").classList.toggle("bus-live", busLive);

  const busEl = $("st-bus");
  if (busEl) {
    busEl.textContent = robot.connected ? `${robot.port} · ${owner}` : owner;
  }
  const modeEl = $("st-mode");
  if (modeEl) modeEl.textContent = replayActive ? `replay · ${mode}` : mode;
  const stR = $("st-robot");
  stR.textContent = robot.connected ? robot.port : "free";
  const stL = $("st-leader");
  if (leader.connected) {
    const lOwner = (mode === "teleop" || mode === "record") ? mode : "connected";
    stL.textContent = `${leader.port} · ${lOwner}`;
  } else {
    stL.textContent = "off";
  }
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

  // During rollout the charts switch to a time axis so the predicted chunk can be
  // drawn ahead of "now" and kept afterwards for comparison with the real curve.
  const rolloutLive = !replayActive && mode === "rollout";
  const elapsedS = Number(task.elapsed_s) || 0;
  if (d.joints && Object.keys(d.joints).length) {
    updateJoints(d.joints);
    if (!replayActive) pushChart(stateChart, d.joints, rolloutLive ? elapsedS : null);
  }
  if (!replayActive && d.action && Object.keys(d.action).length) {
    pushChart(actionChart, d.action, rolloutLive ? elapsedS : null);
  }
  if (rolloutLive) {
    recordRolloutPrediction(d.prediction, elapsedS);
    renderRolloutOverlays(elapsedS);
  } else {
    clearRolloutPredictions();
  }
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

let savedPresets = { record: {}, rollout: {}, pose: {}, debug: {} };

function presetSelectId(kind) {
  if (kind === "record") return "rec-preset";
  if (kind === "rollout") return "roll-preset";
  if (kind === "debug") return "dbg-preset";
  return "pose-preset";
}

function presetNameId(kind) {
  if (kind === "record") return "rec-preset-name";
  if (kind === "rollout") return "roll-preset-name";
  if (kind === "debug") return "dbg-preset-name";
  return "pose-preset-name";
}

function fillPresetSelect(id, group) {
  const sel = $(id);
  const current = sel.value;
  sel.innerHTML = `<option value="">—</option>`;
  Object.keys(group || {}).sort().forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  });
  if (current && group[current]) sel.value = current;
}

function refreshPresetSelects() {
  fillPresetSelect("rec-preset", savedPresets.record);
  fillPresetSelect("roll-preset", savedPresets.rollout);
  fillPresetSelect("pose-preset", savedPresets.pose);
  fillPresetSelect("dbg-preset", savedPresets.debug);
}

function setTaskButton(id, on, label) {
  const el = $(id);
  if (!el) return;
  const lbl = el.querySelector(".hdr-lbl");
  if (lbl) lbl.textContent = on ? "stop" : label;
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
  const armPort = ($("arm-port") && $("arm-port").value) || robot.port || "";
  const leadPort = ($("leader-port") && $("leader-port").value) || leader.port || "";
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

async function saveNamedPreset(kind, name, payload) {
  const key = (name || "").trim();
  if (!key) throw new Error("preset name is empty");
  await api(`/api/presets/${kind}/${encodeURIComponent(key)}`, payload, "PUT");
  savedPresets[kind][key] = payload;
  refreshPresetSelects();
  const select = $(presetSelectId(kind));
  if (select) select.value = key;
}
async function deleteNamedPreset(kind, name) {
  if (!name) return;
  await api(`/api/presets/${kind}/${encodeURIComponent(name)}`, undefined, "DELETE");
  delete savedPresets[kind][name];
  refreshPresetSelects();
}

bind("btn-rec-save", () => saveNamedPreset("record", $("rec-preset-name").value || $("rec-preset").value, recordFields()));
bind("btn-rec-load", () => applyRecordFields(savedPresets.record[$("rec-preset").value]));
bind("btn-rec-del", () => deleteNamedPreset("record", $("rec-preset").value));
bind("btn-roll-save", () => saveNamedPreset("rollout", $("roll-preset-name").value || $("roll-preset").value, rolloutFields()));
bind("btn-roll-load", () => applyRolloutFields(savedPresets.rollout[$("roll-preset").value]));
bind("btn-roll-del", () => deleteNamedPreset("rollout", $("roll-preset").value));
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
  await saveNamedPreset(kind, copy, JSON.parse(JSON.stringify(src)));
  const nameId = presetNameId(kind);
  if ($(nameId)) $(nameId).value = copy;
}

bind("btn-rec-dup", () => duplicateNamedPreset("record", $("rec-preset").value));
bind("btn-roll-dup", () => duplicateNamedPreset("rollout", $("roll-preset").value));
bind("btn-pose-save", () => saveNamedPreset("pose", $("pose-preset-name").value || $("pose-preset").value, { ...targets }));
bind("btn-pose-load", () => {
  const pose = savedPresets.pose[$("pose-preset").value];
  if (!pose) return;
  applyPoseToSliders(pose);
});
bind("btn-pose-dup", () => duplicateNamedPreset("pose", $("pose-preset").value));
bind("btn-pose-del", () => deleteNamedPreset("pose", $("pose-preset").value));
$("rec-preset").addEventListener("change", () => {
  $("rec-preset-name").value = $("rec-preset").value;
  applyRecordFields(savedPresets.record[$("rec-preset").value]);
});
$("roll-preset").addEventListener("change", () => {
  $("roll-preset-name").value = $("roll-preset").value;
  applyRolloutFields(savedPresets.rollout[$("roll-preset").value]);
});
$("pose-preset").addEventListener("change", () => {
  $("pose-preset-name").value = $("pose-preset").value;
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

bind("btn-robot-on", () => {
  const port = $("arm-port").value;
  persistUi();
  return api("/api/robot/connect", port ? { port } : {});
});
bind("btn-robot-off", () => api("/api/robot/disconnect"));
bind("btn-leader-on", () => {
  const port = $("leader-port").value;
  persistUi();
  return api("/api/leader/connect", port ? { port } : {});
});
bind("btn-leader-off", () => api("/api/leader/disconnect"));
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
  if (mode === "teleop" || pending === "teleop_start") return requestStop();
  exitReplayForControl();
  return runAction("btn-hdr-teleop", "teleop requested", () => api("/api/teleop/start", { auto_record: autoRecord, ...captureFields() }));
});
bind("btn-hdr-record", () => {
  const mode = (last && last.mode) || "";
  const pending = last && last.task && last.task.pending;
  if (mode === "record" || pending === "record_start") return requestStop();
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
  if (mode === "rollout" || pending === "rollout_start") return requestStop();
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
    return requestStop();
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

initTabList("library-tabs", "lerobot-monitor-library-tab", "videos");
initTabList("side-tabs", "lerobot-monitor-side-tab", "joints");

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
  videosCache.forEach((vid) => {
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
  datasetsCache.forEach((ds) => {
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
  snapshotsCache.forEach((snapshot) => {
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
bind("btn-dbg-save", () => saveNamedPreset("debug", $("dbg-preset-name").value || $("dbg-preset").value, debugFields()));
bind("btn-dbg-load", () => applyDebugFields(savedPresets.debug[$("dbg-preset").value]));
bind("btn-dbg-dup", () => duplicateNamedPreset("debug", $("dbg-preset").value));
bind("btn-dbg-del", () => deleteNamedPreset("debug", $("dbg-preset").value));
if ($("dbg-preset")) {
  $("dbg-preset").addEventListener("change", () => {
    $("dbg-preset-name").value = $("dbg-preset").value;
    applyDebugFields(savedPresets.debug[$("dbg-preset").value]);
  });
}
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
  const rect = canvas.getBoundingClientRect();
  if (!rect.width) return;
  const canvasX = ((event.clientX - rect.left) * chart.width) / rect.width;
  const pixel = Math.min(area.right, Math.max(area.left, canvasX));
  seekViz(xScale.getValueForPixel(pixel));
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
  const keys = Object.keys(series).filter((key) => key.startsWith(prefix));
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
      borderWidth: 1.2,
      pointRadius: 0,
      tension: 0.15,
    });
  });
  overlaySeries.forEach((points, name) => {
    chart.data.datasets.push({
      label: `${name} · pred`,
      data: points,
      borderColor: PAL[seriesIndex.get(name) % PAL.length],
      borderWidth: 1.3,
      borderDash: [4, 3],
      pointRadius: 0,
      tension: 0.15,
      // Predictions stay out of the legend: the dashed style already says "prediction".
      $prediction: true,
    });
  });
  const timesEnd = times.length ? Math.max(0, Number(times[times.length - 1]) || 0) : 0;
  const overlayEnd = (overlay || []).reduce((max, point) => Math.max(max, overlayStart + (Number(point.x) || 0)), 0);
  const seriesEnd = Math.max(timesEnd, overlayEnd);
  const domainMax = Math.max(1, vizState.duration || 0, seriesEnd);
  chart.$replaySeriesEnd = seriesEnd;
  chart.$replayCursorTime = Math.min(domainMax, Math.max(0, vizState.elapsed || 0));
  chart.options.scales.x = {
    type: "linear",
    display: true,
    min: 0,
    max: domainMax,
    ticks: { color: "#85819c", font: { size: 9, family: "IBM Plex Mono" }, callback: (value) => `${Number(value).toFixed(2)}s` },
    grid: { color: "#171a28" },
    border: { color: "#2c3148" },
  };
  chart.options.plugins.tooltip = {
    mode: "index",
    intersect: false,
    callbacks: {
      title: (items) => items.length ? `t = ${Number(items[0].parsed.x).toFixed(3)} s` : "",
      label: (context) => `${context.dataset.label}: ${Number(context.parsed.y).toFixed(4)}`,
    },
  };
  chart.options.plugins.legend = {
    display: chart.data.datasets.some((dataset) => !dataset.$prediction),
    labels: {
      filter: (item) => {
        const dataset = chart.data.datasets[item.datasetIndex];
        return !dataset || !dataset.$prediction;
      },
    },
  };
  chart.update("none");
  positionReplayChartCursor(chart);
}

function resetReplayCharts() {
  [stateChart, actionChart].forEach((chart) => {
    if (!chart) return;
    chart.$timeAxis = false;
    chart.$live = [];
    chart.$predictions = [];
    chart.data.labels = [];
    chart.data.datasets = [];
    chart.options.plugins.legend.display = false;
    chart.$replayCursorTime = Number.NaN;
    chart.$replaySeriesEnd = 0;
    chart.options.scales.x = { display: false, type: "category" };
    chart.options.plugins.tooltip = {};
    chart.update("none");
    positionReplayChartCursor(chart);
  });
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
  modelsCache.forEach((m) => {
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
    if (select) {
      const option = document.createElement("option");
      option.value = m.path;
      option.textContent = label;
      option.disabled = !m.path;
      select.appendChild(option);
    }
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
  const robotPort = last.robot && last.robot.connected ? last.robot.port : "";
  const leaderPort = last.leader && last.leader.connected ? last.leader.port : "";
  const seen = new Set();

  function addOption(parent, p) {
    if (!p || !p.port || seen.has(p.port)) return;
    seen.add(p.port);
    const opt = document.createElement("option");
    opt.value = p.port;
    let tag = "";
    if (p.port === robotPort) tag = "  · arm connected";
    else if (p.port === leaderPort) tag = "  · leader connected";
    opt.textContent = `${p.port} — ${p.description || p.port}${tag}`;
    parent.appendChild(opt);
  }

  sel.innerHTML = "";
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
  const preferred = connectedPort || fallback || current || "";
  if (preferred && !seen.has(preferred)) {
    addOption(sel, { port: preferred, description: preferred });
  }
  if (preferred && [...sel.options].some((o) => o.value === preferred)) sel.value = preferred;
  if (!sel.options.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "no serial devices";
    sel.appendChild(opt);
  }
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
    savedPresets = m.saved_presets || savedPresets;
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
