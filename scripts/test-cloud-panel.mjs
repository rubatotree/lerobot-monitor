import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import {
  createCloudPanel,
  deploymentActions,
  escapeHTML,
  gpuAvailable,
  statusClass,
} from '../src/lerobot_monitor/web/static/cloud-panel.js';

const require = createRequire(import.meta.url);
let JSDOM = null;
try { ({ JSDOM } = require('jsdom')); } catch { JSDOM = null; }

const panelMarkup = async () => {
  const html = await readFile(new URL('../src/lerobot_monitor/web/static/index.html', import.meta.url), 'utf8');
  const panel = html.match(/<section class="panel side-tab-panel" id="cloud-panel"[\s\S]*?<\/section>/)?.[0];
  const dialog = html.match(/<dialog id="cloud-dialog"[\s\S]*?<\/dialog>/)?.[0];
  const logDialog = html.match(/<dialog id="cloud-log-dialog"[\s\S]*?<\/dialog>/)?.[0];
  assert.ok(panel && dialog && logDialog, 'cloud panel markup must exist in index.html');
  return `<!doctype html><html data-base="/lerobot"><body>${panel}${dialog}${logDialog}</body></html>`;
};

const pause = () => new Promise((resolve) => setTimeout(resolve, 0));
async function until(predicate) {
  for (let index = 0; index < 200; index += 1) {
    if (predicate()) return;
    await pause();
  }
  assert.ok(predicate(), 'operation did not finish');
}

async function fixture(t) {
  const dom = new JSDOM(await panelMarkup(), { url: 'http://127.0.0.1:8090/lerobot/' });
  const doc = dom.window.document;
  doc.getElementById('cloud-panel').hidden = false;
  // jsdom defaults document.hidden to true; the panel only polls a visible page.
  Object.defineProperty(doc, 'hidden', { configurable: true, get: () => false });
  dom.window.HTMLDialogElement.prototype.showModal = function showModal() { this.open = true; };
  dom.window.HTMLDialogElement.prototype.close = function close() { this.open = false; };
  const oldFormData = globalThis.FormData;
  globalThis.FormData = dom.window.FormData;
  const hosts = [
    { id: 'h1', alias: '8x4090-server', root: '/data/me/lerobot-monitor', port: 8091, status: 'connected', operation_status: 'idle' },
    { id: 'h2', alias: '8A6000-server', root: '/data2/me/lerobot-monitor', port: 8091, status: 'disconnected', operation_status: 'idle' },
  ];
  const models = [{ id: 'm1', name: 'SmolVLA', source_kind: 'huggingface', source: 'lerobot/smolvla_base', status: 'ready' }];
  const gpus = [
    { index: 1, uuid: 'GPU-free', name: 'RTX 4090', healthy: true, memory_total_mb: 24576, memory_used_mb: 0 },
    { index: 2, uuid: 'GPU-busy', name: 'RTX 4090', healthy: true, busy: true, memory_total_mb: 24576, memory_used_mb: 20000 },
    { index: 0, uuid: 'GPU-broken', name: 'RTX 4090', healthy: false },
  ];
  const calls = [];
  let failure = null;
  const fetcher = async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : undefined;
    calls.push({ url, method: options.method || 'GET', body, headers: options.headers });
    if (failure && url.includes(failure.path)) {
      return new Response(JSON.stringify({ detail: failure.message }), { status: failure.status });
    }
    let result = {};
    if (url === '/lerobot/api/cloud/hosts') result = hosts;
    else if (url === '/lerobot/api/cloud/jobs') result = [];
    else if (url.endsWith('/health')) result = { status: 'ok', runtime: { configured: true, profile: 'smolvla' } };
    else if (url.endsWith('/gpus')) result = gpus;
    else if (url.endsWith('/deployments') && (options.method || 'GET') === 'GET') result = models;
    else if (url.endsWith('/jobs')) result = [{ id: 'j1', kind: 'load', status: 'succeeded' }];
    else if (url.endsWith('/logs')) result = { lines: ['first line', 'second line'] };
    else result = { ok: true };
    return new Response(JSON.stringify(result), { status: 200, headers: { 'Content-Type': 'application/json' } });
  };
  const app = createCloudPanel(doc, { fetcher });
  await app.init();
  t.after(() => {
    app.stop();
    globalThis.FormData = oldFormData;
    dom.window.close();
  });
  const click = (selector) => {
    const element = doc.querySelector(selector);
    assert.ok(element, selector);
    element.click();
  };
  const set = (selector, value) => {
    const element = doc.querySelector(selector);
    assert.ok(element, selector);
    element.value = value;
  };
  const submit = () => doc.getElementById('cloud-dialog-form')
    .dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }));
  return { dom, doc, app, calls, hosts, models, gpus, click, set, submit, fail: (value) => { failure = value; } };
}

