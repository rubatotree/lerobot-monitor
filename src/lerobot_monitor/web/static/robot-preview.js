import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { createPreviewScheduler, advancePose } from "./preview-scheduler.js";
import URDFLoader from "./vendor/urdf/URDFLoader.js";

const BASE = (document.documentElement.dataset.base || "/lerobot").replace(/\/$/, "");
const SOURCE_KEY = "lerobot-monitor-preview-source";
const POWER_KEY = "lerobot-monitor-preview-power";
const WRIST_CORNER_KEY = "lerobot-monitor-preview-wrist-corner";
const AUTO_SLIDER_HOLD_MS = 800;

const state = {
  initialized: false,
  powered: false,
  renderer: null,
  scene: null,
  camera: null,
  controls: null,
  wristCamera: null,
  wristRenderer: null,
  wristCorner: "bottom-right",
  robot: null,
  model: null,
  models: [],
  activeModelId: "",
  status: {},
  timeline: {},
  sliderPose: null,
  sliderAt: 0,
  focus: null,
  targetPose: {},
  displayPose: {},
  scheduler: null,
  sliderTimer: null,
  viewportWidth: 0,
  viewportHeight: 0,
  powerGeneration: 0,
  resizeObserver: null,
  stageVisible: true,
  wristVisible: false,
  visibilityObserver: null,
  visibilityHandler: null,
  pageVisible: !document.hidden,
  loadGeneration: 0,

};

function $(id) {
  return document.getElementById(id);
}

function finitePose(pose) {
  if (!pose || typeof pose !== "object") return null;
  const out = {};
  Object.entries(pose).forEach(([name, value]) => {
    const number = Number(value);
    if (Number.isFinite(number)) out[name] = number;
  });
  return Object.keys(out).length ? out : null;
}

