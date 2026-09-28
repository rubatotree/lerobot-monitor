// Browser verification for rollout execution speed.
// Uses a temporary monitor config/store, synthetic status messages and an
// intercepted startup request; no policy or physical hardware is started.
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
const SHOTS = process.env.ROLLOUT_SPEED_SHOTS
  || path.join(os.tmpdir(), "lerobot-rollout-speed-shots");
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

async function runViewport(browser, viewport, base) {
  const context = await browser.newContext({ viewport:{width:viewport.width,height:viewport.height}, deviceScaleFactor:viewport.width<500?2:1 });
  const page = await context.newPage();
  const errors=[], starts=[], armUpdates=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/rollout/start',async route=>{
    starts.push(route.request().postDataJSON());
    await route.fulfill({status:200,contentType:'application/json',body:'{"ok":true}'});
  });
  page.on('request',request=>{ if(request.url().endsWith('/api/control/rates')&&request.method()==='PUT') armUpdates.push(request.postDataJSON()); });
  const status={...base,mode:'joints',display_mode:'joints',motion_locked:false,
    task:{...(base.task||{}),pending:null}, rates:{...(base.rates||{}),mode:'joints',default_hz:30,effective_hz:30,
    modes:{...(base.rates?.modes||{}),rollout:{kind:'inherit'}}}};
  await page.addInitScript(initial=>{
    class SyntheticWebSocket {
      constructor() {
        this.readyState=1;
        window.__speedStatus=next=>this.onmessage?.({data:JSON.stringify({...next,ts:Date.now()/1000})});
        setTimeout(()=>{ this.onopen?.({}); window.__speedStatus(initial); },100);
      }
      close() { this.readyState=3; }
      send() {}
    }
    window.WebSocket=SyntheticWebSocket;
  },status);
  await page.goto(`http://127.0.0.1:${viewport.port}/lerobot/`,{waitUntil:'domcontentloaded'});
  await page.waitForFunction(()=>!!window.RolloutSpeed&&!!window.applyRolloutFields);
  await page.waitForTimeout(400);
  await page.locator('#side-tab-rollout').click();
  await page.evaluate(()=>window.applyRolloutFields({policy_fps:15,execution_speed:1}));
  check(`${viewport.name} default speed is1x`,await page.locator('#rollout-speed-label').textContent()==='1×');
  const armBefore=await page.locator('#rollout-rate .rate-chip-value').textContent();
  const pick=async value=>{await page.locator('#rollout-speed').click();await page.locator(`[data-rollout-speed="${value}"]`).click();};
  await pick('.25'.replace(/^\./,'0.'));
  check(`${viewport.name} low preset scales execution`,(await page.locator('#rollout-speed-preview').textContent()).includes('3.75 Hz'));
  await pick('2');
  check(`${viewport.name} high preset scales execution`,(await page.locator('#rollout-speed-preview').textContent()).includes('30 Hz'));
  await page.locator('#rollout-speed').click();
  const bounds=await page.locator('#rollout-speed-panel').evaluate(el=>{
    const r=el.getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,w:innerWidth,h:innerHeight};
  });
  check(`${viewport.name} popover contained`,bounds.left>=0&&bounds.top>=0&&bounds.right<=bounds.w&&bounds.bottom<=bounds.h,JSON.stringify(bounds));
  await page.locator('#rollout-speed-custom').fill('1.234');
  await page.locator('#rollout-speed-apply').click();
  check(`${viewport.name} custom speed preserved`,await page.evaluate(()=>window.rolloutFields().execution_speed===1.234));
  check(`${viewport.name} CLI uses effective frequency`,(await page.locator('#info-roll').textContent()).includes('--fps=18.51'));
  for(const value of ['0','-1','0.001','1000','']) {
    await page.locator('#rollout-speed').click();
    await page.locator('#rollout-speed-custom').fill(value);
    await page.locator('#rollout-speed-apply').click();
    check(`${viewport.name} invalid custom ${value||'empty'} rejected`,await page.evaluate(()=>window.rolloutFields().execution_speed===1.234&&document.getElementById('rollout-speed-custom').getAttribute('aria-invalid')==='true'));
    await page.keyboard.press('Escape');
  }
  check(`${viewport.name} Escape restores focus`,await page.evaluate(()=>document.activeElement.id==='rollout-speed'));
  await page.locator('#rollout-speed').click();
  await page.mouse.click(3,3);
  check(`${viewport.name} outside click closes`,await page.locator('#rollout-speed').getAttribute('aria-expanded')==='false');
  await page.evaluate(()=>window.saveNamedPreset('rollout','Speed test',window.rolloutFields()));
  await pick('1');
  await page.locator('#preset-select').selectOption('Speed test');
  await page.locator('#btn-preset-load').click();
  check(`${viewport.name} preset restores custom multiplier`,await page.evaluate(()=>window.rolloutFields().execution_speed===1.234));
  await page.evaluate(()=>{ const old=window.rolloutFields();delete old.execution_speed;return window.saveNamedPreset('rollout','Legacy speed',old); });
  await page.locator('#preset-select').selectOption('Legacy speed');
  await page.locator('#btn-preset-load').click();
  check(`${viewport.name} old preset defaults1x`,await page.evaluate(()=>window.rolloutFields().execution_speed===1));
  await pick('0.5');
  await page.waitForTimeout(600);
  await page.reload({waitUntil:'domcontentloaded'});
  await page.waitForFunction(()=>document.getElementById('pol-execution-speed').value==='0.5');
  await page.locator('#side-tab-rollout').click();
  check(`${viewport.name} saved UI multiplier reloads`,await page.evaluate(()=>window.rolloutFields().execution_speed===.5));
  const active={...status,mode:'rollout',display_mode:'rollout',rates:{...status.rates,mode:'rollout',policy_hz:15,execution_speed:.5,effective_policy_hz:7.5},task:{...status.task,execution_speed:.5,effective_policy_fps:7.5}};
  await page.evaluate(next=>window.__speedStatus(next),active);
  await pick('2');
  check(`${viewport.name} active run separate from edited next run`,(await page.locator('#rollout-speed-label').textContent()).includes('Next 2×')&&(await page.locator('#rollout-speed-live').textContent()).includes('Running: 0.5× · target 7.5 Hz'));
  check(`${viewport.name} Arm chip unchanged by speed`,Number((await page.locator('#rollout-rate .rate-chip-value').textContent()).match(/[\d.]+/)[0])===Number(armBefore.match(/[\d.]+/)[0]));
  await page.locator('#rollout-speed').click();
  await page.locator('#rollout-speed-custom').fill('1.75');
  await page.evaluate(next=>window.__speedStatus(next),active);
  check(`${viewport.name} status push preserves custom draft`,await page.locator('#rollout-speed-custom').inputValue()==='1.75');
  fs.mkdirSync(SHOTS,{recursive:true});
  await page.screenshot({path:path.join(SHOTS,`rollout-speed-${viewport.name}.png`)});
  await page.keyboard.press('Escape');
  await page.evaluate(next=>window.__speedStatus(next),status);
  await pick('8');
  check(`${viewport.name} insufficient Arm rate explained`,(await page.locator('#rollout-speed-warning').textContent()).includes('Raise the rollout Arm rate'));
  await page.locator('#btn-hdr-rollout').click();
  check(`${viewport.name} insufficient Arm blocks startup`,starts.length===0);
  await pick('0.5');
  await page.locator('#btn-hdr-rollout').click();
  await page.waitForTimeout(100);
  check(`${viewport.name} startup payload keeps base and multiplier`,starts.length===1&&starts[0].policy_fps===15&&starts[0].execution_speed===.5,JSON.stringify(starts));
  check(`${viewport.name} speed never writes Arm rate`,armUpdates.length===0);
  check(`${viewport.name} no page errors`,errors.length===0,errors.join(';'));
  await context.close();
}

async function main() {
  const port = await getFreePort();
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "lerobot-rollout-speed-"));
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
    browser = await chromium.launch({
      channel: "chrome",
      headless: process.env.ROLLOUT_SPEED_HEADED !== "1",
    });
    await runViewport(browser, { name: "1440x900", width: 1440, height: 900, port }, base);
    await runViewport(browser, { name: "390x844", width: 390, height: 844, port }, base);
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
