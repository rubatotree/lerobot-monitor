// Browser verification for model residency actions.
// Uses a temporary monitor config/store, synthetic status messages and an
// intercepted model requests; no policy or physical hardware is started.
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
const SHOTS = process.env.MODEL_RESIDENCY_SHOTS
  || path.join(os.tmpdir(), "lerobot-model-residency-shots");
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
  const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, deviceScaleFactor: viewport.width < 500 ? 2 : 1 });
  const page = await context.newPage();
  const errors = [], requests = [], replies = [];
  page.on('pageerror', error => errors.push(error.message));
  const instance = (id, state) => ({ id, state, device: 'cuda:0', gpu_bytes: 400000000,
    phase: 'weights', completed_steps: 3, total_steps: 8, elapsed_ms: 5600, phase_elapsed_ms: 1900,
    can_cancel_load: ['queued', 'loading'].includes(state), can_release: state === 'in_use', can_unload: ['ready', 'cancelled', 'error'].includes(state) });
  const residency = (state, id = 'first') => ({ state, instances: [instance(id, state)], gpu_bytes: 400000000,
    can_cancel_load: ['queued', 'loading'].includes(state), can_release: state === 'in_use', can_unload: ['ready', 'cancelled', 'error'].includes(state) });
  const models = [
    { id: 'owner/policy', path: '/synthetic/one', name: 'Policy loading', playable: true, metadata: {}, residency: residency('queued') },
    { id: 'other/policy', path: '/synthetic/two', name: 'Policy running', playable: true, metadata: {}, residency: residency('in_use', 'other') },
  ];
  let states = Object.fromEntries(models.map(model => [model.path, model.residency]));
  const status = () => ({ ...base, model_residency: states });
  await page.route('**/api/models', route => route.fulfill({ json: models }));
  await page.route('**/api/status', route => route.fulfill({ json: status() }));
  await page.route('**/api/models/**', async route => {
    requests.push({ url: route.request().url(), body: route.request().postDataJSON() });
    const reply = replies.shift();
    if (!reply) throw new Error('Unexpected model request: ' + route.request().url());
    await route.fulfill(await reply());
  });
  await page.addInitScript(initial => {
    class SyntheticWebSocket {
      constructor() {
        this.readyState = 1;
        window.__modelsEmit = next => this.onmessage?.({ data: JSON.stringify({ ...next, ts: Date.now() / 1000 }) });
        setTimeout(() => { this.onopen?.({}); window.__modelsEmit(initial); }, 100);
      }
      close() { this.readyState = 3; }
      send() {}
    }
    window.WebSocket = SyntheticWebSocket;
  }, status());
  await page.goto(`http://127.0.0.1:${viewport.port}/lerobot/`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => !!window.runModelResidencyAction);
  const row = index => page.locator('#md-list .library-item').nth(index);
  const button = (action, index = 0) => row(index).locator(`[data-residency-action="${action}"]`);
  const badge = (index = 0) => row(index).locator('.model-residency-badge');
  await row(0).waitFor();
  const emit = async (first, second = states['/synthetic/two']) => {
    states = { '/synthetic/one': first, '/synthetic/two': second };
    await page.evaluate(next => window.__modelsEmit(next), status());
  };
  const expectBadge = text => page.waitForFunction(text => document.querySelector('#md-list .model-residency-badge')?.textContent === text, text);
  const assert = (name, ok, detail = '') => check(`${viewport.name} ${name}`, ok, detail);
  const respond = data => replies.push(async () => ({ json: data }));
  const delayed = () => { let finish; replies.push(() => new Promise(resolve => { finish = resolve; })); return data => finish({ json: data }); };
  const waitRequest = async count => {
    const deadline = Date.now() + 5000;
    while (requests.length < count && Date.now() < deadline) await page.waitForTimeout(10);
    if (requests.length !== count) throw new Error('Request count mismatch');
  };
  await emit(residency('queued'));
  assert('queued cancellation available', await button('cancel-load').isEnabled());
  assert('queued Load and Unload disabled', await button('load').isDisabled() && await button('unload').isDisabled());
  assert('active release explains stopping and retained weights', /Stop.*task.*Keep weights loaded/.test(await button('release', 1).getAttribute('title')));
  const finishCancel = delayed();
  await button('cancel-load').click(); await waitRequest(1);
  assert('pending cancellation labelled accessibly', await button('cancel-load').getAttribute('aria-busy') === 'true' && await button('cancel-load').isDisabled());
  await emit(residency('loading'));
  await page.evaluate(model => window.runModelResidencyAction(model, 'cancel-load'), models[0]);
  assert('duplicate request ignored after push rerender', requests.length === 1 && await button('cancel-load').isDisabled());
  // The HTTP snapshot is stale: a later pushed terminal state must win.
  await emit(residency('cancelled'));
  finishCancel({ ok: true, count: 1, residency: residency('cancelling') });
  await page.waitForFunction(() => !document.querySelector('#md-list [data-residency-action="load"]').disabled);
  assert('newer terminal push wins delayed response', await badge().textContent() === 'Cancelled');
  assert('cancelled load can retry', await button('load').textContent() === 'Retry');
  assert('model ID encoded and aggregate body empty', requests[0].url.includes('owner%2Fpolicy/cancel-load') && Object.keys(requests[0].body).length === 0);
  states['/cache/policy'] = { ...residency('queued', 'retry'), source_paths:['/synthetic/one', '/cache/policy'] };
  delete states['/synthetic/one'];
  respond({ ok: true, accepted: true, instance_id: 'retry' });
  await button('load').click(); await expectBadge('Queued');
  assert('Retry uses compatible load endpoint', requests[1].url.endsWith('/load'));
  assert('GET refresh resolves canonical cache alias', await badge().textContent() === 'Queued');
  states['/cache/policy'] = { ...residency('cancelling'), source_paths:['/synthetic/one', '/cache/policy'] };
  await page.evaluate(next => window.__modelsEmit(next), status());
  assert('pushed canonical cache alias retains cancellation', await badge().textContent() === 'Cancelling' && await button('load').isDisabled());
  await emit(residency('loading'));
  respond({ ok: true, count: 0, residency: residency('ready') });
  await button('cancel-load').click(); await expectBadge('Ready');
  assert('stale operation shows neutral outcome', (await row(0).textContent()).includes('No matching operation remains'));
  await emit(residency('loading'));
  replies.push(async () => ({ status: 409, json: { detail: 'Synthetic load changed; retry' } }));
  await button('cancel-load').click();
  await row(0).locator('.model-residency-error').waitFor();
  assert('failure visible and capability restored', (await row(0).textContent()).includes('Synthetic load changed') && await button('cancel-load').isEnabled());
  const multi = residency('in_use');
  multi.instances = [instance('load-2', 'loading'), instance('use-2', 'in_use')];
  multi.can_cancel_load = true;
  await emit(multi);
  await row(0).locator('.lib-item-title').click();
  assert('selected details expose instance operations', await button('cancel-load-load-2').isEnabled() && await button('release-use-2').isEnabled());
  const cancelledMulti = structuredClone(multi);
  cancelledMulti.instances[0] = instance('load-2', 'cancelling'); cancelledMulti.can_cancel_load = false;
  respond({ ok: true, count: 1, residency: cancelledMulti });
  await button('cancel-load-load-2').click();
  await page.waitForFunction(() => document.querySelector('[data-residency-action="cancel-load-load-2"]')?.textContent === 'Cancelling…');
  await page.waitForTimeout(70);
  assert('cancel addresses selected instance', requests.at(-1).body.instance_id === 'load-2');
  assert('cooperative cancellation remains pending', await button('cancel-load-load-2').isDisabled() && (await row(0).textContent()).includes('current loading step'));
  const stopReply = delayed();
  await button('release-use-2').click(); await waitRequest(6);
  assert('release addresses selected lease', requests.at(-1).body.instance_id === 'use-2');
  const stoppingMulti = structuredClone(cancelledMulti);
  stoppingMulti.instances[1] = instance('use-2', 'stopping'); stoppingMulti.state = 'stopping'; stoppingMulti.can_release = false;
  stopReply({ ok: true, count: 1, residency: stoppingMulti });
  await expectBadge('Stopping');
  assert('stopping disables repeat release and unload', await button('release-use-2').isDisabled() && await button('unload').isDisabled());
  assert('other model remains in use', await badge(1).textContent() === 'In use' && await button('release', 1).isEnabled());
  assert('release does not call Unload', !requests.some(item => item.url.endsWith('/unload')));
  assert('retained weights explained', (await row(0).textContent()).includes('Model weights stay loaded'));
  const geometry = await row(0).evaluate(el => { const r = el.getBoundingClientRect(); return { x:r.x, right:r.right, width:innerWidth, overflow:el.scrollWidth-el.clientWidth }; });
  assert('actions fit library card', geometry.x >= 0 && geometry.right <= geometry.width + 1 && geometry.overflow <= 1, JSON.stringify(geometry));
  fs.mkdirSync(SHOTS, { recursive: true });
  await row(0).evaluate(el => { for (let parent = el.parentElement; parent; parent = parent.parentElement) parent.scrollTop = 0; });
  await page.screenshot({ path:path.join(SHOTS, `model-actions-${viewport.name}.png`) });
  await emit(residency('ready'));
  assert('released model ready and unloadable', await button('unload').isEnabled() && await badge().textContent() === 'Ready');
  const releaseReply = delayed();
  await button('release', 1).click(); await waitRequest(7);
  await emit(residency('ready'), residency('ready', 'other'));
  releaseReply({ ok:true, count:1, residency:residency('stopping', 'other') });
  await page.waitForTimeout(100);
  assert('release response cannot regress newer ready push', await badge(1).textContent() === 'Ready' && await button('unload',1).isEnabled());
  assert('aggregate release has empty body', Object.keys(requests.at(-1).body).length === 0);
  assert('no browser exceptions', errors.length === 0, errors.join(';'));
  await context.close();
}
async function main() {
  const port = await getFreePort();
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "lerobot-model-actions-"));
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
      headless: process.env.MODEL_RESIDENCY_HEADED !== "1",
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