function readStorage(key, fallback = "") {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function writeStorage(key, value) {
  try {
    localStorage.setItem(key, String(value));
  } catch { /* storage is optional */ }
}

function api(path, body, method = "POST") {
  const options = { method };
  if (body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  return fetch(BASE + path, options).then(async (response) => {
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.error || data.detail) {
      const detail = data.error || data.detail || response.statusText;
      throw new Error(typeof detail === "string" ? detail : "request failed");
    }
    return data;
  });
}

function setStatusText(message, error = false) {
  const node = $("preview-model-status");
  if (!node) return;
  node.textContent = message || "";
  node.classList.toggle("error", Boolean(error));
}

function setPowerButton() {
  const button = $("preview-power");
  if (!button) return;
  button.classList.toggle("on", state.powered);
  button.setAttribute("aria-pressed", String(state.powered));
  button.title = state.powered ? "Turn virtual arm off" : "Turn virtual arm on";
  const panel = $("arm-preview");
  if (panel) panel.classList.toggle("preview-off", !state.powered);
}

function setAutoBadge(label) {
  const badge = $("preview-auto-badge");
  if (!badge) return;
  const source = $("preview-source")?.value || "auto";
  badge.hidden = source !== "auto";
  badge.textContent = source === "auto" ? `AUTO · ${label || "follower"}` : "";
}

function livePose(name) {
  if (name === "commanded_action") return finitePose(state.status.action);
  if (name === "prediction") {
    const actions = state.status.prediction?.actions;
    return finitePose(Array.isArray(actions) ? actions[0] : null);
  }
  return finitePose(state.status.joints);
}

function timelinePose(name) {
  if (!state.timeline.active) return null;
  if (name === "joint_state") return finitePose(state.timeline.jointState);
  if (name === "commanded_action") return finitePose(state.timeline.commandedAction);
  if (name === "prediction") return finitePose(state.timeline.predictionAction);
  return null;
}

function fallbackPose(name) {
  const slider = finitePose(state.sliderPose);
  const follower = livePose("joint_state");
  if (name === "joints") return slider || follower;
  if (name === "prediction") {
    return timelinePose("prediction")
      || timelinePose("commanded_action")
      || timelinePose("joint_state")
      || follower
      || slider;
  }
  if (name === "commanded_action") {
    return timelinePose("commanded_action")
      || timelinePose("joint_state")
      || follower
      || slider;
  }
  if (name === "joint_state") {
    return timelinePose("joint_state") || follower || slider;
  }
  return follower || slider;
}

function autoPose() {
  const focus = state.focus;
  const freshSlider = Date.now() - state.sliderAt <= AUTO_SLIDER_HOLD_MS;
  if (focus?.kind === "joint_slider" || freshSlider) {
    return { pose: finitePose(focus?.pose) || finitePose(state.sliderPose), label: "joints" };
  }
  if (focus?.kind === "action_chart") {
    return {
      pose: finitePose(focus.pose),
      label: focus.source === "prediction" ? "prediction" : "action",
    };
  }
  if (focus?.kind === "joint_chart") {
    return { pose: finitePose(focus.pose), label: "state" };
  }
  if (focus?.kind === "main_camera") {
    return { pose: livePose("joint_state"), label: "follower" };
  }
  return { pose: livePose("joint_state"), label: "follower" };
}

function resolvedPose() {
  const source = $("preview-source")?.value || "auto";
  if (source === "auto") {
    const result = autoPose();
    setAutoBadge(result.label);
    return result.pose || fallbackPose("joint_state");
  }
  setAutoBadge("");
  return fallbackPose(source);
}

function disposeObject(object) {
  const geometries = new Set();
  const materials = new Set();
  const textures = new Set();
  object?.traverse((child) => {
    if (child.geometry) geometries.add(child.geometry);
    for (const material of [].concat(child.material || [])) materials.add(material);
  });
  for (const material of materials) {
    for (const value of Object.values(material)) if (value?.isTexture) textures.add(value);
    material.dispose();
  }
  for (const texture of textures) texture.dispose();
  for (const geometry of geometries) geometry.dispose();
}

function fitCameraToObject() {
  if (!state.robot || !state.camera || !state.controls) return;
  const box = new THREE.Box3().setFromObject(state.robot);
  if (box.isEmpty()) return;
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  const radius = Math.max(size.x, size.y, size.z, 0.2);
  state.controls.target.copy(center);
  const direction = new THREE.Vector3(1.25, 1.05, 1.4).normalize();
  state.camera.position.copy(center).addScaledVector(direction, radius * 2.4);
  state.camera.near = Math.max(radius / 200, 0.001);
  state.camera.far = Math.max(radius * 100, 20);
  state.camera.updateProjectionMatrix();
  state.controls.update();
  scheduleFrame({ main: true });
}

function setView(name) {
  if (!state.robot || !state.camera || !state.controls) return;
  const box = new THREE.Box3().setFromObject(state.robot);
  if (box.isEmpty()) return;
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  const radius = Math.max(size.x, size.y, size.z, 0.2);
  const distance = radius * 2.4;
  const directions = {
    iso: new THREE.Vector3(1.25, 1.05, 1.4),
    front: new THREE.Vector3(0, 0, 1),
    side: new THREE.Vector3(1, 0, 0),
    top: new THREE.Vector3(0, 1, 0),
  };
  const direction = (directions[name] || directions.iso).normalize();
  state.controls.target.copy(center);
  state.camera.position.copy(center).addScaledVector(direction, distance);
  state.camera.updateProjectionMatrix();
  state.controls.update();
  scheduleFrame({ main: true });
}

function focusObject(object) {
  if (!object || !state.camera || !state.controls) return;
  const box = new THREE.Box3().setFromObject(object);
  if (box.isEmpty()) return;
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  const radius = Math.max(size.length() * 0.65, 0.08);
  const direction = state.camera.position.clone().sub(state.controls.target).normalize();
  state.controls.target.copy(center);
  state.camera.position.copy(center).addScaledVector(direction, radius * 2.1);
  state.controls.update();
  scheduleFrame({ main: true });
}

function attachPicking(canvas) {
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  canvas.addEventListener("dblclick", (event) => {
    if (!state.robot) return;
    const rect = canvas.getBoundingClientRect();
    pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, state.camera);
    const hit = raycaster.intersectObject(state.robot, true)[0];
    if (hit?.object) focusObject(hit.object);
  });
}

