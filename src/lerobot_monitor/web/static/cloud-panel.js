/* Cloud panel: the standalone port-8095 manager, hosted inside the Monitor page.
 *
 * It talks to ``/api/cloud/...`` on the Monitor origin. The browser never sees
 * an SSH alias' token: Monitor owns the tunnel and injects the bearer token
 * when it proxies to the remote cloud service.
 */

const LABELS = {
  connected: "connected", disconnected: "disconnected", connecting: "connecting",
  ready: "ready", loaded: "loaded", loading: "loading", unloading: "unloading",
  deploying: "deploying", deleting: "deleting", queued: "queued", running: "running",
  pending: "pending", downloading: "downloading", uploading: "uploading",
  failed: "failed", error: "error", succeeded: "succeeded", success: "succeeded",
  completed: "succeeded", cancelled: "cancelled", offline: "offline", healthy: "available",
  busy: "busy", unavailable: "unavailable", stopped: "stopped", unloaded: "unloaded",
};

export const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

export const collection = (value, key) => (
  Array.isArray(value) ? value : (Array.isArray(value?.[key]) ? value[key] : [])
);

export const statusText = (value) => LABELS[value] || String(value || "unknown");

export function statusClass(value) {
  if (["connected", "ready", "loaded", "success", "succeeded", "completed", "healthy"].includes(value)) return "good";
  if (["failed", "error", "unavailable"].includes(value)) return "bad";
  if (["loading", "unloading", "running", "queued", "pending", "connecting", "downloading", "uploading", "busy", "deploying", "deleting"].includes(value)) return "warn";
  return "neutral";
}

export function gpuAvailable(gpu, deployments = []) {
  return Boolean(gpu.uuid)
    && gpu.healthy !== false
    && !gpu.error
    && !gpu.busy
    && gpu.available !== false
    && !deployments.some((model) => model.gpu_uuid === gpu.uuid
      && ["loaded", "loading", "unloading"].includes(model.status));
}

export function deploymentActions(model) {
  const busy = ["loading", "unloading", "queued", "running", "downloading", "uploading", "deploying", "deleting"].includes(model.status);
  return {
    load: !busy && ["ready", "registered", "deployed", "unloaded", "stopped", "error", "failed"].includes(model.status),
    unload: model.status === "loaded",
    remove: !busy && model.status !== "loaded",
  };
}

const KINDS = {
  bootstrap: "Initialize service", upgrade: "Upgrade service", probe: "Probe server",
  upload: "Upload checkpoint", download: "Download weights", deploy: "Add model",
  load: "Load model", unload: "Unload model", runtime: "Install runtime",
  install_runtime: "Install runtime",
};

