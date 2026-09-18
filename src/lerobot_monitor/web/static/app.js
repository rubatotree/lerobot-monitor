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
let modelsCache = [];
let previewVideoId = "";
let previewEpisode = 0;
const MATCH_TOL = {
  shoulder_pan: 2, shoulder_lift: 2, elbow_flex: 2,
  wrist_flex: 2, wrist_roll: 2, gripper: 3,
};

function $(id) { return document.getElementById(id); }

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
  if (!res.ok) {
    const detail = data.detail;
    throw new Error(typeof detail === "string" ? detail : res.statusText);
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
function requestStop() {
  actionBusy = false;
  document.querySelectorAll(".hdr-icon.pending").forEach((el) => el.classList.remove("pending"));
  localLog("stop requested");
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

function mkChart(id) {
  return new Chart($(id).getContext("2d"), {
    type: "line",
    data: { labels: [], datasets: [] },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      elements: { point: { radius: 0 }, line: { borderWidth: 1.4 } },
      plugins: { legend: { display: false } },
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
}

const stateChart = mkChart("chart-state");
const actionChart = mkChart("chart-action");

function pushChart(chart, scalars) {
  const keys = Object.keys(scalars);
  while (chart.data.datasets.length < keys.length) {
    const i = chart.data.datasets.length;
    chart.data.datasets.push({
      label: keys[i] || "",
      data: [],
      borderColor: PAL[i % PAL.length],
      tension: 0.2,
    });
  }
  chart.data.datasets.forEach((ds, i) => { if (keys[i]) ds.label = keys[i]; });
  chart.data.labels.push("");
  if (chart.data.labels.length > HIST) chart.data.labels.shift();
  keys.forEach((k, i) => {
    const ds = chart.data.datasets[i];
    ds.data.push(scalars[k]);
    if (ds.data.length > HIST) ds.data.shift();
  });
  chart.update("none");
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
  if (followSliders) return;
  pendingLive[name] = value;
  if (liveTimer) return;
  liveTimer = setTimeout(flushLive, 40);
}

function taskFollowing() {
  const mode = (last && (last.display_mode || last.mode)) || "";
  return mode === "teleop" || mode === "record" || mode === "rollout";
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
    <img alt="${label}" src="${src}"/>`;
  const img = card.querySelector("img");
  img.addEventListener("load", () => {
    card.classList.add("has-sig");
    img.classList.add("live");
  });
  host.appendChild(card);
  camCards[id] = card;
  setCamGrid(Object.keys(camCards).length || 1);
  return card;
}

function syncCamCards(ids) {
  Object.keys(camCards).forEach((id) => {
    if (!ids.includes(id)) {
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
    addCamCard(
      String(cam.name),
      cam.label || `cam ${cam.name}`,
      `${BASE}/camera/${encodeURIComponent(cam.name)}`,
      `${cam.fps} fps · :${cam.port}`,
    );
  });
  syncCamCards(ids);
  if (!streaming.length) {
    $("cameras").dataset.empty = "1";
  }
}

let camMenuKey = "";

function renderCamMenu(list) {
  camMenu = list || [];
  const root = $("cam-rows");
  if (!root) return;
  const key = camMenu.map((c) => `${c.name}:${c.label}:${c.enabled}:${c.show_main}:${c.feed_robot}:${c.streaming}:${c.width}x${c.height}:${c.port}:${c.connected}`).join("|");
  if (key === camMenuKey) return;
  camMenuKey = key;
  const ae = document.activeElement;
  const focused = ae && root.contains(ae) && ["INPUT", "TEXTAREA", "SELECT"].includes(ae.tagName);
  if (focused) return;
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
    card.innerHTML = `
      <header>
        <h3>${shown}</h3>
        <span class="${cam.streaming ? "on" : "off"}">${cam.streaming ? "stream on" : "local only"}</span>
      </header>
      <img class="mini" alt="preview ${shown}" src="${BASE}/camera/${encodeURIComponent(name)}"/>
      <label>Name
        <input type="text" data-label="${name}" value="${shown}" placeholder="front / side"/>
      </label>
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
      </label>
      <div class="checks">
        <label class="check"><input type="checkbox" data-enabled="${name}" ${cam.enabled ? "checked" : ""}/> Enable</label>
        <label class="check"><input type="checkbox" data-main="${name}" ${cam.show_main ? "checked" : ""} ${cam.enabled ? "" : "disabled"}/> Main view</label>
        <label class="check"><input type="checkbox" data-robot="${name}" ${cam.feed_robot ? "checked" : ""} ${cam.enabled && cam.show_main ? "" : "disabled"}/> Robot input</label>
      </div>
      <p class="hw-status ${cam.streaming ? "on" : ""}">${cam.streaming ? `http://127.0.0.1:${cam.port}/video` : "network stream off"}</p>
      <div class="row-actions">
        <button type="button" data-apply="${name}">Apply size</button>
        <button type="button" data-stream="${name}">${cam.streaming ? "Stop stream" : "Open network stream"}</button>
      </div>`;
    root.appendChild(card);
  });
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
  const mode = d.display_mode || d.mode || "offline";
  const owner = d.owner || (d.robot && d.robot.connected ? mode : "free");
  const pill = $("mode-pill");
  pill.textContent = mode;
  pill.className = `pill ${mode}`;
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
  if (modeEl) modeEl.textContent = mode;
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
  const pending = d.task && d.task.pending;
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
  if (task.resetting && task.reset_time_s) {
    const frac = Math.min(1, (task.episode_elapsed_s || 0) / task.reset_time_s);
    $("prog-fill").style.width = `${frac * 100}%`;
    $("st-episode").textContent = `reset  ${fmtTime(task.episode_elapsed_s)} / ${fmtTime(task.reset_time_s)}`;
  } else if (task.episode_time_s) {
    const frac = Math.min(1, (task.episode_elapsed_s || 0) / task.episode_time_s);
    $("prog-fill").style.width = `${frac * 100}%`;
    $("st-episode").textContent = `#${task.episode_index}  ${fmtTime(task.episode_elapsed_s)} / ${fmtTime(task.episode_time_s)}`;
  } else {
    $("prog-fill").style.width = "0%";
    $("st-episode").textContent = task.recording ? `#${task.episode_index}` : "—";
  }

  const hold = $("chk-hold");
  if (document.activeElement !== hold) hold.checked = !!d.hold;

  renderMainCameras(d.cameras || []);
  renderCamMenu(d.cameras || []);

  if (d.joints && Object.keys(d.joints).length) {
    updateJoints(d.joints);
    pushChart(stateChart, d.joints);
  }
  if (d.action && Object.keys(d.action).length) pushChart(actionChart, d.action);
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
    try { applyStatus(JSON.parse(ev.data)); } catch { /* ignore */ }
  };
}

function bind(id, fn) {
  const el = $(id);
  if (!el) return;
  el.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    try { await fn(); } catch (err) { toastError(err); }
  });
}