function initViewport() {
  if (state.renderer) return;
  const host = $("preview-stage");
  if (!host) return;
  const renderer = new THREE.WebGLRenderer({
    antialias: false,
    alpha: true,
    powerPreference: "low-power",
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.25));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  host.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(42, 1, 0.01, 100);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = false;
  controls.screenSpacePanning = true;
  controls.minDistance = 0.08;
  controls.maxDistance = 12;
  controls.addEventListener("change", () => scheduleFrame({ main: true }));
  scene.add(new THREE.HemisphereLight(0xe7edff, 0x222637, 2.2));
  const key = new THREE.DirectionalLight(0xffffff, 2.4);
  key.position.set(2.5, 4, 3);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0x7c8cff, 1.1);
  rim.position.set(-3, 1, -2);
  scene.add(rim);
  const grid = new THREE.GridHelper(2, 20, 0x2c3348, 0x1d2232);
  grid.material.transparent = true;
  grid.material.opacity = 0.45;
  scene.add(grid);
  state.renderer = renderer;
  state.scene = scene;
  state.camera = camera;
  state.controls = controls;
  state.scheduler = createPreviewScheduler({
    updatePose: (dt) => advancePose(state.displayPose, state.targetPose, dt, applyJoint),
    renderMain: () => state.renderer.render(state.scene, state.camera),
    renderWrist: () => {
      updateWristCamera();
      state.wristRenderer.render(state.scene, state.wristCamera);
    },
  });
  syncVisibility();
  attachPicking(renderer.domElement);
  state.resizeObserver = new ResizeObserver(() => resize());
  state.resizeObserver.observe(host);
  resize();
  scheduleFrame({ main: true });
}

function initWristRenderer() {
  if (state.wristRenderer) return;
  const canvas = $("preview-wrist-canvas");
  if (!canvas) return;
  state.wristCamera = new THREE.PerspectiveCamera(60, 16 / 9, 0.005, 20);
  state.wristRenderer = new THREE.WebGLRenderer({
    canvas,
    antialias: false,
    alpha: false,
    powerPreference: "low-power",
  });
  state.wristRenderer.setPixelRatio(1);
  state.wristRenderer.setSize(320, 180, false);
  state.wristRenderer.outputColorSpace = THREE.SRGBColorSpace;
}

function resize() {
  if (!state.renderer || !state.camera) return;
  const host = $("preview-stage");
  if (!host) return;
  const width = Math.max(1, host.clientWidth);
  const height = Math.max(1, host.clientHeight);
  const pixelRatio = Math.min(window.devicePixelRatio || 1, 1.25);
  if (state.viewportWidth === width && state.viewportHeight === height
      && state.renderer.getPixelRatio() === pixelRatio) return;
  state.viewportWidth = width;
  state.viewportHeight = height;
  if (state.renderer.getPixelRatio() !== pixelRatio) state.renderer.setPixelRatio(pixelRatio);
  state.renderer.setSize(width, height, false);
  state.camera.aspect = width / height;
  state.camera.updateProjectionMatrix();
  scheduleFrame({ main: true });
}

function applyJoint(name, value) {
  if (!state.robot || !state.model) return;
  const urdfName = state.model.joint_map?.[name] || name;
  const joint = state.robot.joints?.[urdfName];
  if (!joint) return;
  const number = Number(value);
  if (!Number.isFinite(number)) return;
  if (String(urdfName).toLowerCase().includes("gripper") && joint.limit) {
    const lower = Number(joint.limit.lower);
    const upper = Number(joint.limit.upper);
    if (Number.isFinite(lower) && Number.isFinite(upper) && upper !== lower) {
      joint.setJointValue(lower + (Math.max(0, Math.min(100, number)) / 100) * (upper - lower));
      return;
    }
  }
  joint.setJointValue(THREE.MathUtils.degToRad(number));
}

