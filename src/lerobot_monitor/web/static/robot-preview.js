import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import URDFLoader from "./vendor/urdf/URDFLoader.js";

const BASE = (document.documentElement.dataset.base || "/lerobot").replace(/\/$/, "");
const SOURCE_KEY = "lerobot-monitor-preview-source";
const POWER_KEY = "lerobot-monitor-preview-power";
const WRIST_CORNER_KEY = "lerobot-monitor-preview-wrist-corner";
const AUTO_SLIDER_HOLD_MS = 800;
const VIEWPORT_FPS = 30;
const WRIST_FPS = 10;
const JOINT_FALLBACK = [
  "shoulder_pan", "shoulder_lift", "elbow_flex",
  "wrist_flex", "wrist_roll", "gripper",
];

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
  lastFrameAt: 0,
  lastWristAt: 0,
  raf: 0,
  resizeObserver: null,
  stageVisible: true,
  pageVisible: !document.hidden,
  loadGeneration: 0,
  loadError: "",
  activeUntil: 0,
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

function disposeMaterial(material) {
  if (!material) return;
  const materials = Array.isArray(material) ? material : [material];
  materials.forEach((item) => {
    Object.values(item).forEach((value) => {
      if (value && value.isTexture) value.dispose();
    });
    item.dispose?.();
  });
}

