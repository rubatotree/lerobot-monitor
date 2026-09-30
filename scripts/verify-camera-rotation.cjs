// Playwright verification for main-view camera rotation (0/90/180/270).
//
// Starts an isolated monitor with no real cameras, injects a synthetic status
// stream that advertises one camera, serves a two-tone PNG for its MJPEG URL,
// then checks the rotation control, the layout box swap, the persisted choice
// and the rendered pixels (the source's red half must face up after 90°).
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
const SHOTS = process.env.CAMERA_ROTATION_SHOTS
  || path.resolve(ROOT, "..", ".agent-progress", "camera-rotation-shots");
const results = [];

function check(name, ok, detail = "") {
  results.push({ name, ok: !!ok, detail });
  if (!ok) console.log(`FAIL  ${name}${detail ? ` — ${detail}` : ""}`);
}

// 24x12 PNG: red left half, blue right half, so rotation is visible in pixels.
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

// Replaces WebSocket so the page receives a status stream with one camera, and
// keeps `window.__emitCameraStatus()` available for the test.
async function injectCameraStatus(page) {
  await page.addInitScript(({ camera }) => {
    window.__cameraFrameCount = 0;
    class SyntheticWebSocket {
      constructor(url) {
        this.url = url;
        this.readyState = 0;
        this.onopen = null;
        this.onclose = null;
        this.onerror = null;
        this.onmessage = null;
        const frame = () => JSON.stringify({
          ts: Date.now() / 1000,
          mode: "idle",
          display_mode: "idle",
          task: {},
          cameras: [{ ...camera }],
        });
        window.__emitCameraStatus = () => {
          window.__cameraFrameCount += 1;
          if (this.onmessage) this.onmessage({ data: frame() });
        };
        setTimeout(() => {
          if (this.readyState === 3) return;
          this.readyState = 1;
          if (this.onopen) this.onopen({});
          let index = 0;
          const emit = () => {
            if (this.readyState === 3 || index >= 20) return;
            window.__emitCameraStatus();
            index += 1;
            setTimeout(emit, 120);
          };
          emit();
        }, 0);
      }

      send() {}

      close() {
        this.readyState = 3;
        if (this.onclose) this.onclose({});
      }
    }
    window.WebSocket = SyntheticWebSocket;
  }, { camera: CAMERA });
}

async function cardState(page) {
  return page.evaluate(() => {
    const card = document.querySelector("#cameras .cam-card");
    if (!card) return null;
    const stage = card.querySelector(".cam-stage");
    const img = card.querySelector("img");
    const button = card.querySelector("[data-cam-rotate]");
    const value = card.querySelector("[data-cam-rotate-value]");
    const style = getComputedStyle(img);
    const parts = style.transform === "none" ? [] : style.transform.replace(/^matrix\(|\)$/g, "").split(",").map(Number);
    return {
      rotation: card.dataset.rotation,
      label: value ? value.textContent : "",
      pressed: button ? button.getAttribute("aria-pressed") : null,
      live: img.classList.contains("live") && card.classList.contains("has-sig"),
      stageWidth: stage ? stage.clientWidth : 0,
      stageHeight: stage ? stage.clientHeight : 0,
      imgLayoutWidth: img.offsetWidth,
      imgLayoutHeight: img.offsetHeight,
      matrix: parts.map((part) => Math.round(part * 1000) / 1000),
      stored: localStorage.getItem("lerobot-monitor-camera-rotation"),
    };
  });
}

function matrixIs(parts, a, b, c, d) {
  const close = (value, want) => Math.abs(value - want) <= 0.01;
  return parts.length === 6
    && close(parts[0], a) && close(parts[1], b) && close(parts[2], c) && close(parts[3], d);
}

// Screenshots the image element, decodes it inside the page and averages small
// windows so letterboxed bars cannot masquerade as camera content.
async function sampleFrame(page) {
  const shot = await page.locator("#cameras .cam-card img").screenshot();
  const dataUrl = `data:image/png;base64,${shot.toString("base64")}`;
  return page.evaluate(async (url) => {
    const image = new Image();
    image.src = url;
    await image.decode();
    const canvas = document.createElement("canvas");
    canvas.width = image.naturalWidth;
    canvas.height = image.naturalHeight;
    const context = canvas.getContext("2d");
    context.drawImage(image, 0, 0);
    const window_ = Math.max(2, Math.round(Math.min(canvas.width, canvas.height) * 0.06));
    const sample = (fx, fy) => {
      const x = Math.max(0, Math.min(canvas.width - window_, Math.round(canvas.width * fx) - Math.floor(window_ / 2)));
      const y = Math.max(0, Math.min(canvas.height - window_, Math.round(canvas.height * fy) - Math.floor(window_ / 2)));
      const data = context.getImageData(x, y, window_, window_).data;
      let r = 0;
      let g = 0;
      let b = 0;
      for (let index = 0; index < data.length; index += 4) {
        r += data[index];
        g += data[index + 1];
        b += data[index + 2];
      }
      const count = data.length / 4;
      return { r: Math.round(r / count), g: Math.round(g / count), b: Math.round(b / count) };
    };
    return {
      left: sample(0.2, 0.5),
      right: sample(0.8, 0.5),
      top: sample(0.5, 0.2),
      bottom: sample(0.5, 0.8),
      width: canvas.width,
      height: canvas.height,
    };
  }, dataUrl);
}

const isRed = (color) => color.r > color.b + 40 && color.r > 60;
const isBlue = (color) => color.b > color.r + 40 && color.b > 60;