function applyPose(pose) {
  if (!pose) return;
  Object.entries(pose).forEach(([name, value]) => applyJoint(name, value));
}

const wristScratch = {
  box: new THREE.Box3(),
  mount: new THREE.Vector3(),
  target: new THREE.Vector3(),
  forward: new THREE.Vector3(),
  size: new THREE.Vector3(),
};

function updateWristCamera() {
  if (!state.robot || !state.wristCamera || !state.model) return;
  const mount = state.robot.links?.[state.model.wrist_camera?.link];
  const lookAt = state.robot.links?.[state.model.wrist_camera?.look_at_link];
  if (!mount || !lookAt) return;
  const scratch = wristScratch;
  scratch.box.setFromObject(mount);
  if (scratch.box.isEmpty()) {
    mount.getWorldPosition(scratch.mount);
    scratch.size.set(0, 0, 0);
  } else {
    scratch.box.getCenter(scratch.mount);
    scratch.box.getSize(scratch.size);
  }
  lookAt.getWorldPosition(scratch.target);
  scratch.forward.copy(scratch.mount).sub(scratch.target);
  if (scratch.forward.lengthSq() < 1e-10) scratch.forward.set(0, 0, -1);
  scratch.forward.normalize();
  const standoff = Math.max(0.12, scratch.size.length() * 1.4);
  state.wristCamera.position.copy(scratch.mount).addScaledVector(scratch.forward, standoff);
  state.wristCamera.up.set(0, 1, 0);
  state.wristCamera.lookAt(scratch.target.addScaledVector(scratch.forward, 0.08));
}

function syncVisibility() {
  state.scheduler?.setVisibility({
    enabled: state.powered && state.pageVisible,
    main: state.stageVisible,
    wrist: state.wristVisible && Boolean(state.wristRenderer)
      && Boolean($("preview-wrist")?.classList.contains("on")),
  });
}

function scheduleFrame(dirty = {}) {
  if (!state.powered || !state.scheduler) return;
  const target = resolvedPose();
  let changed = false;
  if (target && state.robot) {
    for (const [name, value] of Object.entries(target)) {
      if (state.targetPose[name] !== value) {
        state.targetPose[name] = value;
        changed = true;
      }
    }
  }
  state.scheduler.invalidate({ ...dirty, pose: changed });
}

function clearSliderTimer() {
  if (state.sliderTimer !== null) clearTimeout(state.sliderTimer);
  state.sliderTimer = null;
}

function scheduleSliderExpiry() {
  clearSliderTimer();
  const remaining = AUTO_SLIDER_HOLD_MS - (Date.now() - state.sliderAt);
  if (!state.powered || !state.pageVisible || remaining < 0) return;
  // Auto must fall back after the hold even when no status packets arrive.
  state.sliderTimer = setTimeout(() => {
    state.sliderTimer = null;
    scheduleFrame();
  }, remaining + 1);
}

function disposeViewport() {
  ++state.loadGeneration;
  state.scheduler?.dispose();
  state.scheduler = null;
  clearSliderTimer();
  state.viewportWidth = state.viewportHeight = 0;
  state.resizeObserver?.disconnect();
  state.resizeObserver = null;
  disposeObject(state.scene);
  state.robot = null;
  state.model = null;
  state.controls?.dispose();
  state.controls = null;
  if (state.renderer) {
    state.renderer.dispose();
    state.renderer.forceContextLoss?.();
    state.renderer.domElement.remove();
  }
  state.renderer = null;
  state.scene = null;
  state.camera = null;
  if (state.wristRenderer) {
    state.wristRenderer.dispose();
    state.wristRenderer.forceContextLoss?.();
  }
  state.wristRenderer = null;
  state.wristCamera = null;
}

