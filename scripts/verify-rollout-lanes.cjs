// Playwright verification for rollout inference lanes.
//
// Starts an isolated monitor on a temporary config/store, injects a synthetic
// rollout WebSocket stream, then checks canvas pixels, legend persistence,
// tooltip content and responsive containment at desktop and phone widths.
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
const SHOTS = process.env.ROLLOUT_LANES_SHOTS
  || path.resolve(ROOT, "..", ".agent-progress", "rollout-lanes-shots");
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

function syntheticFrames(base, actionNames) {
  const count = 100;
  const startedAt = Date.now() / 1000;
  const rolloutAge = 8;
  const stepS = 1 / 30;
  const frames = [];
  for (let frame = 0; frame < count; frame += 1) {
    const t = startedAt + frame * 0.02;
    const timelineT = rolloutAge + frame * 0.02;
    const pose = {};
    actionNames.forEach((name, index) => {
      pose[name] = Math.round((18 * Math.sin(frame * 0.08 + index * 0.7) + index * 3) * 1000) / 1000;
    });
    const blocks = [];
    const lastChunk = Math.floor(frame / 8);
    for (let chunk = 0; chunk <= lastChunk; chunk += 1) {
      const active = rolloutAge + chunk * 0.16 - 0.02;
      blocks.push({
        id: chunk + 1,
        kind: chunk % 2 ? "sync" : "rtc",
        start: Math.max(0, active - (chunk % 2 ? 0.07 : 0.12)),
        end: Math.max(0, active - 0.01),
        active,
        steps: chunk % 2 ? 1 : (chunk % 3 ? 12 : 8),
        step_s: stepS,
        failed: false,
      });
    }
    frames.push({
      ...base,
      ts: t,
      mode: "rollout",
      display_mode: "rollout",
      action: pose,
      joints: pose,
      task: { ...(base.task || {}), kind: "rollout", elapsed_s: timelineT },
      prediction: {
        id: frame + 1,
        t_s: timelineT,
        step_s: stepS,
        strategy: "policy_queue",
        degraded: false,
        latency_ms: 62,
        actions: Array.from({ length: 12 }, () => ({ ...pose })),
      },
      rollout_timeline: {
        t_s: timelineT,
        step_s: stepS,
        blocks,
      },
    });
  }
  return frames;
}

