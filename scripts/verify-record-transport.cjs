// Playwright verification for the record bar layout and lifecycle:
//   1. the bar sits in the camera grid and the camera windows shrink next to it
//      instead of being covered;
//   2. leaving record mode hides the bar even though the server keeps reporting
//      the last (finalizing/saved) session payload.
//
// Starts an isolated monitor with no real cameras, injects a synthetic status
// stream that advertises one camera, and serves a PNG for its MJPEG URL.
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

function loadPlaywright() {
  const candidates = [
    process.env.PLAYWRIGHT_MODULE,
    "playwright",
    "C:/Users/Admin/AppData/Local/npm-cache/_npx/e41f203b7505f1fb/node_modules/playwright",
  ].filter(Boolean);
  for (const candidate of candidates) {
    try { return require(candidate); } catch { /* try next */ }
  }
  throw new Error("Playwright is unavailable; set PLAYWRIGHT_MODULE to an installed package");
}

const { chromium } = loadPlaywright();
const ROOT = path.resolve(__dirname, "..");
const SHOTS = process.env.RECORD_TRANSPORT_SHOTS
  || path.resolve(ROOT, "..", ".agent-progress", "record-transport-shots");
const results = [];

function check(name, ok, detail = "") {
  results.push({ name, ok: !!ok, detail });
  if (!ok) console.log(`FAIL  ${name}${detail ? ` — ${detail}` : ""}`);
}

const FRAME_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAABgAAAAMCAIAAAD3UuoiAAAAG0lEQVR4nGP4z8BAEDE0/CeMRg0aNWjUIHwIABpbZvBsZPkxAAAAAElFTkSuQmCC",
  "base64",
);

const CAMERA = {
  name: "0",
  label: "front",
  enabled: true,
  show_main: true,
  feed_robot: false,
  streaming: true,
  fps: 15,
  port: 5000,
  width: 640,
  height: 480,
  connected: true,
  remote: false,
  device_key: "local:0",
  error: "",
};

function recordSession(overrides = {}) {
  return {
    session_id: "synthetic-record",
    dataset_id: "synthetic-ds",
    phase: "recording",
    paused: false,
    version: 1,
    episode_index: 2,
    episode_number: 3,
    completed: 2,
    target: 5,
    elapsed_s: 4.5,
    duration_s: 20,
    auto_next: false,
    instant_fallback: false,
    pending: 0,
    saved: 2,
    save_status: "idle",
    error: null,
    staging_path: null,
    needs_rerecord: false,
    can_retry: false,
    can_back: true,
    ...overrides,
  };
}

function statusFrame({ mode, displayMode, record }) {
  return {
    ts: Date.now() / 1000,
    mode,
    display_mode: displayMode || mode,
    task: { pending: "", record },
    cameras: [{ ...CAMERA }],
  };
}

function getFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function requestJson(url) {
  return new Promise((resolve, reject) => {
    const request = http.get(url, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { body += chunk; });
      response.on("end", () => {
        if (!response.statusCode || response.statusCode >= 400) {
          reject(new Error(`HTTP ${response.statusCode}: ${body}`));
          return;
        }
        try { resolve(JSON.parse(body)); } catch (error) { reject(error); }
      });
    });
    request.on("error", reject);
    request.setTimeout(5000, () => request.destroy(new Error("request timeout")));
  });
}

async function waitForServer(url, child, logs) {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    if (child.exitCode != null) throw new Error(`server exited (${child.exitCode})\n${logs()}`);
    try {
      await requestJson(url);
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
  }
  throw new Error(`server did not become ready\n${logs()}`);
}

async function stopChild(child) {
  if (!child || child.exitCode != null) return;
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/pid", String(child.pid), "/t", "/f"], { windowsHide: true });
  } else {
    child.kill("SIGTERM");
  }
  await Promise.race([
    new Promise((resolve) => child.once("exit", resolve)),
    new Promise((resolve) => setTimeout(resolve, 3000)),
  ]);
}