async function loadModel(modelId) {
  if (!state.powered || !state.scene) return;
  const model = state.models.find((row) => row.id === modelId);
  if (!model) throw new Error(`robot model '${modelId}' is not installed`);
  const generation = ++state.loadGeneration;
  setStatusText(`loading ${model.name || model.id}…`);
  const baseUrl = `${BASE}/api/robot-models/${encodeURIComponent(model.id)}/files/`;
  const packages = {};
  Object.entries(model.packages || {}).forEach(([name, value]) => {
    packages[name] = `${baseUrl}${String(value).replace(/^\/+/, "")}`.replace(/\/$/, "");
  });
  const manager = new THREE.LoadingManager();
  let failed = false;
  const complete = new Promise((resolve) => { manager.onLoad = resolve; });
  manager.onError = () => { failed = true; };
  const loader = new URDFLoader(manager);
  loader.packages = packages;
  const loaded = await loader.loadAsync(`${baseUrl}${model.urdf}`);
  // loadAsync resolves the URDF tree before its mesh and texture requests end.
  // Keep it detached until all resources arrive, including stale requests.
  await complete;
  if (generation !== state.loadGeneration || !state.powered || !state.scene) {
    disposeObject(loaded);
    return;
  }
  if (failed) {
    disposeObject(loaded);
    throw new Error("Some robot model files could not be loaded");
  }
  if (state.robot) {
    state.scene.remove(state.robot);
    disposeObject(state.robot);
  }
  loaded.rotation.x = -Math.PI / 2;
  state.scene.add(loaded);
  state.robot = loaded;
  state.model = model;
  state.targetPose = finitePose(model.default_pose) || finitePose(state.status.joints) || {};
  state.displayPose = { ...state.targetPose };
  applyPose(state.displayPose);
  fitCameraToObject();
  setStatusText(model.missing ? "model files missing" : "");
  scheduleFrame({ main: true, wrist: true });
}

function renderModelOptions() {
  const select = $("preview-model");
  if (!select) return;
  select.innerHTML = "";
  state.models.forEach((model) => {
    const option = document.createElement("option");
    option.value = model.id;
    option.textContent = `${model.name || model.id}${model.missing ? " · missing" : ""}`;
    option.disabled = Boolean(model.missing);
    select.appendChild(option);
  });
  if (state.activeModelId) select.value = state.activeModelId;
  const active = state.models.find((row) => row.id === state.activeModelId);
  const update = $("preview-model-update");
  const remove = $("preview-model-delete");
  if (update) update.disabled = !active || Boolean(active.builtin);
  if (remove) remove.disabled = !active || Boolean(active.builtin);
}

async function refreshModels(preferred = "") {
  state.models = await api("/api/robot-models", undefined, "GET");
  const wanted = preferred || state.activeModelId || readStorage("lerobot-monitor-robot-model");
  const active = state.models.find((row) => row.id === wanted && !row.missing)
    || state.models.find((row) => row.active && !row.missing)
    || state.models.find((row) => !row.missing);
  state.activeModelId = active?.id || "";
  renderModelOptions();
  return active;
}

async function activateModel(modelId, load = true) {
  if (!modelId) return;
  const row = await api("/api/robot-models/active", { id: modelId });
  state.activeModelId = row.id;
  writeStorage("lerobot-monitor-robot-model", row.id);
  renderModelOptions();
  if (load && state.powered && state.renderer) await loadModel(row.id);
}

async function alignToConnectedRobot() {
  const robot = state.status.robot || {};
  if (!robot.connected || robot.virtual || !robot.type) return;
  const match = state.models.find(
    (row) => !row.missing && (row.robot_types || []).includes(robot.type),
  );
  if (match && match.id !== state.activeModelId) {
    await activateModel(match.id, true);
    setStatusText(`aligned to ${robot.type}`);
  } else if (!match) {
    setStatusText(`no installed model for ${robot.type}`, true);
  }
}

