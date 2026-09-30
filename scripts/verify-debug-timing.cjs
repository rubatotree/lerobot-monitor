// Playwright verification for the Debug panel's inference timing bar.
//
// Starts an isolated monitor on a temporary config/store, opens the Debug tab and
// renders synthetic inference results through the real render function, then checks
// the bar geometry, chips, captions and responsive containment at desktop and phone
// widths.
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
const SHOTS = process.env.DEBUG_TIMING_SHOTS
  || path.resolve(ROOT, "..", ".agent-progress", "debug-timing-shots");
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

const COLD = {
  fps: 30,
  latency_ms: 2000,
  model_wait_ms: 800,
  model_load_ms: 800,
  compute_ms: 900,
  actions: Array.from({ length: 16 }, () => ({ gripper: 0 })),
  generated_steps: 50,
  stage_ms: {
    cloud_encode: 10,
    cloud_upload: 250,
    cloud_compute: 300,
    cloud_download: 20,
  },
  evaluation: {
    score: 61.2,
    steps: 16,
    predicted_steps: 16,
    coverage: 1,
    joints: {
      gripper: { mae: 0.12, rmse: 0.2, nrmse: 0.02 },
      shoulder_pan: { mae: 1.5, rmse: 2.4, nrmse: 0.24 },
    },
  },
};
const WARM = {
  fps: 30,
  latency_ms: 400,
  model_wait_ms: 30,
  model_load_ms: 0,
  compute_ms: 350,
  actions: Array.from({ length: 4 }, () => ({ gripper: 0 })),
};
// A cloud run from the measuring build: the stage legs stay what they always were, and
// timing_ms adds the measured walk whose chores reconcile with the bar's "other".
const PHASES = {
  fps: 30,
  latency_ms: 3200,
  model_wait_ms: 900,
  model_load_ms: 900,
  compute_ms: 2500,
  actions: Array.from({ length: 16 }, () => ({ gripper: 0 })),
  generated_steps: 50,
  stage_ms: {
    cloud_encode: 41,
    cloud_upload: 800,
    cloud_compute: 128,
    cloud_download: 24,
  },
  timing_ms: {
    client_open: 900,
    client_lease: 74,
    client_build: 4,
    client_encode: 41,
    client_serialize: 9,
    client_transport: 430,
    client_ttfb: 912,
    client_read: 31,
    client_parse: 2,
    client_poses: 1,
    client_close: 1226,
    client_close_http: 620,
    client_close_join: 606,
    server_read: 700,
    server_parse: 18,
    server_service: 160,
    server_ipc: 30,
    server_worker: 128,
    server_decode: 22,
    server_prepare: 18,
    server_policy: 80,
    server_emit: 4,
  },
};
const PHASE_LABELS = [
  "session", "inference", "lease", "build", "encode", "serialize", "client setup", "tunnel",
  "server read", "validate", "service", "worker ipc", "policy worker", "decode", "prepare",
  "policy (gpu)", "emit", "read", "parse", "poses", "close", "session delete", "heartbeat join",
];

function msOf(text) {
  const value = Number.parseFloat(String(text));
  if (!Number.isFinite(value)) return Number.NaN;
  return String(text).includes("s") && !String(text).includes("ms") ? value * 1000 : value;
}

async function timingState(page) {
  return page.evaluate(() => {
    const host = document.getElementById("dbg-timing");
    const track = host && host.querySelector(".debug-timing-track");
    const trackWidth = track ? track.getBoundingClientRect().width : 0;
    const chips = {};
    [...(host ? host.querySelectorAll(".debug-timing-chip") : [])].forEach((chip) => {
      const kind = [...chip.classList].find((name) => name.startsWith("is-")) || "";
      chips[kind.replace(/^is-/, "")] = chip.querySelector(".debug-timing-value")?.textContent || "";
    });
    return {
      hidden: !host || host.classList.contains("hidden"),
      segs: [...(host ? host.querySelectorAll(".debug-timing-seg") : [])].map((element) => ({
        kind: element.dataset.kind,
        width: element.getBoundingClientRect().width,
        pct: trackWidth > 0 ? (element.getBoundingClientRect().width / trackWidth) * 100 : 0,
      })),
      chips,
      note: host?.querySelector(".debug-timing-note")?.textContent || "",
      trackWidth,
      scrollWidth: host?.scrollWidth || 0,
      clientWidth: host?.clientWidth || 0,
      pageOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    };
  });
}