async function injectStatusSocket(page, first) {
  await page.addInitScript(({ frame }) => {
    class SyntheticWebSocket {
      constructor(url) {
        this.url = url;
        this.readyState = 0;
        this.onopen = null;
        this.onclose = null;
        this.onerror = null;
        this.onmessage = null;
        window.__emitStatus = (next) => {
          window.__statusEmits = (window.__statusEmits || 0) + 1;
          if (this.onmessage) this.onmessage({ data: JSON.stringify(next) });
        };
        setTimeout(() => {
          if (this.readyState === 3) return;
          this.readyState = 1;
          if (this.onopen) this.onopen({});
          window.__emitStatus(frame);
        }, 0);
      }

      send() {}

      close() {
        this.readyState = 3;
        if (this.onclose) this.onclose({});
      }
    }
    window.WebSocket = SyntheticWebSocket;
  }, { frame: first });
}

async function layoutState(page) {
  return page.evaluate(() => {
    const transport = document.getElementById("record-transport");
    const cameras = document.getElementById("cameras");
    const card = document.querySelector("#cameras .cam-card");
    const cardRect = card ? card.getBoundingClientRect() : null;
    const transportRect = transport ? transport.getBoundingClientRect() : null;
    const cameraRect = cameras.getBoundingClientRect();
    return {
      hidden: transport.classList.contains("hidden"),
      recordOpen: cameras.classList.contains("record-open"),
      position: getComputedStyle(transport).position,
      transport: transportRect
        ? { top: transportRect.top, bottom: transportRect.bottom, height: transportRect.height, width: transportRect.width }
        : null,
      camera: cardRect
        ? { top: cardRect.top, bottom: cardRect.bottom, height: cardRect.height, left: cardRect.left, right: cardRect.right }
        : null,
      camerasBox: { top: cameraRect.top, bottom: cameraRect.bottom },
      phase: document.getElementById("record-phase").textContent,
      title: document.getElementById("record-title").textContent,
    };
  });
}

async function emit(page, frame) {
  await page.evaluate((next) => window.__emitStatus(next), frame);
  await page.waitForTimeout(160);
}