let savedPresets = { record: {}, rollout: {}, pose: {} };

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
}

function setTaskButton(id, on, label) {
  const el = $(id);
  if (!el) return;
  const lbl = el.querySelector(".hdr-lbl");
  if (lbl) lbl.textContent = on ? "stop" : label;
}

function recordFields() {
  return {
    task: $("rec-task").value,
    repo_id: $("rec-repo").value,
    episode_time_s: Number($("rec-ep").value) || 20,
    reset_time_s: Number($("rec-reset").value) || 0,
    num_episodes: Number($("rec-num").value) || 50,
    fps: Number($("rec-fps").value) || 15,
    format: ($("rec-format") && $("rec-format").value) || "mp4",
    root: ($("rec-root") && $("rec-root").value) || "",
    resume: !!( $("chk-rec-resume") && $("chk-rec-resume").checked),
    video: !($("chk-rec-video") ) || $("chk-rec-video").checked,
    streaming_encoding: !($("chk-rec-stream-enc")) || $("chk-rec-stream-enc").checked,
    encoder_threads: Number($("rec-enc-threads") && $("rec-enc-threads").value) || 2,
    video_id: selectedVideoId || undefined,
    dataset_id: selectedVideoId || undefined,
    merge: true,
  };
}
function applyRecordFields(p) {
  if (!p) return;
  if (p.task != null) $("rec-task").value = p.task;
  if (p.repo_id != null) $("rec-repo").value = p.repo_id;
  if (p.episode_time_s != null) $("rec-ep").value = p.episode_time_s;
  if (p.reset_time_s != null) $("rec-reset").value = p.reset_time_s;
  if (p.num_episodes != null && $("rec-num")) $("rec-num").value = p.num_episodes;
  if (p.fps != null && $("rec-fps")) $("rec-fps").value = p.fps;
  if (p.format != null && $("rec-format")) $("rec-format").value = p.format;
  if (p.root != null && $("rec-root")) $("rec-root").value = p.root;
  if (p.resume != null && $("chk-rec-resume")) $("chk-rec-resume").checked = !!p.resume;
  if (p.video != null && $("chk-rec-video")) $("chk-rec-video").checked = !!p.video;
  if (p.streaming_encoding != null && $("chk-rec-stream-enc")) $("chk-rec-stream-enc").checked = !!p.streaming_encoding;
  if (p.encoder_threads != null && $("rec-enc-threads")) $("rec-enc-threads").value = p.encoder_threads;
  if (p.video_id) selectedVideoId = p.video_id;
  else if (p.dataset_id) selectedVideoId = p.dataset_id;
}
function kvPairs() {
  const extra = {};
  document.querySelectorAll("#roll-kv .kv-row").forEach((row) => {
    const key = row.querySelector(".kv-k").value.trim();
    const value = row.querySelector(".kv-v").value;
    if (key) extra[key] = value;
  });
  return extra;
}
function setKvPairs(extra) {
  const host = $("roll-kv");
  host.innerHTML = "";
  const entries = Object.entries(extra || {});
  if (!entries.length) entries.push(["", ""]);
  entries.forEach(([key, value]) => addKvRow(key, value));
}
function addKvRow(key = "", value = "") {
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
  k.addEventListener("input", persistUi);
  v.addEventListener("input", persistUi);
  del.addEventListener("click", () => {
    row.remove();
    if (!$("roll-kv").children.length) addKvRow();
    persistUi();
  });
  row.append(k, v, del);
  $("roll-kv").appendChild(row);
}
function rolloutFields() {
  return {
    policy_path: $("pol-path").value.trim(),
    task: $("pol-task").value,
    duration_s: Number($("pol-dur").value),
    device: $("pol-dev").value.trim() || "cuda",
    fps: Number($("pol-fps") && $("pol-fps").value) || 15,
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
  if (p.fps != null && $("pol-fps")) $("pol-fps").value = p.fps;
  if (p.extra != null) setKvPairs(p.extra);
}

let uiTimer = null;
function persistUi() {
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
    const src = c.streaming ? `http://localhost:${c.port}/video` : String(c.index ?? c.name);
    return `${key}: {type: opencv, index_or_path: '${src}', width: ${c.width}, height: ${c.height}, fps: 25}`;
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
  const recFps = rec.fps || (meta.recording && meta.recording.fps) || 15;
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
    flag("dataset.fps", recFps),
    flag("dataset.video", rec.video),
    flag("dataset.streaming_encoding", rec.streaming_encoding),
    flag("dataset.encoder_threads", rec.encoder_threads),
  ]);
  set("info-roll", [
    ...envLines,
    "# in-process  equivalent CLI",
    "lerobot-rollout",
    flag("strategy.type", "base"),
    flag("policy.path", roll.policy_path || ""),
    flag("policy.device", roll.device),
    ...robotFlags,
    flag("task", roll.task || ""),
    flag("duration", roll.duration_s),
    flag("fps", roll.fps || recFps),
    flag("record", autoRecord),
    ...extraFlags(roll.extra),
  ]);
}
["rec-task", "rec-repo", "rec-ep", "rec-reset", "rec-num", "rec-fps", "rec-format", "rec-root", "chk-rec-resume", "chk-rec-video", "chk-rec-stream-enc", "rec-enc-threads", "pol-path", "pol-task", "pol-dur", "pol-fps", "pol-dev", "arm-port", "leader-port"].forEach((id) => {
  const el = $(id);
  if (!el) return;
  el.addEventListener("change", persistUi);
  el.addEventListener("input", persistUi);
});

