/* Browser verification for the Add model dialog's Local/Cloud tabs.
 *
 * Boots the real Monitor server (virtual follower, no hardware), mocks only the
 * cloud routes, then drives the model dialog in Chromium.
 * Run: node scripts/verify-library-model-tabs.cjs
 */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { spawn } = require('node:child_process');
let chromium;
try { ({ chromium } = require('playwright')); }
catch { ({ chromium } = require(process.env.CODEX_PLAYWRIGHT_PATH || 'C:/Users/Admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright')); }

const ROOT = path.resolve(__dirname, '..');
const PORT = Number(process.env.LIBRARY_TABS_PORT || 8098);
const ORIGIN = `http://127.0.0.1:${PORT}/lerobot/`;
const OUTPUT = path.resolve('.tmp_library_tabs');
const ADDRESS = '#library-modal-pane-local input[aria-label="Upstream repo_id or local path"]';
const SUBMIT = '#library-modal-body .library-modal-actions button';
const STATUS = '#library-modal-body .library-modal-status';

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
      if ((await fetch(ORIGIN)).ok) return;
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
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    const modelCalls = [];
    await page.route('**/lerobot/api/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const json = (body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
      if (url.pathname === '/lerobot/api/cloud/hosts') return json([{ id: 'h1', alias: '8x4090-server', status: 'connected' }]);
      if (url.pathname === '/lerobot/api/cloud/hosts/h1/connect') {
        return json({ deployments: [
          { id: 'd1', name: 'ACT · classify blocks', status: 'loaded' },
          { id: 'd2', name: 'SmolVLA base', status: 'ready' },
        ] });
      }
      if (url.pathname === '/lerobot/api/models/cloud') {
        modelCalls.push(JSON.parse(request.postData() || '{}'));
        return json({ id: 'cloud://h1/d1', name: 'ACT' });
      }
      return route.continue();
    });

    await page.goto(ORIGIN);
    await page.click('#lib-tab-models');
    await page.click('#btn-md-add');
    await page.locator('#library-modal-body').waitFor();
    assert.equal(await page.locator('#library-modal-tab-local').getAttribute('aria-selected'), 'true');
    assert.equal(await page.locator('#library-modal-tab-cloud').getAttribute('aria-selected'), 'false');
    assert.equal(await page.locator(ADDRESS).isVisible(), true, 'Local tab shows the address field');
    assert.equal(await page.locator('#library-modal-pane-cloud').isVisible(), false, 'Cloud fields stay hidden on the Local tab');

    // Local validation still works and must not hit the cloud API.
    await page.click(SUBMIT);
    await page.waitForFunction((selector) => document.querySelector(selector)?.textContent.includes('Enter an address'), STATUS);
    assert.equal(modelCalls.length, 0);
    await page.screenshot({ path: path.join(OUTPUT, 'add-model-local.png') });

    // Switching to Cloud hides every local field, including the address input.
    await page.click('#library-modal-tab-cloud');
    assert.equal(await page.locator('#library-modal-pane-local').isVisible(), false);
    assert.equal(await page.locator(ADDRESS).isVisible(), false, 'Cloud tab must not ask for a local address');
    assert.equal(await page.locator(SUBMIT).textContent(), 'Add cloud model');
    await page.waitForFunction((selector) => !document.querySelector(selector), STATUS);

    await page.click('#library-modal-pane-cloud button');
    await page.waitForFunction(() => document.querySelectorAll('#library-modal-pane-cloud select')[1].options.length === 2);
    await page.screenshot({ path: path.join(OUTPUT, 'add-model-cloud.png') });

    await page.click(SUBMIT);
    await page.waitForFunction(() => document.getElementById('library-modal').classList.contains('hidden'));
    assert.equal(modelCalls.length, 1, 'cloud submit must post exactly one registration');
    assert.deepEqual(modelCalls[0], { host_id: 'h1', deployment_id: 'd1', name: '' });

    // The Local tab still works after the round trip.
    await page.click('#btn-md-add');
    assert.equal(await page.locator(ADDRESS).isVisible(), true);
    await page.click('#library-modal-tab-cloud');
    await page.click('#library-modal-tab-local');
    assert.equal(await page.locator(ADDRESS).isVisible(), true);

    for (const width of [390, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      await page.click('#library-modal-tab-cloud');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, `horizontal overflow at ${width}`);
      await page.screenshot({ path: path.join(OUTPUT, `add-model-cloud-${width}.png`), fullPage: true });
      await page.click('#library-modal-tab-local');
      await page.screenshot({ path: path.join(OUTPUT, `add-model-local-${width}.png`), fullPage: true });
    }

    assert.deepEqual(errors, []);
    console.log(`PASS: Add model dialog switches between Local and Cloud tabs; cloud never asks for a local address. Screenshots: ${OUTPUT}`);
  } finally {
    await browser.close();
    child.kill();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
