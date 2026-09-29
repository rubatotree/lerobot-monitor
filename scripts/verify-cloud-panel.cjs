/* Browser verification for the merged Monitor cloud panel.
 *
 * Boots the real Monitor server (virtual follower, no hardware), mocks only the
 * cloud API routes, then drives the Cloud side tab in Chromium.
 * Run: node scripts/verify-cloud-panel.cjs
 */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { spawn } = require('node:child_process');
let chromium;
try { ({ chromium } = require('playwright')); }
catch { ({ chromium } = require(process.env.CODEX_PLAYWRIGHT_PATH || 'C:/Users/Admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright')); }

const ROOT = path.resolve(__dirname, '..');
const PORT = Number(process.env.CLOUD_PANEL_PORT || 8097);
const ORIGIN = `http://127.0.0.1:${PORT}/lerobot/`;
const OUTPUT = path.resolve('.tmp_cloud_panel');

const HOSTS = [
  { id: 'h1', alias: '8x4090-server', root: '/data/zhuyutian/lerobot-monitor', port: 8091, status: 'connected', operation_status: 'idle' },
  { id: 'h2', alias: '8A6000-server', root: '/data2/zhuyutian/lerobot-monitor', port: 8091, status: 'disconnected', operation_status: 'idle' },
];
const GPUS = Array.from({ length: 8 }, (_, index) => ({
  index, uuid: `GPU-${index}-example-382ae1a6`, name: 'NVIDIA GeForce RTX 4090',
  memory_total_mb: 24564, memory_used_mb: index === 3 ? 18040 : 0, healthy: index !== 0, busy: index === 3,
  processes: index === 3 ? [{ user: 'zhuyutian', program: 'python', memory_used_mb: 18040 }] : [],
}));
const MODELS = [
  { id: 'smolvla', name: 'SmolVLA · pick and place', source_kind: 'huggingface', source: 'lerobot/smolvla_base', revision: 'main', status: 'loaded', gpu_uuid: GPUS[3].uuid },
  { id: 'pi', name: 'π0.5 base', source_kind: 'huggingface', source: 'lerobot/pi05_base', status: 'ready' },
  { id: 'act', name: 'ACT · local checkpoint', source_kind: 'path', source: '/data/zhuyutian/models/act/pretrained_model', status: 'error', error: '缺少推理依赖，请先安装模型运行环境。' },
];

function serverBinary() {
  return process.platform === 'win32'
    ? path.join(ROOT, '.venv', 'Scripts', 'python.exe')
    : path.join(ROOT, '.venv', 'bin', 'python');
}

async function waitForServer(child, timeoutMs = 90000) {
  const deadline = Date.now() + timeoutMs;
  let logs = '';
  child.stdout.on('data', (chunk) => { logs += chunk; });
  child.stderr.on('data', (chunk) => { logs += chunk; });
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`Monitor server exited early (${child.exitCode})\n${logs}`);
    try {
      const response = await fetch(ORIGIN);
      if (response.ok) return;
    } catch { /* not up yet */ }
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  throw new Error(`Monitor server did not become ready\n${logs}`);
}

async function main() {
  await fs.mkdir(OUTPUT, { recursive: true });
  const hfHome = path.join(OUTPUT, 'hf');
  await fs.mkdir(hfHome, { recursive: true });
  const child = spawn(serverBinary(), ['-m', 'lerobot_monitor', '--config', 'config.example.yaml', '--port', String(PORT)], {
    cwd: ROOT,
    env: { ...process.env, HF_HOME: hfHome, PYTHONUNBUFFERED: '1' },
    windowsHide: true,
  });
  const browser = await chromium.launch({ headless: true, channel: process.env.CLOUD_PANEL_BROWSER || 'chrome' });
  try {
    await waitForServer(child);
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    const cloudRequests = [];
    await page.route('**/lerobot/api/cloud/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      cloudRequests.push({ path: url.pathname, method: request.method() });
      let body = {};
      if (url.pathname === '/lerobot/api/cloud/hosts') body = HOSTS;
      else if (url.pathname === '/lerobot/api/cloud/jobs') body = [];
      else if (url.pathname.endsWith('/health')) body = { status: 'ok', runtime: { configured: true, profile: 'smolvla' } };
      else if (url.pathname.endsWith('/gpus')) body = GPUS;
      else if (url.pathname.endsWith('/deployments') && request.method() === 'GET') body = MODELS;
      else if (url.pathname.endsWith('/logs')) body = { lines: ['loading smolvla', 'ready'] };
      else if (url.pathname.endsWith('/jobs')) body = [{ id: 'j1', kind: 'load', status: 'succeeded', created_at: Date.now() / 1000 }];
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    });

    await page.goto(ORIGIN);
    await page.click('#side-tab-cloud');
    await page.locator('#cloud-panel .cloud-gpu').first().waitFor();
    const tabFits = await page.evaluate(() => {
      const tab = document.getElementById('side-tab-cloud').getBoundingClientRect();
      const list = document.getElementById('side-tabs').getBoundingClientRect();
      return tab.left >= list.left - 1 && tab.right <= list.right + 1;
    });
    assert.equal(tabFits, true, 'Cloud tab must be fully visible in the side tab row');
    assert.equal(await page.locator('#cloud-panel .cloud-model').count(), 3);
    assert.equal(await page.locator('#cloud-panel .cloud-job').count(), 1);
    assert.match(await page.locator('#cloud-runtime-status').textContent(), /Runtime/);
    assert.match(await page.locator('#cloud-gpu-summary').textContent(), /6 \/ 8 available/);

    for (const width of [390, 768, 1440, 1920]) {
      await page.setViewportSize({ width, height: 1000 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, `horizontal overflow at ${width}`);
      await page.screenshot({ path: path.join(OUTPUT, `cloud-panel-${width}.png`), fullPage: true });
    }

    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: '+ Add', exact: true }).click();
    await page.locator('#cloud-model-name').fill('Kept while polling');
    await page.locator('#cloud-model-kind').selectOption('path');
    await page.locator('#cloud-model-source').fill('/data/models/checkpoint');
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await page.locator('#cloud-model-name').waitFor({ state: 'hidden' });

    await page.locator('#cloud-model-list [data-cloud-model-action="load"]').first().click();
    await page.locator('#cloud-load-gpu').selectOption('GPU-1-example-382ae1a6');
    await page.click('#cloud-dialog-submit');
    await page.waitForFunction(() => !document.getElementById('cloud-dialog')?.open);
    const loadCall = cloudRequests.find((call) => call.path.endsWith('/pi/load'));
    assert.ok(loadCall && loadCall.method === 'POST', 'load must POST to the same-origin proxy');
    assert.equal(cloudRequests.some((call) => call.path.startsWith('/api/hosts/')), false, 'must not use the standalone manager paths');

    await page.locator('#cloud-model-list [data-cloud-model-action="logs"]').first().click();
    await page.waitForFunction(() => document.getElementById('cloud-log-content').textContent.includes('ready'));
    assert.match(await page.locator('#cloud-log-content').textContent(), /loading smolvla/);

    await page.screenshot({ path: path.join(OUTPUT, 'cloud-panel-actions-390.png'), fullPage: true });
    assert.deepEqual(errors, []);
    console.log(`PASS: merged cloud panel rendered at four widths with no overflow or console errors. Screenshots: ${OUTPUT}`);
  } finally {
    await browser.close();
    child.kill();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