async function renderTiming(page, payload) {
  await page.evaluate((value) => { window.renderChunkTiming(value); }, payload);
  await page.waitForTimeout(120);
}

async function profileState(page) {
  return page.evaluate(() => {
    const host = document.getElementById("dbg-timing");
    const sections = [...(host ? host.querySelectorAll(".debug-profile-section") : [])].map((details) => ({
      section: details.dataset.section,
      open: details.open,
      meta: details.querySelector(".debug-profile-meta")?.textContent || "",
      rows: [...details.querySelectorAll(".debug-profile-row")].map((row) => ({
        kind: row.dataset.kind,
        label: row.querySelector(".debug-profile-label")?.textContent || "",
        value: row.querySelector(".debug-profile-value")?.textContent || "",
        detail: row.querySelector(".debug-profile-detail")?.textContent || "",
        nrmse: row.querySelector(".is-nrmse")?.textContent || "",
        pct: row.style.getPropertyValue("--pct"),
        indent: row.style.getPropertyValue("--indent"),
      })),
    }));
    return {
      sections,
      scrollWidth: host?.scrollWidth || 0,
      clientWidth: host?.clientWidth || 0,
      pageOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    };
  });
}

async function toggleSection(page, section) {
  await page.locator(`#dbg-timing details[data-section="${section}"] > summary`).click();
  await page.waitForTimeout(80);
}

async function parkTimingCard(page) {
  // Park the card's top 160 px into the scrollport: neither the sticky tab strip nor a
  // short scrollport may hide the bar this shot is meant to prove.
  await page.locator("#dbg-timing").evaluate((element) => {
    const OFFSET = 160;
    const box = element.getBoundingClientRect();
    let scroller = element.parentElement;
    while (scroller && scroller !== document.body && scroller.scrollHeight <= scroller.clientHeight + 1) {
      scroller = scroller.parentElement;
    }
    if (scroller && scroller !== document.body) {
      scroller.scrollTop += (box.top - scroller.getBoundingClientRect().top) - OFFSET;
      return;
    }
    window.scrollBy(0, box.top - OFFSET);
  });
  await page.waitForTimeout(150);
}

