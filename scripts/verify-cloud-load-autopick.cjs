// Browser verification for the cloud-model Load autopick: a free GPU loads
// directly on the lowest index, an all-busy host falls back to the dialog, and
// a host without healthy GPUs errors in place. Temporary monitor config/store,
// intercepted API routes and a synthetic WebSocket; no policy or hardware runs.
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
const SHOTS = process.env.CLOUD_LOAD_SHOTS || path.join(os.tmpdir(), "lerobot-cloud-load-shots");
const results = [];

function check(name, ok, detail = "") {
  results.push({ name, ok: !!ok, detail });
  console.log(`${ok ? "ok  " : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
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
    request.setTimeout(2000, () => request.destroy(new Error("request timeout")));
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

const gpu = (index, busy, extras = {}) => ({
  uuid: `gpu-uuid-${index}`, index, healthy: true, busy,
  memory_used_mb: busy ? 21000 : 8, memory_total_mb: 24576, processes: [], ...extras,
});

async function run(browser, port, base) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const model = {
    id: "cloud/policy", path: "cloud://h1/deployments/d1", source: "cloud",
    cloud_host_id: "h1", cloud_deployment_id: "d1", name: "Cloud SmolVLA",
    playable: true, metadata: {},
    residency: { state: "unloaded", instances: [] },
  };
  const scenario = { gpus: [gpu(0, true), gpu(2, false), gpu(1, false)] };
  const loadRequests = [];
  await page.route("**/api/models", (route) => route.fulfill({ json: [model] }));
  await page.route("**/api/status", (route) => route.fulfill({ json: { ...base, model_residency: {} } }));
  await page.route("**/api/cloud/jobs", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/cloud/hosts/h1/connect", async (route) => {
    await route.fulfill({
      json: {
        host: { id: "h1", status: "connected" },
        gpus: scenario.gpus,
        deployments: [{ id: "d1", status: "unloaded" }],
      },
    });
  });
  await page.route("**/api/models/cloud%2Fpolicy/load", async (route) => {
    loadRequests.push(route.request().postDataJSON());
    await route.fulfill({ json: { ok: true, job_id: "job-1" } });
  });
  await page.addInitScript((initial) => {
    class SyntheticWebSocket {
      constructor() {
        this.readyState = 1;
        const emit = (next) => this.onmessage?.({ data: JSON.stringify({ ...next, ts: Date.now() / 1000 }) });
        setTimeout(() => { this.onopen?.({}); emit(initial); }, 100);
      }
      close() { this.readyState = 3; }
      send() {}
    }
    window.WebSocket = SyntheticWebSocket;
  }, base);
  await page.goto(`http://127.0.0.1:${port}/lerobot/`, { waitUntil: "domcontentloaded" });
  const loadButton = page.locator('#md-list [data-residency-action="load"]');
  await loadButton.waitFor();
  const modalOpen = () => page.evaluate(() => {
    const modal = document.getElementById("library-modal");
    return !!modal && !modal.classList.contains("hidden");
  });
  const logText = () => page.locator("#log").textContent();

  // A: free GPUs exist — the lowest index loads immediately, no dialog.
  await loadButton.click();
  await page.waitForFunction(
    () => document.getElementById("log")?.textContent.includes("Model load requested"),
    undefined, { timeout: 8000 },
  );
  check("free GPU loads without the dialog", loadRequests.length === 1 && !(await modalOpen()));
  check("lowest free index selected", loadRequests[0]?.device === "gpu-uuid-1",
    JSON.stringify(loadRequests[0] || null));

  // B: every GPU busy — the dialog opens and nothing is submitted.
  loadRequests.length = 0;
  scenario.gpus = [gpu(0, true), gpu(1, true), gpu(2, true)];
  await loadButton.click();
  await page.waitForFunction(() => {
    const modal = document.getElementById("library-modal");
    return modal && !modal.classList.contains("hidden")
      && modal.textContent.includes("No GPU is currently available");
  }, undefined, { timeout: 8000 });
  check("all GPUs busy opens the dialog", await modalOpen());
  check("all-busy dialog submits nothing", loadRequests.length === 0);
  const disabledOptions = await page.locator("#library-modal select option").evaluateAll(
    (options) => options.map((option) => option.disabled),
  );
  check("busy GPUs are not selectable for sharing", disabledOptions.length === 3
    && disabledOptions.every(Boolean), JSON.stringify(disabledOptions));
  await page.click("#btn-library-modal-close");
  await page.waitForFunction(() => document.getElementById("library-modal").classList.contains("hidden"));

  // C: no healthy GPU at all — error in place, no dialog, no request.
  loadRequests.length = 0;
  scenario.gpus = [];
  await loadButton.click();
  await page.waitForFunction(
    () => document.getElementById("log")?.textContent.includes("No healthy cloud GPU"),
    undefined, { timeout: 8000 },
  );
  check("no GPU reports an error", (await logText()).includes("No healthy cloud GPU"));
  check("no GPU opens no dialog and submits nothing", !(await modalOpen()) && loadRequests.length === 0);

  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, "cloud-load-autopick.png"), fullPage: true });
  check("no browser exceptions", errors.length === 0, errors.join(";"));
  await context.close();
}

async function main() {
  const port = await getFreePort();
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "lerobot-cloud-load-"));
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
    "  auto_connect: true",
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
    "rollout:",
    "  device: cpu",
    "",
  ].join("\n"), "utf8");

  let logText = "";
  const child = spawn("uv", [
    "run", "--no-sync", "lerobot-monitor", "--config", configPath, "--port", String(port),
  ], {
    cwd: ROOT,
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
    windowsHide: true,
  });
  child.stdout.on("data", (chunk) => { logText += chunk.toString(); });
  child.stderr.on("data", (chunk) => { logText += chunk.toString(); });

  let browser = null;
  try {
    const statusUrl = `http://127.0.0.1:${port}/lerobot/api/status`;
    await waitForServer(statusUrl, child, () => logText.slice(-4000));
    const base = await requestJson(statusUrl);
    browser = await chromium.launch({
      channel: "chrome",
      headless: process.env.CLOUD_LOAD_HEADED !== "1",
    });
    await run(browser, port, base);
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
