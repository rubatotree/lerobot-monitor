const JOINT_FALLBACK = [
  "shoulder_pan", "shoulder_lift", "elbow_flex",
  "wrist_flex", "wrist_roll", "gripper",
];

const PAL = ["#c56a32", "#c9a227", "#7d9a62", "#8eb4c8", "#c45c4a", "#b58a62"];
const HIST = 180;

let meta = { joints: JOINT_FALLBACK, limits: {}, presets: {}, cameras: [] };
let targets = {};
let last = {};
let ws;

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

async function api(path, body) {
  const opts = body === undefined
    ? { method: "POST" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function toastError(err) {
  const log = $("log");
  const li = document.createElement("li");
  li.className = "error";
  li.textContent = `${new Date().toLocaleTimeString()}  ${err.message || err}`;
  log.prepend(li);
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
          ticks: { color: "#6e675c", font: { size: 9, family: "IBM Plex Mono" } },
          grid: { color: "#221e18" },
          border: { color: "#3d362c" },
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

function ensureJointRows(names) {
  const root = $("joint-rows");
  if (root.dataset.ready === "1") return;
  root.innerHTML = "";
  names.forEach((name) => {
    const [lo, hi] = limitsFor(name);
    const row = document.createElement("div");
    row.className = "j-row";
    row.innerHTML = `
      <span class="j-name" title="${name}">${name}
        <div class="j-cur" data-cur="${name}">cur —</div>
      </span>
      <input type="range" min="${lo}" max="${hi}" step="0.1" value="0" data-joint="${name}"/>
      <span class="j-val" data-val="${name}">0.0</span>`;
    const slider = row.querySelector("input");
    slider.addEventListener("input", () => {
      targets[name] = Number(slider.value);
      row.querySelector(".j-val").textContent = fmt(targets[name]);
    });
    slider.addEventListener("change", async () => {
      try {
        await api("/api/joints", { joints: { [name]: Number(slider.value) } });
      } catch (err) { toastError(err); }
    });
    root.appendChild(row);
  });
  root.dataset.ready = "1";
}

function updateJoints(joints) {
  const names = meta.joints.length ? meta.joints : JOINT_FALLBACK;
  ensureJointRows(names);
  names.forEach((name) => {
    const cur = joints[name];
    const el = document.querySelector(`[data-cur="${name}"]`);
    if (el) el.textContent = `cur ${fmt(cur)}`;
    if (targets[name] == null && cur != null) {
      targets[name] = cur;
      const slider = document.querySelector(`input[data-joint="${name}"]`);
      const val = document.querySelector(`[data-val="${name}"]`);
      if (slider && document.activeElement !== slider) slider.value = String(cur);
      if (val) val.textContent = fmt(cur);
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
function ensureCameras(list) {
  const host = $("cameras");
  const names = (list || []).map((c) => c.name || c);
  const all = names.length ? names : (meta.cameras || []);
  all.forEach((name) => {
    if (camCards[name]) return;
    const card = document.createElement("article");
    card.className = "cam-card";
    card.innerHTML = `
      <div class="cam-lbl"><span>${name}</span><span data-cam-meta="${name}">no signal</span></div>
      <div class="no-sig">No signal</div>
      <img alt="${name}" src="/camera/${encodeURIComponent(name)}"/>`;
    const img = card.querySelector("img");
    img.addEventListener("load", () => {
      card.classList.add("has-sig");
      img.classList.add("live");
    });
    host.appendChild(card);
    camCards[name] = card;
  });
  setCamGrid(Object.keys(camCards).length || 1);
}

function renderLogs(logs) {
  const ol = $("log");
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
    const items = await fetch("/api/sessions").then((r) => r.json());
    const ol = $("sessions");
    ol.innerHTML = "";
    items.slice(0, 12).forEach((s) => {
      const li = document.createElement("li");
      li.textContent = `${s.session_id}  ${s.kind}  ${s.frames ?? 0}f`;
      ol.appendChild(li);
    });
  } catch { /* ignore */ }
}

function applyStatus(d) {
  last = d;
  const mode = d.mode || "offline";
  const pill = $("mode-pill");
  pill.textContent = mode;
  pill.className = `pill ${mode}`;
  $("fps").textContent = d.fps ? `${d.fps} Hz` : "— Hz";

  const robot = d.robot || {};
  const leader = d.leader || {};
  const stR = $("st-robot");
  stR.textContent = robot.connected ? robot.port : (robot.error || "offline");
  stR.className = `stat-val ${robot.connected ? "ok" : "bad"}`;
  const stL = $("st-leader");
  stL.textContent = leader.connected ? leader.port : (leader.error || "offline");
  stL.className = `stat-val ${leader.connected ? "ok" : ""}`;

  const task = d.task || {};
  $("st-task").textContent = task.kind || mode;
  $("st-elapsed").textContent = fmtTime(task.elapsed_s);
  $("st-session").textContent = task.session_id || "—";
  if (task.episode_time_s) {
    const frac = Math.min(1, (task.episode_elapsed_s || 0) / task.episode_time_s);
    $("prog-fill").style.width = `${frac * 100}%`;
    $("st-episode").textContent = `#${task.episode_index}  ${fmtTime(task.episode_elapsed_s)} / ${fmtTime(task.episode_time_s)}`;
  } else {
    $("prog-fill").style.width = "0%";
    $("st-episode").textContent = task.recording ? `#${task.episode_index}` : "—";
  }

  const hold = $("chk-hold");
  if (document.activeElement !== hold) hold.checked = !!d.hold;

  ensureCameras(d.cameras || []);
  (d.cameras || []).forEach((cam) => {
    const el = document.querySelector(`[data-cam-meta="${cam.name}"]`);
    if (el) el.textContent = cam.connected ? `${cam.fps} fps` : (cam.error || "no signal");
  });

  if (d.joints && Object.keys(d.joints).length) {
    updateJoints(d.joints);
    pushChart(stateChart, d.joints);
  }
  if (d.action && Object.keys(d.action).length) pushChart(actionChart, d.action);
  if (d.logs) renderLogs(d.logs);
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
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
  $(id).addEventListener("click", async () => {
    try { await fn(); } catch (err) { toastError(err); }
  });
}

bind("btn-apply", () => api("/api/joints", { joints: { ...targets } }));
document.querySelectorAll("[data-preset]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    try {
      await api("/api/joints/preset", { name: btn.dataset.preset });
      const pose = (meta.presets || {})[btn.dataset.preset] || {};
      Object.assign(targets, pose);
      Object.entries(pose).forEach(([name, value]) => {
        const slider = document.querySelector(`input[data-joint="${name}"]`);
        const val = document.querySelector(`[data-val="${name}"]`);
        if (slider) slider.value = String(value);
        if (val) val.textContent = fmt(value);
      });
    } catch (err) { toastError(err); }
  });
});
$("chk-hold").addEventListener("change", async (e) => {
  try { await api("/api/hold", { enabled: e.target.checked }); }
  catch (err) { toastError(err); }
});

bind("btn-robot-on", () => api("/api/robot/connect"));
bind("btn-robot-off", () => api("/api/robot/disconnect"));
bind("btn-leader-on", () => api("/api/leader/connect"));
bind("btn-leader-off", () => api("/api/leader/disconnect"));
bind("btn-teleop-on", () => api("/api/teleop/start"));
bind("btn-teleop-off", () => api("/api/teleop/stop"));
bind("btn-rec-on", () => api("/api/record/start", {
  task: $("rec-task").value,
  repo_id: $("rec-repo").value,
  episode_time_s: Number($("rec-ep").value) || 20,
}));
bind("btn-rec-next", () => api("/api/record/next"));
bind("btn-rec-off", async () => { await api("/api/record/stop"); refreshSessions(); });
bind("btn-roll-on", () => api("/api/rollout/start", {
  policy_path: $("pol-path").value.trim(),
  task: $("pol-task").value,
  duration_s: Number($("pol-dur").value),
  device: $("pol-dev").value.trim() || "cuda",
  record: $("chk-roll-rec").checked,
}));
bind("btn-roll-off", async () => { await api("/api/rollout/stop"); refreshSessions(); });
bind("btn-estop", () => api("/api/estop"));
bind("btn-resume", () => api("/api/resume"));

document.addEventListener("keydown", (e) => {
  if (["INPUT", "TEXTAREA"].includes(e.target.tagName)) return;
  if (e.key === "Escape") {
    api("/api/estop").catch(toastError);
    e.preventDefault();
  }
});

fetch("/api/meta")
  .then((r) => r.json())
  .then((m) => {
    meta = m;
    ensureJointRows(meta.joints || JOINT_FALLBACK);
    ensureCameras((meta.cameras || []).map((name) => ({ name })));
  })
  .catch(() => ensureJointRows(JOINT_FALLBACK));

connectWs();
refreshSessions();
setInterval(refreshSessions, 8000);