async function setPower(enabled, persist = true) {
  const next = Boolean(enabled);
  if (next === state.powered) return;
  const generation = ++state.powerGeneration;
  state.powered = next;
  if (persist) writeStorage(POWER_KEY, next ? "1" : "0");
  setPowerButton();
  if (!next) {
    disposeViewport();
    $("preview-wrist")?.classList.remove("on");
    $("preview-wrist-toggle")?.setAttribute("aria-pressed", "false");
    await api("/api/virtual-follower/disconnect").catch(() => {});
    return;
  }
  initViewport();
  try {
    await api("/api/virtual-follower/connect", { model_id: state.activeModelId });
    if (!state.powered || generation !== state.powerGeneration) return;
    await loadModel(state.activeModelId);
    scheduleSliderExpiry();
  } catch (error) {
    if (generation !== state.powerGeneration) return;
    setStatusText(error.message || String(error), true);
  }
}

function toggleWrist() {
  if (!state.powered) return;
  const panel = $("preview-wrist");
  if (!panel) return;
  const enabled = !panel.classList.contains("on");
  panel.classList.toggle("on", enabled);
  $("preview-wrist-toggle")?.setAttribute("aria-pressed", String(enabled));
  if (enabled) initWristRenderer();
  syncVisibility();
  if (enabled) scheduleFrame({ wrist: true });
}

function cycleWristCorner() {
  const corners = ["bottom-right", "bottom-left", "top-right", "top-left"];
  const index = (corners.indexOf(state.wristCorner) + 1) % corners.length;
  state.wristCorner = corners[index];
  writeStorage(WRIST_CORNER_KEY, state.wristCorner);
  const panel = $("preview-wrist");
  if (panel) {
    panel.dataset.corner = state.wristCorner;
  }
}

function bindControls() {
  $("preview-power")?.addEventListener("click", () => {
    setPower(!state.powered).catch((error) => setStatusText(error.message, true));
  });
  $("preview-source")?.addEventListener("change", (event) => {
    writeStorage(SOURCE_KEY, event.target.value);
    scheduleFrame();
  });
  $("preview-model")?.addEventListener("change", (event) => {
    activateModel(event.target.value).catch((error) => setStatusText(error.message, true));
  });
  $("preview-wrist-toggle")?.addEventListener("click", toggleWrist);
  $("preview-wrist-corner")?.addEventListener("click", () => {
    cycleWristCorner();
  });
  ["iso", "front", "side", "top"].forEach((view) => {
    $(`preview-view-${view}`)?.addEventListener("click", () => setView(view));
  });
  $("preview-view-reset")?.addEventListener("click", fitCameraToObject);
  $("preview-model-install")?.addEventListener("click", async () => {
    const input = $("preview-model-source");
    const remote = input?.value.trim();
    if (!remote) return;
    setStatusText("downloading robot model…");
    try {
      const row = await api("/api/robot-models", { remote });
      await refreshModels(row.id);
      await activateModel(row.id, true);
      input.value = "";
      setStatusText(`installed ${row.name || row.id}`);
    } catch (error) {
      setStatusText(error.message || String(error), true);
    }
  });
  $("preview-model-update")?.addEventListener("click", async () => {
    if (!state.activeModelId) return;
    setStatusText("updating robot model…");
    try {
      await api(`/api/robot-models/${encodeURIComponent(state.activeModelId)}/update`);
      await refreshModels(state.activeModelId);
      await loadModel(state.activeModelId);
      setStatusText("model updated");
    } catch (error) {
      setStatusText(error.message || String(error), true);
    }
  });
  $("preview-model-delete")?.addEventListener("click", async () => {
    if (!state.activeModelId) return;
    const row = state.models.find((item) => item.id === state.activeModelId);
    if (!row || row.builtin) return;
    try {
      await api(`/api/robot-models/${encodeURIComponent(row.id)}`, undefined, "DELETE");
      await refreshModels();
      await activateModel(state.activeModelId, true);
      setStatusText("model deleted");
    } catch (error) {
      setStatusText(error.message || String(error), true);
    }
  });
}