async function runChecks(browser, origin) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  const errors = [];
  const failedResponses = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400) failedResponses.push(`${response.status()} ${response.url()}`);
  });
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.route("**/camera/0*", (route) => route.fulfill({
    status: 200,
    contentType: "image/png",
    body: FRAME_PNG,
  }));
  await page.route("**/api/cameras/0/resolutions*", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: "[]",
  }));
  await injectStatusSocket(page, statusFrame({ mode: "idle", record: null }));
  await page.goto(`${origin}/lerobot/?v=${Date.now()}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#cameras .cam-card", { timeout: 30000 });
  await page.waitForFunction(() => window.__statusEmits > 0, null, { timeout: 10000 });
  await page.waitForFunction(
    () => document.querySelector("#cameras .cam-card img")?.classList.contains("live"),
    null,
    { timeout: 15000 },
  );

  const idle = await layoutState(page);
  check("idle hides the record bar", idle.hidden && !idle.recordOpen, JSON.stringify(idle));
  check("record bar is not an overlay any more", idle.position === "static", JSON.stringify(idle));
  check("idle camera window uses the whole area", !!idle.camera, JSON.stringify(idle));
  const idleHeight = idle.camera ? idle.camera.height : 0;

  await emit(page, statusFrame({ mode: "record", record: recordSession() }));
  const recording = await layoutState(page);
  check("record bar shows during a record session",
    !recording.hidden && recording.recordOpen, JSON.stringify(recording));
  check("record bar sits above the camera window",
    recording.camera && recording.transport
      && recording.camera.top >= recording.transport.bottom - 1,
    JSON.stringify(recording));
  check("record bar stays inside the camera area",
    recording.transport && recording.transport.bottom <= recording.camerasBox.bottom + 1,
    JSON.stringify(recording));
  check("camera window shrinks instead of being covered",
    recording.camera && recording.camera.height < idleHeight - 20,
    JSON.stringify({ idleHeight, recordingHeight: recording.camera && recording.camera.height }));
  check("record bar reports the session",
    recording.title.includes("synthetic-ds") && recording.phase.includes("Recording"),
    JSON.stringify({ title: recording.title, phase: recording.phase }));
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, "record-open.png") });

  // Stop keeps the last session payload on the wire in idle mode: the bar must go.
  await emit(page, statusFrame({
    mode: "idle",
    displayMode: "hold",
    record: recordSession({ phase: "completed", version: 2, saved: 3, pending: 0 }),
  }));
  const stopped = await layoutState(page);
  check("leaving record mode hides the bar", stopped.hidden && !stopped.recordOpen, JSON.stringify(stopped));
  check("camera window reclaims the freed space",
    stopped.camera && stopped.camera.height > idleHeight - 2,
    JSON.stringify({ idleHeight, stoppedHeight: stopped.camera && stopped.camera.height }));
  await page.screenshot({ path: path.join(SHOTS, "record-exited.png") });

  await emit(page, statusFrame({
    mode: "record",
    record: recordSession({ phase: "resetting", version: 3 }),
  }));
  const second = await layoutState(page);
  check("re-entering record mode brings the bar back",
    !second.hidden && second.recordOpen && second.camera
      && second.camera.top >= second.transport.bottom - 1,
    JSON.stringify(second));

  check("no page errors", errors.length === 0 && failedResponses.length === 0,
    [...errors, ...failedResponses].join(" | "));
  await context.close();
}

async function main() {
  const port = await getFreePort();
  const origin = `http://127.0.0.1:${port}`;
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "lerobot-record-transport-"));
  const storePath = path.join(temp, "monitor_store.json");
  const configPath = path.join(temp, "config.yaml");
  const root = path.join(temp, "data").replaceAll("\\", "/");
  fs.writeFileSync(configPath, [
    "server:",
    "  host: 127.0.0.1",
    `  port: ${port}`,
    "  base_path: /lerobot",
    `store_path: ${storePath.replaceAll("\\", "/")}`,
    "robot:",
    "  auto_connect: false",
    "  port: \"\"",
    "virtual_follower:",
    "  enabled: true",
    "  auto_connect: false",
    "leader:",
    "  auto_connect: false",
    "  port: \"\"",
    "cameras:",
    "  probe: false",
    "recording:",
    `  root: ${root}/videos`,
    "library:",
    `  videos_root: ${root}/videos`,
    "  dataset_roots:",
    `    - ${root}/datasets`,
    "  models_roots:",
    `    - ${root}/models`,
    `  snapshots_root: ${root}/snapshots`,
    "robot_models:",
    `  root: ${root}/robot_models`,
    "",
  ].join("\n"), "utf8");

  let logText = "";
  const child = spawn("uv", [
    "run",
    "--no-sync",
    "lerobot-monitor",
    "--config",
    configPath,
    "--port",
    String(port),
  ], {
    cwd: ROOT,
    env: { ...process.env, PYTHONUNBUFFERED: "1", HF_HOME: path.join(temp, "hf") },
    windowsHide: true,
  });
  child.stdout.on("data", (chunk) => { logText += chunk.toString(); });
  child.stderr.on("data", (chunk) => { logText += chunk.toString(); });

  let browser = null;
  try {
    await waitForServer(`${origin}/lerobot/api/status`, child, () => logText.slice(-4000));
    browser = await chromium.launch({
      channel: "chrome",
      headless: process.env.RECORD_TRANSPORT_HEADED !== "1",
    });
    await runChecks(browser, origin);
  } finally {
    if (browser) await browser.close();
    await stopChild(child);
  }

  const failed = results.filter((result) => !result.ok);
  console.log(`${results.length - failed.length}/${results.length} checks passed`);
  console.log(`screenshots: ${SHOTS}`);
  process.exitCode = failed.length ? 1 : 0;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
