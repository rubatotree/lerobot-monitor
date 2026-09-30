// Playwright verification for the UI fix batch:
//   1. Hardware side tab sits to the right of Cloud.
//   2. The arm port defaults to empty instead of the virtual preview.
//   3. Presets restore dragged Library resources when the option is missing.
//   4. Library / Cloud metadata exposes working copy chips.
//
// Starts an isolated monitor on a temporary config/store, seeds presets,
// a dataset and a cloud host through the HTTP API, then drives the real UI.
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
const results = [];

function check(name, ok, detail = "") {
  results.push({ name, ok: !!ok, detail });
  if (!ok) console.log(`FAIL  ${name}${detail ? ` — ${detail}` : ""}`);
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

function requestJson(url, { method = "GET", body } = {}) {
  return new Promise((resolve, reject) => {
    const payload = body === undefined ? null : Buffer.from(JSON.stringify(body), "utf8");
    const request = http.request(url, {
      method,
      headers: payload ? { "content-type": "application/json", "content-length": payload.length } : undefined,
    }, (response) => {
      let text = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { text += chunk; });
      response.on("end", () => {
        if (!response.statusCode || response.statusCode >= 400) {
          reject(new Error(`HTTP ${response.statusCode}: ${text}`));
          return;
        }
        try { resolve(JSON.parse(text)); } catch (error) { reject(error); }
      });
    });
    request.on("error", reject);
    request.setTimeout(5000, () => request.destroy(new Error("request timeout")));
    if (payload) request.write(payload);
    request.end();
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

const MISSING_MODEL_PATH = "C:/not/a/library/model";
const MISSING_DATASET_ID = "missing_dataset_id";

async function seedFixtures(origin) {
  await requestJson(`${origin}/lerobot/api/presets/rollout/legacy-model`, {
    method: "PUT",
    body: { policy_path: MISSING_MODEL_PATH, task: "preset fallback" },
  });
  await requestJson(`${origin}/lerobot/api/presets/debug/legacy-model`, {
    method: "PUT",
    body: { policy_path: MISSING_MODEL_PATH },
  });
  await requestJson(`${origin}/lerobot/api/presets/record/legacy-ds`, {
    method: "PUT",
    body: { dataset_id: MISSING_DATASET_ID, task: "preset fallback" },
  });
  await requestJson(`${origin}/lerobot/api/datasets/empty`, {
    method: "POST",
    body: { name: "copy-fixture", fps: 15, robot_type: "so101", cameras: [] },
  });
}

async function readClipboard(page) {
  return page.evaluate(() => navigator.clipboard.readText());
}

async function runChecks(browser, origin, port) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    permissions: ["clipboard-read", "clipboard-write"],
  });
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.goto(`${origin}/lerobot/?v=${Date.now()}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#side-tabs", { timeout: 30000 });
  await page.waitForTimeout(600);

  // 1. Side tab order and switching.
  const tabs = await page.evaluate(() => {
    const cloud = document.getElementById("side-tab-cloud").getBoundingClientRect();
    const hardware = document.getElementById("side-tab-hardware").getBoundingClientRect();
    return { cloudLeft: cloud.left, cloudRight: cloud.right, hardwareLeft: hardware.left, hardwareRight: hardware.right };
  });
  check("Hardware tab sits right of Cloud", tabs.hardwareLeft > tabs.cloudRight,
    JSON.stringify(tabs));
  await page.click("#side-tab-hardware");
  check("Hardware tab opens its panel",
    await page.isVisible("#hw-panel") && !(await page.isVisible("#cloud-panel")));
  await page.click("#side-tab-cloud");
  check("Cloud tab opens its panel",
    await page.isVisible("#cloud-panel") && !(await page.isVisible("#hw-panel")));

  // 2. Arm port default and the virtual preview staying off.
  const arm = await page.evaluate(() => ({
    port: document.getElementById("arm-port").value,
    label: document.getElementById("st-robot").textContent,
    pressed: document.getElementById("btn-hdr-arm-power").getAttribute("aria-pressed"),
    previewPower: document.getElementById("preview-power").getAttribute("aria-pressed"),
  }));
  check("arm port defaults to empty", arm.port === "", JSON.stringify(arm));
  check("arm header reports No device", arm.label.trim() === "No device", JSON.stringify(arm));
  check("arm power button is off", arm.pressed === "false", JSON.stringify(arm));
  check("virtual preview stays off by default", arm.previewPower === "false", JSON.stringify(arm));

  // 3. Preset load fallback for dragged Library resources.
  for (const [tab, preset, selectId, expected] of [
    ["#side-tab-rollout", "legacy-model", "#pol-path", MISSING_MODEL_PATH],
    ["#side-tab-debug", "legacy-model", "#dbg-policy", MISSING_MODEL_PATH],
    ["#side-tab-record", "legacy-ds", "#rec-dataset-select", `dataset:${MISSING_DATASET_ID}`],
  ]) {
    await page.click(tab);
    await page.selectOption("#preset-select", preset);
    await page.click("#btn-preset-load");
    await page.waitForFunction(
      ([id, want]) => {
        const select = document.querySelector(id);
        return !!select && select.value === want
          && [...select.options].some((option) => option.value === want);
      },
      [selectId, expected],
      { timeout: 8000 },
    ).catch(() => {});
    const state = await page.evaluate(([id]) => {
      const select = document.querySelector(id);
      return {
        value: select.value,
        option: [...select.options].some((option) => option.value === select.value),
      };
    }, [selectId]);
    check(`${preset} restores ${selectId}`, state.value === expected && state.option,
      `${state.value} / option=${state.option}`);
  }

  // 4. Copy chips: Library metadata row + copy-all.
  await page.click("#lib-tab-datasets");
  await page.waitForSelector("#ds-list .library-item", { timeout: 15000 });
  await page.waitForSelector("#ds-list .lib-meta-row .copy-chip", { timeout: 15000 });
  const row = await page.evaluate(() => {
    const line = document.querySelector("#ds-list .lib-meta-row");
    return {
      label: line.querySelector("strong").textContent,
      value: line.querySelector("span").textContent,
    };
  });
  await page.click("#ds-list .lib-meta-row .copy-chip");
  await page.waitForTimeout(150);
  const rowCopy = await readClipboard(page);
  check("metadata row copy chip copies its value", rowCopy === `${row.label}: ${row.value}`,
    JSON.stringify({ rowCopy, row }));

  await page.click("#ds-list .lib-meta-head .copy-chip");
  await page.waitForTimeout(150);
  const allCopy = await readClipboard(page);
  const allRows = await page.evaluate(() => [...document.querySelectorAll("#ds-list .library-item .lib-meta-row")]
    .map((line) => `${line.querySelector("strong").textContent}: ${line.querySelector("span").textContent}`));
  check("metadata copy-all covers every row",
    allCopy.split("\n").length === allRows.length
      && allRows.every((line) => allCopy.includes(line)),
    JSON.stringify({ allCopy, allRows }));

  // 5. Copy chips: Cloud host card (uses whatever host is registered, since
  // the manager state lives outside the temp store).
  await page.click("#side-tab-cloud");
  const hosts = await requestJson(`${origin}/lerobot/api/cloud/hosts`);
  const list = Array.isArray(hosts) ? hosts : (hosts.hosts || []);
  if (!list.length) {
    console.log("skip  cloud host copy chip — no cloud host registered");
  } else {
    const chip = await page.$("#cloud-host-meta .copy-chip");
    check("cloud host card exposes a copy chip", !!chip);
    if (chip) {
      await page.click("#cloud-host-meta .copy-chip");
      await page.waitForTimeout(150);
      const hostCopy = await readClipboard(page);
      check("cloud host copy chip copies the selected server info",
        hostCopy.includes(`Alias: ${list[0].alias}`) && hostCopy.includes("Status:"),
        JSON.stringify(hostCopy));
    }
  }

  check("no page errors", errors.length === 0, errors.join(" | "));
  await context.close();
}

async function main() {
  const port = await getFreePort();
  const origin = `http://127.0.0.1:${port}`;
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "lerobot-ui-fixes-"));
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
    "rollout:",
    "  device: cpu",
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
    await seedFixtures(origin);
    browser = await chromium.launch({
      channel: "chrome",
      headless: process.env.UI_FIXES_HEADED !== "1",
    });
    await runChecks(browser, origin, port);
  } finally {
    if (browser) await browser.close();
    await stopChild(child);
  }

  const failed = results.filter((result) => !result.ok);
  console.log(`${results.length - failed.length}/${results.length} checks passed`);
  process.exitCode = failed.length ? 1 : 0;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