test('pure helpers keep explicit boundaries', () => {
  assert.equal(escapeHTML('<img src=x onerror="alert(1)">'), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
  assert.equal(statusClass('loaded'), 'good');
  assert.equal(statusClass('failed'), 'bad');
  assert.equal(statusClass('loading'), 'warn');
  assert.equal(statusClass('unknown'), 'neutral');
  assert.equal(gpuAvailable({ uuid: 'GPU-1', healthy: false }), false);
  assert.equal(gpuAvailable({ uuid: 'GPU-1', busy: true }), false);
  assert.equal(gpuAvailable({ uuid: 'GPU-1' }, [{ gpu_uuid: 'GPU-1', status: 'loading' }]), false);
  assert.equal(gpuAvailable({ uuid: 'GPU-1' }, []), true);
  assert.deepEqual(deploymentActions({ status: 'loaded' }), { load: false, unload: true, remove: false });
  assert.deepEqual(deploymentActions({ status: 'ready' }), { load: true, unload: false, remove: true });
});

test('panel renders host, GPU, deployment and job state from the Monitor API', { skip: !JSDOM }, async (t) => {
  const f = await fixture(t);
  await until(() => f.app.state.connected);
  assert.equal(f.doc.getElementById('cloud-host-select').value, 'h1');
  assert.equal(f.doc.querySelectorAll('.cloud-gpu').length, 3);
  assert.equal(f.doc.querySelectorAll('.cloud-model').length, 1);
  assert.equal(f.doc.querySelectorAll('.cloud-job').length, 1);
  assert.equal(f.doc.getElementById('cloud-gpu-summary').textContent, '1 / 3 available');
  assert.match(f.doc.getElementById('cloud-runtime-status').textContent, /Runtime/);
  assert.equal(f.calls.every((call) => call.url.startsWith('/lerobot/api/cloud/')), true);
  assert.equal(f.calls.some((call) => call.url.includes('/cloud/api/v1/')), true);
});

test('load action sends the explicitly chosen available GPU', { skip: !JSDOM }, async (t) => {
  const f = await fixture(t);
  await until(() => f.app.state.connected);
  f.click('[data-cloud-model-action="load"]');
  const select = f.doc.getElementById('cloud-load-gpu');
  assert.equal(select.options.length, 2);
  assert.equal(select.options[1].value, 'GPU-free');
  f.set('#cloud-load-gpu', 'GPU-free');
  f.submit();
  await until(() => f.calls.some((call) => call.url.endsWith('/m1/load')));
  const request = f.calls.find((call) => call.url.endsWith('/m1/load'));
  assert.equal(request.method, 'POST');
  assert.deepEqual(request.body, { gpu_uuid: 'GPU-free' });
});

test('deploy dialog posts to the same-origin cloud proxy', { skip: !JSDOM }, async (t) => {
  const f = await fixture(t);
  await until(() => f.app.state.connected);
  f.click('#cloud-model-add');
  f.set('#cloud-model-name', 'Local ACT');
  f.set('#cloud-model-kind', 'path');
  f.set('#cloud-model-source', '/data/models/act');
  f.submit();
  await until(() => f.calls.some((call) => call.url.endsWith('/cloud/api/v1/deployments') && call.method === 'POST'));
  const request = f.calls.find((call) => call.url.endsWith('/cloud/api/v1/deployments') && call.method === 'POST');
  assert.equal(request.body.name, 'Local ACT');
  assert.equal(request.body.source_kind, 'path');
  assert.equal(request.body.source, '/data/models/act');
});

test('upload source uses the manager upload endpoint', { skip: !JSDOM }, async (t) => {
  const f = await fixture(t);
  await until(() => f.app.state.connected);
  f.click('#cloud-model-add');
  f.set('#cloud-model-name', 'Checkpoint');
  f.set('#cloud-model-kind', 'upload');
  f.set('#cloud-model-source', 'D:\\models\\checkpoint');
  f.submit();
  await until(() => f.calls.some((call) => call.url.endsWith('/h1/upload')));
  const request = f.calls.find((call) => call.url.endsWith('/h1/upload'));
  assert.equal(request.method, 'POST');
  assert.deepEqual(request.body, { name: 'Checkpoint', path: 'D:\\models\\checkpoint' });
  assert.equal(f.calls.some((call) => call.method === 'POST' && call.url.endsWith('/cloud/api/v1/deployments')), false);
});

test('host actions call the Monitor cloud manager and render unsafe text as text', { skip: !JSDOM }, async (t) => {
  const f = await fixture(t);
  await until(() => f.app.state.connected);
  f.click('[data-cloud-host-action="probe"]');
  await until(() => f.calls.some((call) => call.url.endsWith('/h1/probe')));
  assert.equal(f.calls.find((call) => call.url.endsWith('/h1/probe')).method, 'POST');
  f.models[0].name = '<img src=x onerror=alert(1)>';
  await f.app.refresh();
  assert.equal(f.doc.querySelector('#cloud-model-list img'), null);
  assert.ok(f.doc.getElementById('cloud-model-list').textContent.includes('<img'));
  f.click('[data-cloud-model-action="logs"]');
  await until(() => f.doc.getElementById('cloud-log-content').textContent.includes('first line'));
  assert.equal(f.doc.getElementById('cloud-log-content').textContent, 'first line\nsecond line');
});

test('failed request keeps the dialog open with the server detail', { skip: !JSDOM }, async (t) => {
  const f = await fixture(t);
  await until(() => f.app.state.connected);
  f.click('#cloud-model-add');
  f.set('#cloud-model-name', 'Example');
  f.set('#cloud-model-kind', 'path');
  f.set('#cloud-model-source', '/data/models/x');
  f.fail({ path: '/cloud/api/v1/deployments', status: 409, message: 'A deployment already uses this source.' });
  f.submit();
  await until(() => !f.doc.getElementById('cloud-dialog-error').hidden);
  assert.match(f.doc.getElementById('cloud-dialog-error').textContent, /already uses/);
  assert.equal(f.doc.getElementById('cloud-dialog').open, true);
  assert.equal(f.doc.getElementById('cloud-model-name').value, 'Example');
});