async function request(path, { method = "GET", body, timeout = 30000, fetcher = fetch } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetcher(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
      cache: "no-store",
    });
    const text = await response.text();
    let result;
    try { result = text ? JSON.parse(text) : {}; } catch { result = { detail: text.slice(0, 500) }; }
    if (!response.ok) {
      const detail = typeof result.detail === "string"
        ? result.detail
        : JSON.stringify(result.detail || result.error || `Request failed (${response.status})`);
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    return result;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("Request timed out; a remote job may still be running — refresh to check.");
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export function createCloudPanel(doc = document, options = {}) {
  const base = String(options.base || doc.documentElement?.dataset?.base || "").replace(/\/$/, "");
  const fetcher = options.fetcher || fetch;
  const $ = (id) => doc.getElementById(id);
  const state = {
    base, hosts: [], hostId: "", health: null, gpus: [], models: [],
    cloudJobs: [], localJobs: [], connected: false, busy: false, refreshing: false,
    generation: 0, pendingRefresh: false, dialogHandler: null, timer: null, message: "", error: false,
  };
  const esc = escapeHTML;
  const host = () => state.hosts.find((item) => item.id === state.hostId);
  const hostBusy = (id) => state.hosts.find((item) => item.id === id)?.operation_status === "busy";
  const api = (suffix = "", hostId = state.hostId) => (
    hostId ? `${base}/api/cloud/hosts/${encodeURIComponent(hostId)}/cloud/api/v1${suffix}` : null
  );
  const badge = (value) => `<span class="cloud-badge ${statusClass(value)}">${esc(statusText(value))}</span>`;
  const setHTML = (id, html) => { const node = $(id); if (node && node.innerHTML !== html) node.innerHTML = html; };

  function notice(message = "", error = false) {
    state.message = message;
    state.error = error;
    const node = $("cloud-notice");
    if (!node) return;
    node.hidden = !message;
    node.textContent = message;
    node.className = `cloud-notice${error ? " error" : ""}`;
  }

  function renderHostSelect() {
    const select = $("cloud-host-select");
    if (!select) return;
    const signature = state.hosts.map((row) => `${row.id}:${row.alias}:${row.status}`).join("|");
    if (select.dataset.signature !== signature) {
      select.dataset.signature = signature;
      select.innerHTML = state.hosts.length
        ? state.hosts.map((row) => `<option value="${esc(row.id)}">${esc(row.alias)} · ${esc(statusText(row.status || "disconnected"))}</option>`).join("")
        : '<option value="">No server registered</option>';
    }
    if (state.hostId) select.value = state.hostId;
    select.disabled = state.hosts.length === 0;
    const add = $("cloud-host-add");
    if (add) add.disabled = state.busy;
  }

  function renderHostMeta() {
    const selected = host();
    const meta = $("cloud-host-meta");
    const actions = $("cloud-host-actions");
    if (!meta || !actions) return;
    if (!selected) {
      meta.innerHTML = '<div class="cloud-empty"><svg class="cloud-empty-icon" viewBox="0 0 24 24"><path d="M5 12h14M12 5l7 7-7 7"/></svg><strong>No server registered</strong>Add an SSH server to manage cloud models. The alias must exist in your local SSH config.</div>';
      actions.innerHTML = "";
      return;
    }
    const connected = selected.status === "connected";
    const operationBusy = hostBusy(selected.id);
    const disabled = state.busy || operationBusy;
    setHTML("cloud-host-meta", `<div class="cloud-host-line">${badge(selected.status || "disconnected")}${operationBusy ? ` ${badge("busy")}` : ""}${selected.build_outdated ? ` ${badge("outdated build")}` : ""}</div><p class="cloud-host-path mono">${esc(selected.root)} · ${esc(selected.port || 8091)}</p>${selected.build_outdated ? '<p class="cloud-hint">The server runs an older build than this Monitor. Click Upgrade, then reload the model.</p>' : ""}${selected.error ? `<p class="cloud-host-error">${esc(selected.error)}</p>` : ""}`);
    setHTML("cloud-host-actions", `<button type="button" data-cloud-host-action="probe" ${disabled ? "disabled" : ""}>Probe</button><button type="button" data-cloud-host-action="bootstrap" ${disabled ? "disabled" : ""}>Initialize</button>${connected ? `<button type="button" data-cloud-host-action="upgrade" ${disabled ? "disabled" : ""}>Upgrade</button>` : ""}<button type="button" data-cloud-host-action="runtime" ${disabled ? "disabled" : ""}>Runtime</button><button type="button" data-cloud-host-action="${connected ? "disconnect" : "connect"}" class="${connected ? "ghost" : ""}" ${disabled ? "disabled" : ""}>${connected ? "Disconnect" : "Connect"}</button>`);
  }

  function renderGpus() {
    const summary = $("cloud-gpu-summary");
    const list = $("cloud-gpu-list");
    if (!list) return;
    if (!state.connected) {
      if (summary) summary.textContent = "";
      setHTML("cloud-gpu-list", '<div class="cloud-empty"><svg class="cloud-empty-icon" viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h4M6 14h8"/></svg><strong>GPU information</strong>Connect a server to see GPU resources.</div>');
      return;
    }
    const available = state.gpus.filter((gpu) => gpuAvailable(gpu, state.models)).length;
    if (summary) summary.textContent = `${available} / ${state.gpus.length} available`;
    setHTML("cloud-gpu-list", state.gpus.length ? state.gpus.map((gpu) => {
      const free = gpuAvailable(gpu, state.models);
      const healthy = gpu.healthy !== false && !gpu.error;
      const total = Number(gpu.memory_total_mb || 0);
      const used = Number(gpu.memory_used_mb || 0);
      const percent = Math.min(100, Math.max(0, total ? (used / total) * 100 : 0));
      const meterClass = percent >= 90 ? "critical" : percent >= 70 ? "high" : "";
      const processes = Array.isArray(gpu.processes) ? gpu.processes : [];
      const owners = processes.slice(0, 2).map((process) => `<li><strong>${esc(process.user || "unknown")}</strong><span>${esc(process.program || "unknown")}</span><span>${(Number(process.memory_used_mb || 0) / 1024).toFixed(1)} GiB</span></li>`).join("");
      const ownership = owners
        ? `<ul class="cloud-gpu-owners">${owners}</ul>${processes.length > 2 ? `<p class="cloud-hint">+${processes.length - 2} more process(es)</p>` : ""}`
        : (used > 512 ? '<p class="cloud-hint">Driver memory; no attributable compute process</p>' : "");
      return `<article class="cloud-gpu ${healthy ? "" : "bad"}"><div class="cloud-gpu-top"><span class="cloud-gpu-index">GPU ${esc(gpu.index)}</span>${badge(!healthy ? "unavailable" : free ? "healthy" : "busy")}</div><p class="cloud-gpu-name" title="${esc(gpu.name)}">${esc(gpu.name || "unknown GPU")}</p><div class="cloud-gpu-mem"><span class="cloud-gpu-mem-used mono">${(used / 1024).toFixed(1)} / ${(total / 1024).toFixed(1)} GiB</span><span>used</span></div><div class="cloud-meter ${meterClass}" role="meter" aria-label="GPU ${esc(gpu.index)} memory used" aria-valuenow="${Math.round(percent)}" aria-valuemin="0" aria-valuemax="100"><span style="width:${percent}%"></span></div>${ownership}</article>`;
    }).join("") : '<div class="cloud-empty"><svg class="cloud-empty-icon" viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h4M6 14h8"/></svg><strong>No GPU reported</strong>Check the server driver.</div>');
  }

  function renderModels() {
    const count = $("cloud-model-count");
    const runtimeNode = $("cloud-runtime-status");
    const available = state.gpus.filter((gpu) => gpuAvailable(gpu, state.models)).length;
    if (count) count.textContent = state.connected ? String(state.models.length) : "";
    if (runtimeNode) {
      const runtime = state.health?.runtime;
      if (runtime?.configured) {
        runtimeNode.innerHTML = `<span class="cloud-runtime-badge configured">Runtime · ${esc(runtime.profile || "configured")}</span>`;
      } else if (runtime?.configured === false) {
        runtimeNode.innerHTML = '<span class="cloud-runtime-badge missing">Runtime not configured</span>';
      } else {
        runtimeNode.innerHTML = state.connected ? '<span class="cloud-runtime-badge">Runtime status loading…</span>' : "";
      }
    }
    const add = $("cloud-model-add");
    if (add) add.disabled = !state.connected || state.busy;
    setHTML("cloud-model-list", !state.connected
      ? '<div class="cloud-empty"><svg class="cloud-empty-icon" viewBox="0 0 24 24"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg><strong>Cloud models</strong>Connect a server to list and deploy models.</div>'
      : state.models.length ? state.models.map((model) => {
        const actions = deploymentActions(model);
        const active = Number(model.active_sessions || 0) > 0;
        return `<article class="cloud-model" data-status="${esc(model.status || "")}"><div class="cloud-model-main"><div class="cloud-model-title"><h4>${esc(model.name || model.id)}</h4>${badge(model.status)}</div><p class="cloud-model-source mono">${esc(model.source_kind === "huggingface" ? "HF" : "PATH")} · ${esc(model.source || model.path || "")}</p>${model.revision ? `<p class="cloud-model-note mono">revision · ${esc(model.revision)}</p>` : ""}${model.gpu_uuid ? `<p class="cloud-model-note mono">${esc(model.gpu_uuid)}${active ? " · in use" : ""}</p>` : ""}${model.error ? `<p class="cloud-model-error">${esc(model.error)}</p>` : ""}</div><div class="cloud-model-actions">${model.status === "loaded" ? `<button type="button" data-cloud-model-action="unload" data-id="${esc(model.id)}" ${active || state.busy ? "disabled" : ""}>Unload</button>` : `<button type="button" data-cloud-model-action="load" data-id="${esc(model.id)}" ${!actions.load || !available || state.busy ? "disabled" : ""}>Load</button>`}<button type="button" class="ghost" data-cloud-model-action="logs" data-id="${esc(model.id)}">Logs</button><button type="button" data-cloud-model-action="use" data-id="${esc(model.id)}" title="Register this deployment in the Model library so Rollout and Debug can select it">Add to library</button><button type="button" class="ghost" data-cloud-model-action="remove" data-id="${esc(model.id)}" ${!actions.remove || state.busy ? "disabled" : ""}>Remove</button></div></article>`;
      }).join("") : '<div class="cloud-empty"><svg class="cloud-empty-icon" viewBox="0 0 24 24"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg><strong>No deployment yet</strong>Download weights from Hugging Face, register a server path, or upload a local checkpoint.</div>');
  }

  function renderJobs() {
    const jobs = [...state.localJobs.map((job) => ({ ...job, local: true })), ...state.cloudJobs]
      .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")))
      .slice(0, 12);
    setHTML("cloud-job-list", jobs.length ? jobs.map((job) => {
      const date = job.updated_at || job.created_at;
      const dt = date ? new Date(typeof date === "number" ? date * 1000 : date) : null;
      const label = dt && !Number.isNaN(dt.getTime())
        ? dt.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })
        : "";
      return `<article class="cloud-job">${badge(job.status)}<div class="cloud-job-info"><p class="cloud-job-name">${esc(KINDS[job.kind] || job.kind || "Job")}${job.local ? " · local" : ""}</p><p class="cloud-job-detail">${esc(job.error || job.message || job.id)}</p></div><time>${esc(label)}</time></article>`;
    }).join("") : '<div class="cloud-empty"><svg class="cloud-empty-icon" viewBox="0 0 24 24"><path d="M12 8v4l3 3"/><circle cx="12" cy="12" r="10"/></svg><strong>No jobs yet</strong>Deploy, upload and load progress shows here.</div>');
  }

  function render() {
    renderHostSelect();
    renderHostMeta();
    renderGpus();
    renderModels();
    renderJobs();
    const updated = $("cloud-updated-at");
    if (updated && state.connected) updated.textContent = `${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
  }

  async function refresh({ manual = false } = {}) {
    if (state.refreshing) { state.pendingRefresh = true; return; }
    state.refreshing = true;
    const generation = state.generation;
    const button = $("cloud-refresh");
    if (button) button.disabled = true;
    try {
      const [hosts, localJobs] = await Promise.all([
        request(`${base}/api/cloud/hosts`, { fetcher }),
        request(`${base}/api/cloud/jobs`, { fetcher }),
      ]);
      if (generation !== state.generation) return;
      state.hosts = collection(hosts, "hosts");
      state.localJobs = collection(localJobs, "jobs");
      if (!state.hostId || !state.hosts.some((row) => row.id === state.hostId)) {
        state.hostId = state.hosts[0]?.id || "";
      }
      const selected = host();
      if (selected && selected.status === "connected") {
        const root = api("");
        const [health, gpus, models, cloudJobs] = await Promise.all([
          request(`${root}/health`, { fetcher }),
          request(`${root}/gpus`, { fetcher }),
          request(`${root}/deployments`, { fetcher }),
          request(`${root}/jobs`, { fetcher }),
        ]);
        if (generation !== state.generation) return;
        state.health = health;
        state.gpus = collection(gpus, "gpus");
        state.models = collection(models, "deployments");
        state.cloudJobs = collection(cloudJobs, "jobs");
        state.connected = true;
        if (manual) notice("Cloud status updated.");
      } else {
        state.connected = false;
        state.health = null;
        state.gpus = [];
        state.models = [];
        state.cloudJobs = [];
        if (manual && selected) notice("Server is disconnected; connect it to manage models.");
      }
    } catch (error) {
      if (generation === state.generation) {
        state.connected = false;
        state.gpus = [];
        state.models = [];
        state.cloudJobs = [];
        notice(error.message || String(error), true);
      }
    } finally {
      state.refreshing = false;
      if (button) button.disabled = false;
      render();
      if (state.pendingRefresh) { state.pendingRefresh = false; void refresh(); }
    }
  }

  function openDialog(title, bodyHTML, submitLabel, handler) {
    const dialog = $("cloud-dialog");
    const titleNode = $("cloud-dialog-title");
    const body = $("cloud-dialog-body");
    const submit = $("cloud-dialog-submit");
    const errorNode = $("cloud-dialog-error");
    if (!dialog || !body) return;
    if (titleNode) titleNode.textContent = title;
    body.innerHTML = bodyHTML;
    if (submit) { submit.textContent = submitLabel; submit.disabled = false; }
    if (errorNode) { errorNode.hidden = true; errorNode.textContent = ""; }
    state.dialogHandler = handler;
    if (typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
    else dialog.setAttribute("open", "");
    const first = body.querySelector("input, select, textarea");
    if (first) first.focus();
  }

  function closeDialog() {
    const dialog = $("cloud-dialog");
    if (!dialog) return;
    if (typeof dialog.close === "function" && dialog.open) dialog.close();
    else dialog.removeAttribute("open");
    state.dialogHandler = null;
  }

  function field(label, id, input) {
    return `<label class="cloud-field" for="${id}">${esc(label)}${input}</label>`;
  }

  function addHostDialog() {
    openDialog("Add cloud server",
      '<p class="cloud-hint">Uses an existing local SSH alias; SSH keys never enter the browser.</p>'
      + field("SSH alias", "cloud-alias", '<input id="cloud-alias" name="alias" required placeholder="8x4090-server" autocomplete="off"/>')
      + field("Server data directory", "cloud-root", '<input id="cloud-root" name="root" required placeholder="/data/me/lerobot-monitor"/>')
      + '<div class="cloud-field-row">'
      + field("Service port", "cloud-port", '<input id="cloud-port" name="port" type="number" min="1024" max="65535" value="8091" required/>')
      + field("Python command", "cloud-python", '<input id="cloud-python" name="python" value="python3.12" required/>')
      + "</div>", "Add server", async (data) => {
        await request(`${base}/api/cloud/hosts`, {
          method: "POST",
          body: {
            alias: data.get("alias").trim(),
            root: data.get("root").trim(),
            port: Number(data.get("port")),
            python: data.get("python").trim(),
          },
          fetcher,
        });
        notice("Server added. Probe or connect to continue.");
      });
  }

  function runtimeDialog() {
    const selected = host();
    if (!selected) return;
    openDialog("Install inference runtime",
      '<p class="cloud-hint">Build a LeRobot wheel into a separate environment on this server.</p>'
      + field("Local LeRobot wheel", "cloud-wheel", '<input id="cloud-wheel" name="wheel_path" required placeholder="D:\\\\packages\\\\lerobot-0.4.4-py3-none-any.whl"/>')
      + field("Model type", "cloud-profile", '<select id="cloud-profile" name="profile"><option value="act">ACT</option><option value="smolvla" selected>SmolVLA</option><option value="pi">pi0 / pi0.5</option></select>')
      + field("Server Hugging Face cache (optional)", "cloud-hf", '<input id="cloud-hf" name="huggingface_home" placeholder="/data/me/huggingface"/>'),
      "Install runtime", async (data) => {
        if (hostBusy(selected.id)) throw new Error("This server already has a management job running.");
        await request(`${base}/api/cloud/hosts/${encodeURIComponent(selected.id)}/runtime`, {
          method: "POST",
          body: {
            wheel_path: data.get("wheel_path").trim(),
            profile: data.get("profile"),
            huggingface_home: data.get("huggingface_home").trim() || undefined,
          },
          fetcher,
        });
        notice("Runtime install submitted. Progress shows under Recent jobs.");
      });
  }

  function upgradeDialog() {
    const selected = host();
    if (!selected) return;
    openDialog("Upgrade cloud service",
      '<p class="cloud-hint">Install this checkout on the server and switch to it after verification. Active inference sessions make the service refuse the upgrade.</p>',
      "Upgrade", async () => {
        if (hostBusy(selected.id)) throw new Error("This server already has a management job running.");
        await request(`${base}/api/cloud/hosts/${encodeURIComponent(selected.id)}/upgrade`, { method: "POST", body: {}, fetcher });
        notice("Upgrade submitted. Progress shows under Recent jobs.");
      });
  }

  function addModelDialog() {
    const selected = host();
    if (!selected) return;
    openDialog("Add cloud model",
      field("Display name", "cloud-model-name", '<input id="cloud-model-name" name="name" required maxlength="160" placeholder="SmolVLA base"/>')
      + field("Source", "cloud-model-kind", '<select id="cloud-model-kind" name="source_kind"><option value="huggingface">Hugging Face</option><option value="path">Server path</option><option value="upload">Upload local directory</option></select>')
      + field("Address", "cloud-model-source", '<input id="cloud-model-source" name="source" required placeholder="lerobot/smolvla_base" autocomplete="off"/>')
      + field("Revision (optional)", "cloud-model-revision", '<input id="cloud-model-revision" name="revision" placeholder="main, tag or commit"/>')
      + '<p class="cloud-hint" id="cloud-model-help">Downloaded by the server; does not occupy GPU memory.</p>',
      "Add", async (data) => {
        const kind = data.get("source_kind");
        if (kind === "upload") {
          if (hostBusy(selected.id)) throw new Error("This server already has a management job running.");
          await request(`${base}/api/cloud/hosts/${encodeURIComponent(selected.id)}/upload`, {
            method: "POST",
            body: { name: data.get("name").trim(), path: data.get("source").trim() },
            fetcher,
          });
        } else {
          await request(api("/deployments"), {
            method: "POST",
            body: {
              name: data.get("name").trim(),
              source_kind: kind,
              source: data.get("source").trim(),
              revision: kind === "huggingface" ? data.get("revision").trim() || undefined : undefined,
            },
            fetcher,
          });
        }
        notice("Model added. Progress shows under Recent jobs.");
      });
    const kindSelect = $("cloud-model-kind");
    const sourceInput = $("cloud-model-source");
    const revisionInput = $("cloud-model-revision");
    const help = $("cloud-model-help");
    if (kindSelect) kindSelect.addEventListener("change", () => {
      const kind = kindSelect.value;
      if (revisionInput) revisionInput.closest("label").hidden = kind !== "huggingface";
      if (sourceInput) {
        sourceInput.placeholder = kind === "huggingface" ? "lerobot/smolvla_base" : kind === "upload" ? "D:\\models\\my-checkpoint" : "/data/models/my-checkpoint";
      }
      if (help) {
        help.textContent = kind === "huggingface"
          ? "Downloaded by the server; does not occupy GPU memory."
          : kind === "upload"
            ? "Directory on this computer; uploaded and checksum-verified before registration."
            : "Registers an existing server directory; removal keeps the files.";
      }
    });
  }

  async function hostAction(action) {
    if (state.busy || hostBusy(state.hostId)) return;
    const selected = host();
    if (!selected) return;
    if (action === "runtime") { runtimeDialog(); return; }
    if (action === "upgrade") { upgradeDialog(); return; }
    state.busy = true;
    renderHostMeta();
    try {
      if (action === "probe") {
        const result = await request(`${base}/api/cloud/hosts/${encodeURIComponent(selected.id)}/probe`, { method: "POST", body: {}, timeout: 60000, fetcher });
        notice(`Probe ok · Python ${result.python} · uv ${result.uv ? "yes" : "no"} · root ${result.root_exists ? "exists" : "missing"} · writable ${result.writable ? "yes" : "no"}`);
      } else if (action === "connect") {
        await request(`${base}/api/cloud/hosts/${encodeURIComponent(selected.id)}/connect`, { method: "POST", body: {}, timeout: 120000, fetcher });
        notice("Connected.");
      } else if (action === "disconnect") {
        await request(`${base}/api/cloud/hosts/${encodeURIComponent(selected.id)}/disconnect`, { method: "POST", body: {}, fetcher });
        notice("SSH tunnel closed; remote jobs keep running.");
      } else if (action === "bootstrap") {
        await request(`${base}/api/cloud/hosts/${encodeURIComponent(selected.id)}/bootstrap`, { method: "POST", body: {}, fetcher });
        notice("Initialization submitted. Progress shows under Recent jobs.");
      }
    } catch (error) {
      notice(error.message || String(error), true);
    } finally {
      state.busy = false;
      await refresh();
    }
  }

  async function modelAction(action, id) {
    const model = state.models.find((item) => item.id === id);
    if (!model) return;
    const root = api(`/deployments/${encodeURIComponent(id)}`);
    if (action === "logs") {
      const dialog = $("cloud-log-dialog");
      const title = $("cloud-log-title");
      const content = $("cloud-log-content");
      if (title) title.textContent = `${model.name || model.id} · logs`;
      if (content) content.textContent = "Loading logs…";
      if (dialog && typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
      try {
        const result = await request(`${root}/logs`, { fetcher });
        if (content) content.textContent = result.text || (result.lines || []).join("\n") || "No logs.";
      } catch (error) {
        if (content) content.textContent = error.message || String(error);
      }
      return;
    }
    if (action === "use") {
      try {
        const added = await request(`${base}/api/models/cloud`, {
          method: "POST",
          body: { host_id: state.hostId, deployment_id: id, name: model.name || "" },
          fetcher,
        });
        notice(`${added.name || model.name || model.id} added to the Model library; open Rollout or Debug to use it.`);
      } catch (error) {
        notice(error.message || String(error), true);
      }
      return;
    }
    if (action === "load") {
      const gpus = state.gpus.filter((gpu) => gpuAvailable(gpu, state.models));
      openDialog(`Load ${model.name || model.id}`,
        '<p class="cloud-hint">Loads weights onto one GPU. The model stays resident until unloaded.</p>'
        + field("GPU", "cloud-load-gpu", `<select id="cloud-load-gpu" name="gpu_uuid" required><option value="">Choose a GPU</option>${gpus.map((gpu) => `<option value="${esc(gpu.uuid)}">GPU ${esc(gpu.index)} · ${esc(gpu.name)} · ${((Number(gpu.memory_total_mb || 0) - Number(gpu.memory_used_mb || 0)) / 1024).toFixed(1)} GiB free</option>`).join("")}</select>`),
        "Load", async (data) => {
          if (!data.get("gpu_uuid")) throw new Error("Choose a GPU first.");
          await request(`${root}/load`, { method: "POST", body: { gpu_uuid: data.get("gpu_uuid") }, fetcher });
          notice("Load submitted.");
        });
    } else if (action === "unload") {
      openDialog("Unload model",
        `<p class="cloud-hint">Release the resident weights of ${esc(model.name || model.id)}. Files remain on disk.</p>`,
        "Unload", async () => {
          await request(`${root}/unload`, { method: "POST", body: {}, fetcher });
          notice("Unload submitted.");
        });
    } else if (action === "remove") {
      const huggingface = model.source_kind === "huggingface";
      openDialog("Remove deployment",
        `<p class="cloud-hint">Remove ${esc(model.name || model.id)} from this server.</p>`
        + (huggingface ? '<label class="cloud-check"><input type="checkbox" name="delete_files"/> Delete managed weights from disk</label>' : '<p class="cloud-hint">External directories are preserved.</p>'),
        "Remove", async (data) => {
          const deleteFiles = data.get("delete_files") === "on";
          await request(`${root}?delete_files=${deleteFiles}`, { method: "DELETE", fetcher });
          notice("Deployment removed.");
        });
    }
  }

  function bind() {
    $("cloud-refresh")?.addEventListener("click", () => void refresh({ manual: true }));
    $("cloud-host-add")?.addEventListener("click", addHostDialog);
    $("cloud-model-add")?.addEventListener("click", addModelDialog);
    $("cloud-host-select")?.addEventListener("change", (event) => {
      state.hostId = event.target.value;
      state.generation += 1;
      state.connected = false;
      state.gpus = [];
      state.models = [];
      state.cloudJobs = [];
      state.health = null;
      notice("");
      render();
      void refresh();
    });
    $("cloud-host-actions")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-cloud-host-action]");
      if (button && !button.disabled) void hostAction(button.dataset.cloudHostAction);
    });
    $("cloud-model-list")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-cloud-model-action]");
      if (button && !button.disabled) void modelAction(button.dataset.cloudModelAction, button.dataset.id);
    });
    $("cloud-dialog-close")?.addEventListener("click", closeDialog);
    $("cloud-dialog-cancel")?.addEventListener("click", closeDialog);
    $("cloud-log-close")?.addEventListener("click", () => {
      const dialog = $("cloud-log-dialog");
      if (dialog && typeof dialog.close === "function" && dialog.open) dialog.close();
      else dialog?.removeAttribute("open");
    });
    $("cloud-dialog-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!state.dialogHandler) return;
      const submit = $("cloud-dialog-submit");
      const errorNode = $("cloud-dialog-error");
      if (submit?.disabled) return;
      const data = new FormData(event.currentTarget);
      if (submit) submit.disabled = true;
      if (errorNode) errorNode.hidden = true;
      try {
        await state.dialogHandler(data);
        closeDialog();
        await refresh();
      } catch (error) {
        if (errorNode) { errorNode.textContent = error.message || String(error); errorNode.hidden = false; }
      } finally {
        if (submit) submit.disabled = false;
      }
    });
  }

  function visible() {
    const panel = $("cloud-panel");
    return Boolean(panel) && !panel.hidden && !doc.hidden;
  }

  function start() {
    if (state.timer) return;
    state.timer = setInterval(() => { if (visible()) void refresh(); }, 5000);
  }

  function stop() {
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
  }

  function init() {
    bind();
    render();
    if (visible()) void refresh();
    start();
    // The panel is selected through app.js tabs; refresh immediately when shown.
    $("side-tab-cloud")?.addEventListener("click", () => void refresh());
    return api;
  }

  return { init, refresh, render, state, stop, openDialog, closeDialog };
}

if (typeof document !== "undefined" && document.getElementById("cloud-panel")) {
  const panel = createCloudPanel(document);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => panel.init());
  else panel.init();
  if (typeof window !== "undefined") window.CloudPanel = panel;
}