async function runViewport(browser, viewport) {
  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
    deviceScaleFactor: viewport.name.startsWith("390") ? 2 : 1,
  });
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.goto(`http://127.0.0.1:${viewport.port}/lerobot/?v=${Date.now()}`, {
    waitUntil: "domcontentloaded",
  });
  await page.waitForFunction(
    () => typeof window.DebugTiming === "object" && typeof window.renderChunkTiming === "function",
    null,
    { timeout: 30000 },
  );
  await page.locator("#side-tab-debug").click();
  await page.waitForTimeout(120);

  const before = await timingState(page);
  check(`${viewport.name} timing card starts hidden`, before.hidden === true, JSON.stringify(before));

  await renderTiming(page, COLD);
  const cold = await timingState(page);
  const kinds = cold.segs.map((segment) => segment.kind).join(",");
  check(
    `${viewport.name} bar walks every request leg in time order`,
    !cold.hidden && kinds === "encode,upload,compute,download,other,chunk,ghost",
    kinds,
  );
  const share = (kind) => cold.segs.find((segment) => segment.kind === kind)?.pct ?? 0;
  check(`${viewport.name} upload leg keeps its share`, Math.abs(share("upload") - 8.72) < 1.5, String(share("upload")));
  check(`${viewport.name} gpu compute leg keeps its share`, Math.abs(share("compute") - 10.47) < 1.5, String(share("compute")));
  check(`${viewport.name} chores keep their share`, Math.abs(share("other") - 21.63) < 1.5, String(share("other")));
  check(`${viewport.name} chunk leg keeps its share`, Math.abs(share("chunk") - 18.60) < 1.5, String(share("chunk")));
  check(`${viewport.name} would-be tail keeps its share`, Math.abs(share("ghost") - 39.53) < 1.5, String(share("ghost")));
  check(
    `${viewport.name} session leg stays out of the bar`,
    cold.segs.every((segment) => !["load", "wait"].includes(segment.kind)),
    kinds,
  );
  check(
    `${viewport.name} chips list every request leg beside the inference total`,
    cold.chips.inference === "1.20 s"
      && cold.chips.encode === "10 ms"
      && cold.chips.upload === "250 ms"
      && cold.chips.compute === "300 ms"
      && cold.chips.download === "20 ms"
      && cold.chips.other === "620 ms"
      && cold.chips.chunk === "533 ms"
      && cold.chips.transfer === undefined
      && cold.chips.load === undefined,
    JSON.stringify(cold.chips),
  );
  check(
    `${viewport.name} note reports steps, would-be duration and fps`,
    /16 \/ 50 steps/.test(cold.note) && /of 1\.67 s/.test(cold.note) && /@ 30 fps/.test(cold.note),
    cold.note,
  );
  check(
    `${viewport.name} card fits the panel`,
    cold.scrollWidth <= cold.clientWidth + 1 && !cold.pageOverflow,
    JSON.stringify({ scrollWidth: cold.scrollWidth, clientWidth: cold.clientWidth, overflow: cold.pageOverflow }),
  );

  const collapsed = await profileState(page);
  check(
    `${viewport.name} profile sections start collapsed`,
    collapsed.sections.map((entry) => entry.section).join(",") === "timing,cloud,reference"
      && collapsed.sections.every((entry) => !entry.open),
    JSON.stringify(collapsed.sections.map((entry) => [entry.section, entry.open])),
  );

  await toggleSection(page, "timing");
  const timingSection = (await profileState(page)).sections.find((entry) => entry.section === "timing");
  check(
    `${viewport.name} timing section walks load, inference, its legs, then the chunk`,
    timingSection?.open === true
      && timingSection.meta === "1.20 s"
      && timingSection.rows.map((row) => row.kind).join(",") === "load,inference,encode,upload,compute,download,other,chunk,ghost"
      && timingSection.rows[0].value === "800 ms"
      && timingSection.rows[0].detail === "session"
      && timingSection.rows[1].value === "1.20 s"
      && timingSection.rows[1].detail === "excludes load"
      && timingSection.rows[4].kind === "compute"
      && timingSection.rows[4].value === "300 ms"
      && timingSection.rows[4].detail === "gpu"
      && /16 \/ 50 steps/.test(timingSection.rows[7].detail)
      && /34 more steps/.test(timingSection.rows[8].detail),
    JSON.stringify(timingSection),
  );

  await toggleSection(page, "cloud");
  const cloudSection = (await profileState(page)).sections.find((entry) => entry.section === "cloud");
  const cloudShare = cloudSection
    ? cloudSection.rows.reduce((total, row) => total + Number.parseFloat(row.pct) || 0, 0)
    : 0;
  check(
    `${viewport.name} cloud legs keep enc/up/gpu/down apart`,
    cloudSection?.open === true
      && cloudSection.rows.map((row) => row.kind).join(",") === "cloud_encode,cloud_upload,cloud_compute,cloud_download"
      && cloudSection.rows.map((row) => row.label).join(",") === "enc,up,gpu,down"
      && cloudSection.rows[1].value === "250 ms"
      && Math.abs(cloudShare - 100) < 1.5,
    JSON.stringify({ rows: cloudSection?.rows.map((row) => [row.kind, row.label, row.value, row.pct]), share: cloudShare }),
  );

  await toggleSection(page, "reference");
  const referenceSection = (await profileState(page)).sections.find((entry) => entry.section === "reference");
  check(
    `${viewport.name} reference section scores joints worst first`,
    referenceSection?.open === true
      && /score 61\.2 \/ 100/.test(referenceSection.meta)
      && referenceSection.rows[0].kind === "head"
      && referenceSection.rows.slice(1).map((row) => row.label).join(",") === "shoulder_pan,gripper"
      && referenceSection.rows[1].nrmse === "0.2400"
      && referenceSection.rows[1].detail === "1.5000",
    JSON.stringify(referenceSection),
  );

  await renderTiming(page, PHASES);
  const phasesCollapsed = await profileState(page);
  check(
    `${viewport.name} measured phases add a collapsed Phases section`,
    phasesCollapsed.sections.map((entry) => entry.section).join(",") === "timing,cloud,phases"
      && phasesCollapsed.sections.find((entry) => entry.section === "phases")?.open === false,
    JSON.stringify(phasesCollapsed.sections.map((entry) => [entry.section, entry.open])),
  );

  await toggleSection(page, "phases");
  const phasesState = await profileState(page);
  const phasesSection = phasesState.sections.find((entry) => entry.section === "phases");
  const phaseRows = phasesSection ? phasesSection.rows : [];
  check(
    `${viewport.name} phases walk the request inside the cloud round trip`,
    phasesSection?.open === true
      && phaseRows.map((row) => row.label).join(",") === PHASE_LABELS.join(",")
      && phasesSection.meta === "3.20 s"
      && phaseRows[0].indent === "0px"
      && phaseRows[2].indent === "8px"
      && phaseRows[10].indent === "16px"
      && phaseRows[12].indent === "32px"
      && phaseRows[15].indent === "40px"
      && phaseRows[4].kind === "encode"
      && phaseRows[6].kind === "upload"
      && phaseRows[2].kind === "other"
      && phaseRows[10].kind === "compute",
    JSON.stringify(phaseRows.map((row) => [row.label, row.value, row.kind, row.indent])),
  );
  const timingRows = phasesState.sections.find((entry) => entry.section === "timing")?.rows || [];
  const otherRow = timingRows.find((row) => row.kind === "other");
  const choreLabels = ["lease", "build", "parse", "poses", "close"];
  const choreMs = choreLabels.reduce(
    (total, label) => total + msOf(phaseRows.find((row) => row.label === label)?.value),
    0,
  );
  check(
    `${viewport.name} chores inside other reconcile with the bar segment`,
    phaseRows.every((row) => row.label !== "unmeasured")
      && Math.abs(choreMs - msOf(otherRow?.value)) < 10
      && phasesState.scrollWidth <= phasesState.clientWidth + 1
      && !phasesState.pageOverflow,
    `chores ${choreMs} vs other ${otherRow?.value} | ${JSON.stringify(phasesState.sections.find((entry) => entry.section === "phases")?.rows.map((row) => [row.label, row.value, row.pct]))}`,
  );

  fs.mkdirSync(SHOTS, { recursive: true });
  // The tree is the point of this shot: bring the open Phases section next to the bar.
  await page.locator('#dbg-timing details[data-section="phases"]').scrollIntoViewIfNeeded();
  await page.evaluate(() => window.scrollBy(0, -320));
  await page.waitForTimeout(150);
  await page.screenshot({ path: path.join(SHOTS, `debug-timing-phases-${viewport.name}.png`) });

  await renderTiming(page, WARM);
  const warm = await timingState(page);
  const warmKinds = warm.segs.map((segment) => segment.kind).join(",");
  check(
    `${viewport.name} local run keeps gpu plus chores with no transfer legs`,
    warmKinds === "compute,other,chunk"
      && warm.chips.inference === "370 ms"
      && warm.chips.compute === "350 ms"
      && warm.chips.other === "20 ms"
      && warm.chips.chunk === "133 ms"
      && warm.chips.transfer === undefined,
    `${warmKinds} | ${JSON.stringify(warm.chips)}`,
  );
  const warmProfile = await profileState(page);
  check(
    `${viewport.name} local run keeps only the timing section and its open state`,
    warmProfile.sections.map((entry) => entry.section).join(",") === "timing"
      && warmProfile.sections[0].open === true
      && warmProfile.scrollWidth <= warmProfile.clientWidth + 1
      && !warmProfile.pageOverflow,
    JSON.stringify(warmProfile.sections.map((entry) => [entry.section, entry.open])),
  );

  await page.evaluate(() => window.clearChunkTiming());
  await page.waitForTimeout(80);
  const cleared = await timingState(page);
  check(`${viewport.name} clearing hides the card again`, cleared.hidden === true, JSON.stringify(cleared));

  await renderTiming(page, COLD);
  fs.mkdirSync(SHOTS, { recursive: true });
  await parkTimingCard(page);
  await page.screenshot({ path: path.join(SHOTS, `debug-timing-${viewport.name}.png`) });
  check(`${viewport.name} no page errors`, errors.length === 0, errors.join(" | "));
  await context.close();
}

async function main() {
  const port = await getFreePort();
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "lerobot-debug-timing-"));
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
    browser = await chromium.launch({
      channel: "chrome",
      headless: process.env.DEBUG_TIMING_HEADED !== "1",
    });
    await runViewport(browser, { name: "1440x900", width: 1440, height: 900, port });
    await runViewport(browser, { name: "390x844", width: 390, height: 844, port });
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