async function rotateOnce(page) {
  await page.click("#cameras .cam-card .cam-rotate");
  await page.waitForTimeout(120);
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
  // The synthetic camera has no backend entry; answer the mode probe it triggers.
  await page.route("**/api/cameras/0/resolutions*", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: "[]",
  }));
  await injectCameraStatus(page);
  await page.goto(`${origin}/lerobot/?v=${Date.now()}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#cameras .cam-card .cam-rotate", { timeout: 30000 });
  await page.waitForFunction(() => window.__cameraFrameCount > 0, null, { timeout: 10000 });
  await page.waitForFunction(
    () => document.querySelector("#cameras .cam-card img")?.classList.contains("live"),
    null,
    { timeout: 15000 },
  );

  const initial = await cardState(page);
  check("camera card exposes a rotation control", initial && initial.rotation === "0" && initial.label === "0°",
    JSON.stringify(initial));
  check("live camera frame renders through the stage", !!initial?.live, JSON.stringify(initial));
  const base = await sampleFrame(page);
  check("unrotated frame keeps the source orientation",
    isRed(base.left) && isBlue(base.right),
    JSON.stringify(base));

  await rotateOnce(page);
  const r90 = await cardState(page);
  check("first click rotates the window 90°", r90.rotation === "90" && r90.label === "90°",
    JSON.stringify(r90));
  check("90° swaps the image layout box to the stage height",
    Math.abs(r90.imgLayoutWidth - r90.stageHeight) <= 2 && Math.abs(r90.imgLayoutHeight - r90.stageWidth) <= 2,
    JSON.stringify(r90));
  check("90° applies a clockwise transform", matrixIs(r90.matrix, 0, 1, -1, 0), JSON.stringify(r90.matrix));
  const px90 = await sampleFrame(page);
  check("90° renders the source's red half at the top",
    isRed(px90.top) && isBlue(px90.bottom),
    JSON.stringify(px90));

  await rotateOnce(page);
  const r180 = await cardState(page);
  check("second click rotates the window 180°", r180.rotation === "180" && r180.label === "180°",
    JSON.stringify(r180));
  check("180° keeps the stage-sized layout box",
    Math.abs(r180.imgLayoutWidth - r180.stageWidth) <= 2 && Math.abs(r180.imgLayoutHeight - r180.stageHeight) <= 2,
    JSON.stringify(r180));
  check("180° mirrors the frame", matrixIs(r180.matrix, -1, 0, 0, -1), JSON.stringify(r180.matrix));
  const px180 = await sampleFrame(page);
  check("180° renders the source's red half on the right",
    isBlue(px180.left) && isRed(px180.right),
    JSON.stringify(px180));

  await rotateOnce(page);
  const r270 = await cardState(page);
  check("third click rotates the window 270°", r270.rotation === "270" && r270.label === "270°",
    JSON.stringify(r270));
  check("270° applies a counter-clockwise transform", matrixIs(r270.matrix, 0, -1, 1, 0), JSON.stringify(r270.matrix));
  const px270 = await sampleFrame(page);
  check("270° renders the source's red half at the bottom",
    isBlue(px270.top) && isRed(px270.bottom),
    JSON.stringify(px270));
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, "camera-rotation-270.png") });

  await rotateOnce(page);
  const reset = await cardState(page);
  const resetStored = JSON.parse(reset.stored || "{}");
  check("fourth click returns to 0° and drops the stored angle",
    reset.rotation === "0" && reset.label === "0°" && !("local:0" in resetStored),
    JSON.stringify(reset));
  check("rotation state reflects on the control",
    (await page.getAttribute("#cameras .cam-card .cam-rotate", "aria-pressed")) === "false");

  // Persistence: rotate to 90°, reload, expect the window to come back rotated.
  await rotateOnce(page);
  check("stored rotation names the camera identity",
    String((await cardState(page)).stored || "").includes("local:0"),
    String((await cardState(page)).stored));
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector("#cameras .cam-card .cam-rotate", { timeout: 30000 });
  await page.waitForFunction(() => window.__cameraFrameCount > 0, null, { timeout: 10000 });
  const reloaded = await cardState(page);
  check("reload restores the rotated window", reloaded.rotation === "90", JSON.stringify(reloaded));

  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, "camera-rotation-90.png") });

  // Narrow layout: the control must stay usable and must not push the page wide.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(200);
  const narrow = await page.evaluate(() => {
    const card = document.querySelector("#cameras .cam-card");
    const label = card.querySelector(".cam-lbl");
    const button = card.querySelector("[data-cam-rotate]");
    const rect = button.getBoundingClientRect();
    return {
      buttonWidth: Math.round(rect.width),
      buttonVisible: rect.width > 0 && rect.height > 0 && !button.disabled,
      labelClipped: label.scrollWidth > label.clientWidth + 2,
      pageOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      rotation: card.dataset.rotation,
    };
  });
  check("rotation control survives the narrow layout",
    narrow.buttonVisible && !narrow.labelClipped && !narrow.pageOverflow && narrow.rotation === "90",
    JSON.stringify(narrow));
  await page.screenshot({ path: path.join(SHOTS, "camera-rotation-narrow.png") });
  await page.setViewportSize({ width: 1440, height: 900 });
  check("no page errors", errors.length === 0 && failedResponses.length === 0,
    [...errors, ...failedResponses].join(" | "));
  await context.close();
}

async function main() {
  const port = await getFreePort();
  const origin = `http://127.0.0.1:${port}`;
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "lerobot-camera-rotation-"));
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
      headless: process.env.CAMERA_ROTATION_HEADED !== "1",
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