function observeVisibility() {
  if (typeof IntersectionObserver === "function") {
    state.visibilityObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.target.id === "preview-stage") state.stageVisible = entry.isIntersecting;
        if (entry.target.id === "preview-wrist") state.wristVisible = entry.isIntersecting;
      }
      syncVisibility();
      scheduleFrame();
    }, { threshold: 0.01 });
    for (const id of ["preview-stage", "preview-wrist"]) {
      if ($(id)) state.visibilityObserver.observe($(id));
    }
  } else {
    state.wristVisible = true;
  }
  state.visibilityHandler = () => {
    state.pageVisible = !document.hidden;
    syncVisibility();
    if (state.pageVisible) {
      resize();
      scheduleFrame();
      scheduleSliderExpiry();
    } else clearSliderTimer();
  };
  document.addEventListener("visibilitychange", state.visibilityHandler);
  window.addEventListener("resize", resize);
}

export const RobotPreview = {
  async init() {
    if (state.initialized) return;
    state.initialized = true;
    const source = readStorage(SOURCE_KEY, "auto");
    const sourceSelect = $("preview-source");
    if (sourceSelect) sourceSelect.value = source;
    state.wristCorner = readStorage(WRIST_CORNER_KEY, "bottom-right");
    const wrist = $("preview-wrist");
    if (wrist) wrist.dataset.corner = state.wristCorner;
    bindControls();
    observeVisibility();
    await refreshModels();
    state.status = await api("/api/status", undefined, "GET").catch(() => state.status);
    await alignToConnectedRobot().catch(() => {});
    const startPowered = readStorage(POWER_KEY, "1") !== "0";
    state.powered = false;
    setPowerButton();
    if (startPowered) await setPower(true, false);
  },

  setStatus(status) {
    state.status = status || {};
    alignToConnectedRobot().catch(() => {});
    if (state.powered) scheduleFrame();
  },

  setTimelineFrame(frame) {
    state.timeline = frame || {};
    if (state.powered) scheduleFrame();
  },

  setSliderTargets(pose) {
    state.sliderPose = finitePose(pose);
    state.sliderAt = Date.now();
    scheduleSliderExpiry();
    if (state.powered) scheduleFrame();
  },

  setFocusContext(kind, payload = {}) {
    if (!kind) state.focus = null;
    else state.focus = { kind, ...payload };
    if (state.powered) scheduleFrame();
  },

  setPower(enabled) {
    return setPower(enabled);
  },

  resize,

  debugInfo() {
    return {
      ...state.scheduler?.debugInfo(),
      powered: state.powered,
      pendingFrames: state.scheduler?.debugInfo().pendingFrames || 0,
      pixelRatio: state.renderer?.getPixelRatio() || 0,
      antialias: state.renderer?.getContext().getContextAttributes()?.antialias ?? false,
      wristAntialias: state.wristRenderer?.getContext().getContextAttributes()?.antialias ?? false,
      displayPose: { ...state.displayPose },
      targetPose: { ...state.targetPose },
      modelId: state.activeModelId,
      joints: Object.keys(state.robot?.joints || {}).length,
      renderCalls: Number(state.renderer?.info?.render?.calls) || 0,
      wristRenderCalls: Number(state.wristRenderer?.info?.render?.calls) || 0,
      canvasWidth: Number(state.renderer?.domElement?.width) || 0,
      canvasHeight: Number(state.renderer?.domElement?.height) || 0,
    };
  },

  destroy() {
    ++state.powerGeneration;
    state.powered = false;
    setPowerButton();
    state.visibilityObserver?.disconnect();
    document.removeEventListener("visibilitychange", state.visibilityHandler);
    window.removeEventListener("resize", resize);
    disposeViewport();
  },
};

window.RobotPreview = RobotPreview;
RobotPreview.init().catch((error) => setStatusText(error.message || String(error), true));