async function saveNamedPreset(kind, name, payload) {
  const key = (name || "").trim();
  if (!key) throw new Error("preset name is empty");
  await api(`/api/presets/${kind}/${encodeURIComponent(key)}`, payload, "PUT");
  savedPresets[kind][key] = payload;
  refreshPresetSelects();
  $(kind === "record" ? "rec-preset" : kind === "rollout" ? "roll-preset" : "pose-preset").value = key;
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
  const nameId = kind === "record" ? "rec-preset-name" : kind === "rollout" ? "roll-preset-name" : "pose-preset-name";
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
bind("btn-apply", () => api("/api/joints", { joints: { ...targets }, duration_s: 2.5 }));
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
bind("btn-estop", () => api("/api/estop"));
bind("btn-resume", () => api("/api/resume"));
bind("btn-hdr-scan", () => runAction("btn-hdr-scan", "scan requested", async () => {
  await api("/api/scan");
  await refreshPorts();
  await refreshLibrary();
}));
bind("btn-hdr-relax", () => runAction("btn-hdr-relax", "relax requested", () => api("/api/joints/preset", { name: "relax", duration_s: 2.5 })));
bind("btn-hdr-teleop", () => {
  const mode = (last && last.mode) || "";
  const pending = last && last.task && last.task.pending;
  if (mode === "teleop" || pending === "teleop_start") return requestStop();
  return runAction("btn-hdr-teleop", "teleop requested", () => api("/api/teleop/start", { auto_record: autoRecord, ...captureFields() }));
});
bind("btn-hdr-record", () => {
  const mode = (last && last.mode) || "";
  const pending = last && last.task && last.task.pending;
  if (mode === "record" || pending === "record_start") return requestStop();
  return runAction("btn-hdr-record", "record requested — writing video session", async () => {
    const fields = recordFields();
    if (fields.resume && selectedVideoId) {
      fields.video_id = selectedVideoId;
      fields.dataset_id = selectedVideoId;
    } else {
      delete fields.video_id;
      delete fields.dataset_id;
      fields.resume = false;
    }
    await api("/api/record/start", fields);
    refreshLibrary();
  });
});
bind("btn-hdr-rollout", () => {
  const mode = (last && last.mode) || "";
  const pending = last && last.task && last.task.pending;
  if (mode === "rollout" || pending === "rollout_start") return requestStop();
  const path = ($("pol-path") && $("pol-path").value.trim()) || "(no policy)";
  return runAction("btn-hdr-rollout", `rollout requested — loading ${path}`, () => api("/api/rollout/start", { ...rolloutFields(), auto_record: autoRecord }));
});
bind("btn-hdr-capture", () => {
  if (capturing || (last && last.task && last.task.pending === "capture_start")) {
    return requestStop();
  }
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
bind("btn-hdr-estop", () => {
  localLog("E-STOP requested", "error");
  return api("/api/estop");
});

function captureFields() {
  const rec = recordFields();
  return {
    fps: rec.fps,
    format: rec.format,
    video_id: selectedVideoId || undefined,
    resume: rec.resume,
    task: rec.task,
    name: rec.repo_id || rec.task || "capture",
    merge: true,
  };
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
    const id = panel.dataset.panel;
    const collapsed = !!state[id];
    panel.classList.toggle("collapsed", collapsed);
    const btn = panel.querySelector(".panel-toggle");
    if (!btn) return;
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

const LAYOUT_KEY = "lerobot-monitor-layout";
const LAYOUT_DEFAULT = { sideW: 360, bottomH: 320, logW: 480, libW: 320 };

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
    layout.libW = clamp(e.clientX - box.left, 160, box.width - 360);
    applyLayout(layout);
  });
  drag($("split-v"), (e) => {
    const box = main.getBoundingClientRect();
    layout.sideW = clamp(box.right - e.clientX, 200, box.width - 160);
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
if ($("roll-kv") && !$("roll-kv").children.length) addKvRow();
if ($("btn-kv-add")) bind("btn-kv-add", () => addKvRow());

function renderVideos() {
  const ol = $("vid-list");
  if (!ol) return;
  ol.innerHTML = "";
  videosCache.forEach((vid) => {
    const li = document.createElement("li");
    if (vid.id === selectedVideoId) li.className = "sel";
    const n = (vid.episodes || []).length;
    li.textContent = `${vid.name || vid.id}  ·  ${n} ep`;
    li.title = vid.path || vid.id;
    li.addEventListener("click", () => selectVideo(vid.id));
    ol.appendChild(li);
  });
}

function renderDatasets() {
  const ol = $("ds-list");
  if (!ol) return;
  ol.innerHTML = "";
  datasetsCache.forEach((ds) => {
    const li = document.createElement("li");
    if ((ds.repo_id || ds.id) === selectedDatasetId) li.className = "sel";
    const n = ds.episodes != null ? `${ds.episodes} ep` : ds.source || "hf";
    const play = ds.playable ? "" : "  · no video";
    li.textContent = `${ds.repo_id || ds.id}  ·  ${n}${play}`;
    li.title = ds.path || ds.repo_id || "";
    li.addEventListener("click", () => selectHfDataset(ds));
    ol.appendChild(li);
  });
}

function fillCamSelect(videos) {
  const sel = $("vid-cam");
  if (!sel) return;
  const names = (videos || []).map((name) => String(name).replace(/\.(mp4|avi)$/i, ""));
  const unique = [...new Set(names)];
  if (!unique.includes("merged") && unique.length) unique.unshift("merged");
  if (!unique.length) unique.push("merged");
  const current = sel.value;
  sel.innerHTML = "";
  unique.forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  });
  if (current && unique.includes(current)) sel.value = current;
}

function renderEpisodes(vid) {
  const detail = $("vid-detail");
  const list = $("ep-list");
  const title = $("vid-title");
  if (!detail || !list) return;
  if (!vid) {
    detail.classList.add("hidden");
    return;
  }
  detail.classList.remove("hidden");
  if (title) title.textContent = `${vid.id}\n${vid.task || vid.kind || ""}`;
  list.innerHTML = "";
  const episodes = vid.episodes || [];
  episodes.forEach((ep, i) => {
    const li = document.createElement("li");
    li.innerHTML = `<div>ep ${ep.index}  ·  ${(ep.videos || []).join(", ") || "no files"}</div>`;
    const tools = document.createElement("div");
    tools.className = "ep-tools";
    const mk = (label, fn) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "ghost";
      b.textContent = label;
      b.addEventListener("click", (ev) => { ev.stopPropagation(); fn(); });
      return b;
    };
    tools.append(
      mk("play", () => playEpisode(vid.id, ep)),
      mk("up", () => moveEpisode(vid.id, i, -1)),
      mk("down", () => moveEpisode(vid.id, i, 1)),
      mk("del", () => deleteEpisode(vid.id, ep.index)),
    );
    li.appendChild(tools);
    li.addEventListener("click", () => playEpisode(vid.id, ep));
    list.appendChild(li);
  });
}

const vizState = { kind: "", id: "", episode: 0, episodes: 0, duration: 0, chart: null };

function previewFileUrl(kind, id, episode, cam) {
  return `${BASE}/api/preview/file?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}&episode=${episode}&cam=${encodeURIComponent(cam)}&t=${Date.now()}`;
}

function vizVideos() {
  return [...document.querySelectorAll("#viz-cams video")];
}

function clipWindow(video) {
  const start = Number(video.dataset.start || 0);
  const rawEnd = video.dataset.end;
  const end = rawEnd ? Number(rawEnd) : (video.duration || 0);
  const duration = Math.max(0.001, (end || video.duration || 0) - start);
  return { start, end: end || (start + duration), duration };
}

function onVizTime(ev) {
  const lead = vizVideos()[0];
  if (!lead || ev.target !== lead) return;
  const win = clipWindow(lead);
  if (win.end && lead.currentTime >= win.end - 0.05) {
    lead.pause();
    vizVideos().forEach((video) => video.pause());
    const btn = $("viz-play");
    if (btn) btn.textContent = "play";
  }
  const t = Math.max(0, (lead.currentTime || 0) - win.start);
  vizState.duration = win.duration;
  if ($("viz-seek")) $("viz-seek").value = String(Math.round((1000 * t) / win.duration));
  if ($("viz-t")) $("viz-t").textContent = `${t.toFixed(2)}s`;
  vizVideos().slice(1).forEach((video) => {
    if (Math.abs(video.currentTime - lead.currentTime) > 0.12) video.currentTime = lead.currentTime;
  });
}

function renderVizCams(cameras) {
  const host = $("viz-cams");
  if (!host) return;
  host.innerHTML = "";
  host.classList.toggle("multi", (cameras || []).length > 1);
  if (!(cameras || []).length) {
    host.innerHTML = `<p class="hw-status">no playable video for this episode</p>`;
    return;
  }
  (cameras || []).forEach((cam) => {
    const wrap = document.createElement("div");
    wrap.className = "viz-cam";
    wrap.innerHTML = `<span>${cam.name}</span><video playsinline muted></video>`;
    const video = wrap.querySelector("video");
    video.dataset.start = String(cam.start || 0);
    video.dataset.end = cam.end != null ? String(cam.end) : "";
    video.src = previewFileUrl(vizState.kind, vizState.id, vizState.episode, cam.name);
    video.addEventListener("loadedmetadata", () => {
      const start = Number(video.dataset.start || 0);
      if (start > 0) video.currentTime = start;
      if (video === vizVideos()[0]) vizState.duration = clipWindow(video).duration;
    });
    video.addEventListener("timeupdate", onVizTime);
    video.addEventListener("ended", () => {
      const btn = $("viz-play");
      if (btn) btn.textContent = "play";
    });
    host.appendChild(wrap);
  });
}

function renderVizChart(data) {
  const canvas = $("viz-chart");
  if (!canvas || typeof Chart === "undefined") return;
  if (vizState.chart) {
    vizState.chart.destroy();
    vizState.chart = null;
  }
  const series = data.series || {};
  const times = data.t || [];
  const keys = Object.keys(series);
  if (!keys.length) return;
  vizState.chart = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: {
      labels: times.map((t) => Number(t).toFixed(2)),
      datasets: keys.map((key, i) => ({
        label: key.replace("obs.", "s.").replace("act.", "a."),
        data: series[key],
        borderColor: PAL[i % PAL.length],
        borderWidth: 1.2,
        pointRadius: 0,
        tension: 0.15,
      })),
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: true, labels: { boxWidth: 8, color: "#9b98b3", font: { size: 9 } } },
      },
      scales: {
        x: { display: false },
        y: {
          ticks: { color: "#6a6780", font: { size: 8 } },
          grid: { color: "#171a28" },
          border: { color: "#2c3148" },
        },
      },
    },
  });
}