async function injectFrames(page, frames) {
  await page.addInitScript(({ stream, intervalMs }) => {
    window.__rolloutLanesDone = false;
    class SyntheticWebSocket {
      constructor(url) {
        this.url = url;
        this.readyState = 0;
        this.onopen = null;
        this.onclose = null;
        this.onerror = null;
        this.onmessage = null;
        setTimeout(() => {
          if (this.readyState === 3) return;
          this.readyState = 1;
          if (this.onopen) this.onopen({});
          let index = 0;
          const emit = () => {
            if (this.readyState === 3) return;
            const frame = stream[Math.min(index, stream.length - 1)];
            if (this.onmessage) this.onmessage({ data: JSON.stringify(frame) });
            index += 1;
            if (index < stream.length) setTimeout(emit, intervalMs);
            else window.__rolloutLanesDone = true;
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
  }, { stream: frames, intervalMs: 20 });
}

async function laneCanvasStats(page) {
  return page.evaluate(() => {
    const canvas = document.getElementById("chart-action");
    const chart = window.Chart && window.Chart.getChart(canvas);
    if (!canvas || !chart || !chart.chartArea) return null;
    const context = canvas.getContext("2d");
    const scale = canvas.width / Math.max(1, canvas.getBoundingClientRect().width);
    const area = chart.chartArea;
    const laneHeight = Number(chart.$laneHeight) || 0;
    const top = Math.max(0, Math.floor((area.bottom + 4) * scale));
    const bottom = Math.min(canvas.height, Math.ceil((area.bottom + laneHeight) * scale));
    const image = context.getImageData(0, top, canvas.width, Math.max(0, bottom - top));
    let green = 0;
    let blue = 0;
    for (let index = 0; index < image.data.length; index += 4) {
      const red = image.data[index];
      const greenValue = image.data[index + 1];
      const blueValue = image.data[index + 2];
      if (greenValue > 115 && greenValue > red + 18 && blueValue > 90) green += 1;
      if (blueValue > 145 && blueValue > red + 30 && blueValue > greenValue + 20) blue += 1;
    }
    const lanes = chart.$rolloutLanes || {};
    return {
      green,
      blue,
      laneHeight,
      chunks: (lanes.chunks || []).filter((span) => span.chunk).length,
      inferences: (lanes.inferences || []).length,
      overlaps: (lanes.overlaps || []).length,
      inputs: (lanes.inputs || []).length,
      drawStats: chart.$rolloutLaneDrawStats || null,
      canvasWidth: canvas.width,
      canvasHeight: canvas.height,
      area: { left: area.left, right: area.right, top: area.top, bottom: area.bottom },
    };
  });
}

async function runViewport(browser, viewport, base, frames) {
  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await injectFrames(page, frames);
  await page.goto(`http://127.0.0.1:${viewport.port}/lerobot/?v=${Date.now()}`, {
    waitUntil: "domcontentloaded",
  });
  await page.waitForSelector("#chart-action", { timeout: 30000 });
  await page.waitForFunction(
    () => typeof window.RolloutLanes === "object"
      && document.querySelector("#action-legend")?.textContent.includes("chunk input"),
    null,
    { timeout: 30000 },
  );
  await page.waitForFunction(() => window.__rolloutLanesDone === true, null, { timeout: 30000 });
  await page.waitForTimeout(250);

  const labels = await page.locator("#action-legend .chart-legend-label").allTextContents();
  for (const label of ["chunk input", "chunk span", "chunk overlap", "inference"]) {
    check(`${viewport.name} legend contains ${label}`, labels.includes(label), labels.join(", "));
  }
  const stats = await laneCanvasStats(page);
  check(`${viewport.name} lane height reserved`, stats && stats.laneHeight === 26, JSON.stringify(stats));
  check(`${viewport.name} input markers rendered`, stats && stats.inputs > 0, JSON.stringify(stats));
  check(`${viewport.name} chunk spans rendered`, stats && stats.chunks > 1, JSON.stringify(stats));
  check(`${viewport.name} inference spans rendered`, stats && stats.inferences > 0, JSON.stringify(stats));
  check(`${viewport.name} overlap spans rendered`, stats && stats.overlaps > 0, JSON.stringify(stats));
  check(`${viewport.name} green lane pixels visible`, stats && stats.green > 20, JSON.stringify(stats));
  check(`${viewport.name} blue inference pixels visible`, stats && stats.blue > 10, JSON.stringify(stats));

  await page.locator("#chart-action").evaluate((element) => element.scrollIntoView({ block: "center" }));
  await page.waitForTimeout(120);
  const tooltipState = await page.evaluate(() => {
    const canvas = document.getElementById("chart-action");
    const chart = window.Chart.getChart(canvas);
    const lanes = chart && chart.$rolloutLanes;
    const marker = lanes && lanes.inputs && lanes.inputs[lanes.inputs.length - 1];
    if (!marker) return null;
    return {
      x: chart.scales.x.getPixelForValue(marker.x),
      y: chart.chartArea.bottom + 16,
      left: canvas.getBoundingClientRect().left,
      top: canvas.getBoundingClientRect().top,
    };
  });
  if (tooltipState) {
    await page.locator("#chart-action").hover({
      position: { x: tooltipState.x, y: tooltipState.y },
      force: true,
    });
    await page.waitForTimeout(150);
  }
  const tooltip = await page.evaluate(() => {
    const chart = window.Chart.getChart(document.getElementById("chart-action"));
    const element = chart && chart.$hoverTooltipElement;
    const pointer = chart?.$pointerPosition || null;
    const laneHovered = pointer ? window.pointerYInRolloutLanes(chart, pointer.y) : false;
    const target = pointer ? chart.scales.x.getValueForPixel(pointer.x) : null;
    const model = chart && Number.isFinite(Number(target))
      ? window.chartTooltipModel(chart, target, laneHovered)
      : null;
    return {
      hidden: !element || element.hidden,
      lane: element?.querySelector(".chart-tooltip-lane")?.textContent || "",
      pointer,
      laneHovered,
      area: chart ? {
        bottom: chart.chartArea.bottom,
        laneHeight: chart.$laneHeight,
      } : null,
      modelLane: model?.lane?.text || "",
    };
  });
  check(`${viewport.name} lane tooltip visible`, !tooltip.hidden, JSON.stringify(tooltip));
  check(
    `${viewport.name} lane tooltip contains chunk details`,
    /chunk #/.test(tooltip.lane) && /steps/.test(tooltip.lane),
    tooltip.lane,
  );

  const chunkToggle = page.locator("#action-legend .chart-legend-item")
    .filter({ hasText: "chunk span" })
    .locator("input");
  await chunkToggle.scrollIntoViewIfNeeded();
  check(`${viewport.name} chunk span starts enabled`, await chunkToggle.isChecked());
  const beforeToggle = await laneCanvasStats(page);
  await chunkToggle.uncheck();
  await page.waitForTimeout(100);
  const hiddenStats = await laneCanvasStats(page);
  check(
    `${viewport.name} chunk span toggle hides green band`,
    beforeToggle.drawStats?.chunks > 0 && hiddenStats.drawStats?.chunks === 0,
    JSON.stringify({ before: beforeToggle.drawStats, hidden: hiddenStats.drawStats }),
  );
  const persisted = await page.evaluate(() => {
    const saved = JSON.parse(localStorage.getItem("lerobot-monitor-chart-legend") || "{}");
    return saved.chunkSpan;
  });
  check(`${viewport.name} chunk span toggle persisted`, persisted === false, String(persisted));

  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => window.__rolloutLanesDone === true, null, { timeout: 30000 });
  const restoredToggle = page.locator("#action-legend .chart-legend-item")
    .filter({ hasText: "chunk span" })
    .locator("input");
  await restoredToggle.scrollIntoViewIfNeeded();
  check(`${viewport.name} chunk span remains hidden after reload`, !(await restoredToggle.isChecked()));
  await restoredToggle.check();
  await page.waitForFunction(() => {
    const canvas = document.getElementById("chart-action");
    const chart = window.Chart && window.Chart.getChart(canvas);
    const stats = chart && chart.$rolloutLaneDrawStats;
    return stats && stats.chunks > 0 && stats.inferences > 0;
  }, null, { timeout: 10000 });

  const overflow = await page.evaluate(() => ({
    horizontal: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  check(`${viewport.name} no horizontal overflow`, !overflow.horizontal, JSON.stringify(overflow));
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.locator("#chart-action").evaluate((element) => element.scrollIntoView({ block: "center" }));
  await page.waitForTimeout(120);
  await page.screenshot({ path: path.join(SHOTS, `rollout-lanes-${viewport.name}.png`) });
  check(`${viewport.name} no page errors`, errors.length === 0, errors.join(" | "));
  await context.close();
}

async function main() {
  const port = await getFreePort();
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "lerobot-rollout-lanes-"));
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
    "run",
    "--no-sync",
    "lerobot-monitor",
    "--config",
    configPath,
    "--port",
    String(port),
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
    const actionNames = Object.keys(base.joints || {}).length
      ? Object.keys(base.joints)
      : ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"];
    const frames = syntheticFrames(base, actionNames);
    browser = await chromium.launch({
      channel: "chrome",
      headless: process.env.ROLLOUT_LANES_HEADED !== "1",
    });
    await runViewport(browser, { name: "1440x900", width: 1440, height: 900, port }, base, frames);
    await runViewport(browser, { name: "390x844", width: 390, height: 844, port }, base, frames);
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