function disposeObject(object) {
  object?.traverse((child) => {
    child.geometry?.dispose?.();
    disposeMaterial(child.material);
  });
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
  renderFrame(true);
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
  renderFrame(true);
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
  renderFrame(true);
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
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.screenSpacePanning = true;
  controls.minDistance = 0.08;
  controls.maxDistance = 12;
  controls.addEventListener("change", () => scheduleFrame(true));
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
  attachPicking(renderer.domElement);
  state.resizeObserver = new ResizeObserver(() => resize());
  state.resizeObserver.observe(host);
  resize();
  scheduleFrame(true);
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
  state.renderer.setSize(width, height, false);
  state.camera.aspect = width / height;
  state.camera.updateProjectionMatrix();
  renderFrame(true);
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

function updateWristCamera() {
  if (!state.robot || !state.wristCamera || !state.model) return;
  const mount = state.robot.links?.[state.model.wrist_camera?.link];
  const lookAt = state.robot.links?.[state.model.wrist_camera?.look_at_link];
  if (!mount || !lookAt) return;
  const mountBox = new THREE.Box3().setFromObject(mount);
  const mountPosition = mountBox.isEmpty()
    ? mount.getWorldPosition(new THREE.Vector3())
    : mountBox.getCenter(new THREE.Vector3());
  const targetPosition = lookAt.getWorldPosition(new THREE.Vector3());
  const forward = mountPosition.clone().sub(targetPosition);
  if (forward.lengthSq() < 1e-10) forward.set(0, 0, -1);
  forward.normalize();
  const mountSize = mountBox.isEmpty() ? new THREE.Vector3() : mountBox.getSize(new THREE.Vector3());
  const standoff = Math.max(0.12, mountSize.length() * 1.4);
  state.wristCamera.position.copy(mountPosition).addScaledVector(forward, standoff);
  state.wristCamera.up.set(0, 1, 0);
  state.wristCamera.lookAt(targetPosition.clone().addScaledVector(forward, 0.08));
}

function renderFrame(force = false) {
  if (!state.powered || !state.renderer || !state.scene || !state.camera) return;
  if (!state.pageVisible || !state.stageVisible) return;
  const now = performance.now();
  if (!force && now - state.lastFrameAt < 1000 / VIEWPORT_FPS) return;
  state.lastFrameAt = now;
  const target = resolvedPose();
  if (target) state.targetPose = target;
  if (Object.keys(state.targetPose).length) {
    const alpha = force ? 1 : 0.28;
    Object.entries(state.targetPose).forEach(([name, value]) => {
      const previous = Number(state.displayPose[name]);
      state.displayPose[name] = Number.isFinite(previous)
        ? previous + (Number(value) - previous) * alpha
        : Number(value);
      applyJoint(name, state.displayPose[name]);
    });
  }
  state.controls?.update();
  state.renderer.render(state.scene, state.camera);
  if (state.wristRenderer && state.wristCamera && $("preview-wrist")?.classList.contains("on")) {
    if (force || now - state.lastWristAt >= 1000 / WRIST_FPS) {
      state.lastWristAt = now;
      updateWristCamera();
      state.wristRenderer.render(state.scene, state.wristCamera);
    }
  }
}

function scheduleFrame(force = false) {
  if (!state.powered) return;
  const wristVisible = $("preview-wrist")?.classList.contains("on");
  if (!force && !state.raf && !wristVisible) {
    const next = resolvedPose();
    if (next && state.targetPose && Object.keys(state.targetPose).length) {
      const unchanged = Object.entries(next).every(
        ([name, value]) => Math.abs(Number(value) - Number(state.targetPose[name])) < 1e-4,
      );
      if (unchanged) return;
    }
  }
  const now = performance.now();
  state.activeUntil = Math.max(state.activeUntil, now + (force ? 500 : 180));
  if (state.raf) return;
  const tick = () => {
    state.raf = 0;
    const now = performance.now();
    const active = now < state.activeUntil;
    const wristVisible = Boolean($("preview-wrist")?.classList.contains("on"));
    renderFrame(force || active);
    if (!state.powered) return;
    if (active) state.raf = requestAnimationFrame(tick);
    else if (wristVisible) state.raf = setTimeout(tick, 1000 / WRIST_FPS);
  };
  state.raf = requestAnimationFrame(tick);
}

function stopFrameLoop() {
  if (state.raf) {
    cancelAnimationFrame(state.raf);
    clearTimeout(state.raf);
  }
  state.raf = 0;
}

function disposeViewport() {
  stopFrameLoop();
  state.resizeObserver?.disconnect();
  state.resizeObserver = null;
  if (state.robot) disposeObject(state.robot);
  state.robot = null;
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
  manager.onLoad = () => {
    if (generation !== state.loadGeneration || !state.powered) return;
    requestAnimationFrame(() => {
      fitCameraToObject();
      renderFrame(true);
    });
  };
  const loader = new URDFLoader(manager);
  loader.packages = packages;
  const loaded = await loader.loadAsync(`${baseUrl}${model.urdf}`);
  if (generation !== state.loadGeneration) {
    disposeObject(loaded);
    return;
  }
  if (state.robot) {
    state.scene.remove(state.robot);
    disposeObject(state.robot);
  }
  loaded.rotation.x = -Math.PI / 2;
  loaded.traverse((child) => {
    if (child.isMesh) {
      child.castShadow = false;
      child.receiveShadow = false;
      if (child.material) {
        child.material.side = THREE.DoubleSide;
        child.material.needsUpdate = true;
      }
    }
  });
  state.scene.add(loaded);
  state.robot = loaded;
  state.model = model;
  state.displayPose = {};
  state.targetPose = finitePose(model.default_pose) || finitePose(state.status.joints) || {};
  applyPose(state.targetPose);
  requestAnimationFrame(() => fitCameraToObject());
  setStatusText(model.missing ? "model files missing" : "");
  renderFrame(true);
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
  state.powered = next;
  if (persist) writeStorage(POWER_KEY, next ? "1" : "0");
  setPowerButton();
  if (!next) {
    stopFrameLoop();
    disposeViewport();
    $("preview-wrist")?.classList.remove("on");
    await api("/api/virtual-follower/disconnect").catch(() => {});
    return;
  }
  initViewport();
  try {
    await api("/api/virtual-follower/connect", { model_id: state.activeModelId });
    await loadModel(state.activeModelId);
  } catch (error) {
    setStatusText(error.message || String(error), true);
  }
}

function toggleWrist() {
  const panel = $("preview-wrist");
  if (!panel) return;
  const enabled = !panel.classList.contains("on");
  panel.classList.toggle("on", enabled);
  $("preview-wrist-toggle")?.setAttribute("aria-pressed", String(enabled));
  if (enabled) initWristRenderer();
  renderFrame(true);
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
    renderFrame(true);
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
  const host = $("preview-stage");
  if (host && typeof IntersectionObserver === "function") {
    const observer = new IntersectionObserver((entries) => {
      state.stageVisible = Boolean(entries[0]?.isIntersecting);
      if (state.stageVisible) scheduleFrame(true);
      else stopFrameLoop();
    }, { threshold: 0.01 });
    observer.observe(host);
  }
  document.addEventListener("visibilitychange", () => {
    state.pageVisible = !document.hidden;
    if (state.pageVisible) scheduleFrame(true);
    else stopFrameLoop();
  });
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
    if (state.powered) scheduleFrame(true);
  },

  setFocusContext(kind, payload = {}) {
    if (!kind) state.focus = null;
    else state.focus = { kind, ...payload };
    if (state.powered) scheduleFrame(true);
  },

  setPower(enabled) {
    return setPower(enabled);
  },

  resize,

  debugInfo() {
    return {
      powered: state.powered,
      modelId: state.activeModelId,
      joints: Object.keys(state.robot?.joints || {}).length,
      renderCalls: Number(state.renderer?.info?.render?.calls) || 0,
      wristRenderCalls: Number(state.wristRenderer?.info?.render?.calls) || 0,
      canvasWidth: Number(state.renderer?.domElement?.width) || 0,
      canvasHeight: Number(state.renderer?.domElement?.height) || 0,
    };
  },

  destroy() {
    disposeViewport();
  },
};

window.RobotPreview = RobotPreview;
RobotPreview.init().catch((error) => setStatusText(error.message || String(error), true));