async function loadPreview(kind, id, episode) {
  const res = await fetch(`${BASE}/api/preview?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}&episode=${episode}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "preview failed");
  vizState.kind = kind;
  vizState.id = id;
  vizState.episode = episode;
  vizState.episodes = Number(data.episodes || 0);
  const panel = $("viz");
  if (panel) panel.classList.remove("hidden");
  if ($("viz-title")) $("viz-title").textContent = `${data.title || id}\nep ${episode}`;
  if ($("viz-ep")) $("viz-ep").value = String(episode);
  const lastEp = Math.max(0, vizState.episodes - 1);
  if ($("viz-ep-max")) $("viz-ep-max").textContent = `/ ${lastEp}`;
  if ($("viz-play")) $("viz-play").textContent = "play";
  renderVizCams(data.cameras || []);
  renderVizChart(data);
}

function stepPreview(delta) {
  if (!vizState.id) return;
  const lastEp = Math.max(0, vizState.episodes - 1);
  const next = Math.min(lastEp, Math.max(0, vizState.episode + delta));
  loadPreview(vizState.kind, vizState.id, next).catch(toastError);
}

async function selectVideo(id) {
  selectedVideoId = id;
  persistUi();
  renderVideos();
  try {
    const vid = await fetch(BASE + `/api/videos/${encodeURIComponent(id)}`).then((r) => r.json());
    renderEpisodes(vid);
    const detail = $("vid-detail");
    if (detail) detail.classList.remove("hidden");
    await loadPreview("video", id, 0);
  } catch (err) { toastError(err); }
}

function selectHfDataset(ds) {
  selectedDatasetId = ds.repo_id || ds.id;
  if ($("rec-repo")) $("rec-repo").value = selectedDatasetId;
  if (ds.path && $("rec-root")) $("rec-root").value = ds.path;
  persistUi();
  renderDatasets();
  const detail = $("vid-detail");
  if (detail) detail.classList.add("hidden");
  loadPreview("dataset", selectedDatasetId, 0).catch(toastError);
}

function playEpisode(videoId, ep) {
  loadPreview("video", videoId, ep.index).catch(toastError);
}

if ($("viz-prev")) bind("viz-prev", () => stepPreview(-1));
if ($("viz-next")) bind("viz-next", () => stepPreview(1));
if ($("viz-play")) bind("viz-play", () => {
  const videos = vizVideos();
  if (!videos.length) return;
  const lead = videos[0];
  if (lead.paused) {
    const win = clipWindow(lead);
    if (win.end && lead.currentTime >= win.end - 0.05) {
      videos.forEach((v) => { v.currentTime = Number(v.dataset.start || 0); });
    }
    videos.forEach((v) => v.play().catch(() => {}));
    $("viz-play").textContent = "pause";
  } else {
    videos.forEach((v) => v.pause());
    $("viz-play").textContent = "play";
  }
});
if ($("viz-ep")) {
  $("viz-ep").addEventListener("change", () => {
    if (!vizState.id) return;
    const lastEp = Math.max(0, vizState.episodes - 1);
    const ep = Math.min(lastEp, Math.max(0, Number($("viz-ep").value) || 0));
    loadPreview(vizState.kind, vizState.id, ep).catch(toastError);
  });
}
if ($("viz-seek")) {
  $("viz-seek").addEventListener("input", () => {
    const videos = vizVideos();
    if (!videos.length) return;
    const win = clipWindow(videos[0]);
    const t = win.start + (Number($("viz-seek").value) / 1000) * win.duration;
    videos.forEach((v) => { v.currentTime = t; });
    if ($("viz-t")) $("viz-t").textContent = `${(t - win.start).toFixed(2)}s`;
  });
}

async function moveEpisode(videoId, from, delta) {
  const vid = videosCache.find((d) => d.id === videoId) || await fetch(BASE + `/api/videos/${encodeURIComponent(videoId)}`).then((r) => r.json());
  const order = (vid.episodes || []).map((e) => e.index);
  const to = from + delta;
  if (to < 0 || to >= order.length) return;
  const tmp = order[from];
  order[from] = order[to];
  order[to] = tmp;
  try {
    await api(`/api/videos/${encodeURIComponent(videoId)}/episodes/reorder`, { order });
    await refreshLibrary();
    await selectVideo(videoId);
  } catch (err) { toastError(err); }
}

async function deleteEpisode(videoId, index) {
  if (!window.confirm(`Delete episode ${index}?`)) return;
  try {
    await api(`/api/videos/${encodeURIComponent(videoId)}/episodes/${index}`, undefined, "DELETE");
    await refreshLibrary();
    await selectVideo(videoId);
  } catch (err) { toastError(err); }
}

function renderModels() {
  const ol = $("md-list");
  if (!ol) return;
  ol.innerHTML = "";
  modelsCache.forEach((m) => {
    const li = document.createElement("li");
    li.textContent = m.name;
    li.title = m.path;
    li.addEventListener("click", () => {
      $("pol-path").value = m.path;
      persistUi();
    });
    ol.appendChild(li);
  });
}

async function refreshLibrary() {
  try {
    videosCache = await fetch(BASE + "/api/videos").then((r) => r.json());
    if (!Array.isArray(videosCache)) videosCache = [];
  } catch { videosCache = []; }
  try {
    datasetsCache = await fetch(BASE + "/api/datasets").then((r) => r.json());
    if (!Array.isArray(datasetsCache)) datasetsCache = [];
  } catch { datasetsCache = []; }
  try {
    modelsCache = await fetch(BASE + "/api/models").then((r) => r.json());
    if (!Array.isArray(modelsCache)) modelsCache = [];
  } catch { modelsCache = []; }
  renderVideos();
  renderDatasets();
  renderModels();
  if (selectedVideoId) {
    const vid = videosCache.find((d) => d.id === selectedVideoId);
    if (vid) renderEpisodes(vid);
  }
}

bind("btn-vid-refresh", () => refreshLibrary());
bind("btn-ds-refresh", () => refreshLibrary());
bind("btn-md-refresh", () => refreshLibrary());

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
  if (e.key === "Escape") {
    api("/api/estop").catch(toastError);
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
      if (m.recording.fps && $("rec-fps") && !$("rec-fps").value) $("rec-fps").value = m.recording.fps;
      if (m.recording.root && $("rec-root") && !$("rec-root").value) $("rec-root").value = m.recording.root;
      if (m.recording.default_num_episodes && $("rec-num")) $("rec-num").value = m.recording.default_num_episodes;
    }
    refreshPorts();
    updateTaskInfo();
    refreshLibrary();
  })
  .catch(() => ensureJointRows(JOINT_FALLBACK));

connectWs();
refreshSessions();
refreshLibrary();
setInterval(refreshSessions, 8000);
setInterval(refreshLibrary, 15000);
