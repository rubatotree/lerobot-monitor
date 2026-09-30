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
        original_steps: 16, prefix_trimmed: 4, accepted_steps: 12,
        consumed_steps: 4, dispatched_steps: 4, remaining_steps: 8,
        accepted_at: active - 0.005, last_dispatched: active + 0.1,
        status: chunk < lastChunk ? "replaced" : "active",
        replaced_at: chunk < lastChunk ? active + 0.2 : null,
        replaced_by: chunk < lastChunk ? chunk + 2 : null,
        replaced_steps: chunk < lastChunk ? 6 : 0,
        stages: [{ name: "model", start: active - .065, end: active - .015, gpu_ms: 31.5 }],
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
        run_id: "synthetic-run", epoch_ts: startedAt - rolloutAge,
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
        const timeShift = Date.now() / 1000 - stream[0].ts;
        const encode = frame => JSON.stringify({ ...frame, ts: frame.ts + timeShift,
          rollout_timeline: frame.rollout_timeline ? { ...frame.rollout_timeline, epoch_ts: frame.rollout_timeline.epoch_ts + timeShift } : null });
        window.__rolloutEmit = frame => this.onmessage?.({ data: encode(frame) });
        window.__rolloutEmitAbsolute = frame => this.onmessage?.({ data: JSON.stringify(frame) });
        setTimeout(() => {
          if (this.readyState === 3) return;
          this.readyState = 1;
          if (this.onopen) this.onopen({});
          let index = 0;
          const emit = () => {
            if (this.readyState === 3) return;
            const frame = stream[Math.min(index, stream.length - 1)];
            if (this.onmessage) this.onmessage({ data: encode(frame) });
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
    deviceScaleFactor: viewport.name.startsWith("390") ? 2 : 1,
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
  check(`${viewport.name} lane height reserved`, stats && stats.laneHeight === 64, JSON.stringify(stats));
  check(`${viewport.name} input markers rendered`, stats && stats.inputs > 0, JSON.stringify(stats));
  check(`${viewport.name} chunk spans rendered`, stats && stats.chunks > 1, JSON.stringify(stats));
  check(`${viewport.name} inference spans rendered`, stats && stats.inferences > 0, JSON.stringify(stats));
  check(`${viewport.name} overlap spans rendered`, stats && stats.overlaps > 0, JSON.stringify(stats));
  check(`${viewport.name} green lane pixels visible`, stats && stats.green > 20, JSON.stringify(stats));
  check(`${viewport.name} blue inference pixels visible`, stats && stats.blue > 10, JSON.stringify(stats));

  // Dragging the now line trades visible past for visible future at a fixed span.
  await page.locator("#chart-action").evaluate((element) => element.scrollIntoView({ block: "center" }));
  await page.waitForTimeout(200);
  const dragStart = await page.evaluate(() => {
    const chart = window.Chart.getChart(document.getElementById("chart-action"));
    const cursor = chart.$replayCursorElement;
    const rect = cursor.getBoundingClientRect();
    return {
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
      draggable: cursor.classList.contains("draggable"),
      laneHeight: Number(chart.$laneHeight) || 0,
      min: chart.scales.x.min,
      max: chart.scales.x.max,
      now: chart.$nowTime,
    };
  });
  check(`${viewport.name} now line exposes a drag handle`, dragStart.draggable, JSON.stringify(dragStart));
  await page.mouse.move(dragStart.x, dragStart.y);
  await page.mouse.down();
  await page.mouse.move(dragStart.x - 60, dragStart.y, { steps: 6 });
  await page.mouse.up();
  await page.waitForTimeout(250);
  const dragged = await page.evaluate(() => {
    const chart = window.Chart.getChart(document.getElementById("chart-action"));
    return {
      min: chart.scales.x.min,
      max: chart.scales.x.max,
      now: chart.$nowTime,
      laneHeight: Number(chart.$laneHeight) || 0,
      stored: localStorage.getItem("lerobot-monitor-rollout-now-fraction"),
    };
  });
  const spanStart = dragStart.max - dragStart.min;
  const spanDragged = dragged.max - dragged.min;
  const fractionStart = (dragStart.now - dragStart.min) / spanStart;
  const fractionDragged = (dragged.now - dragged.min) / spanDragged;
  check(`${viewport.name} dragging the now line shifts the window`, fractionDragged < fractionStart - 0.02,
    `${fractionStart.toFixed(3)} -> ${fractionDragged.toFixed(3)}`);
  check(`${viewport.name} drag keeps the window span`, Math.abs(spanDragged - spanStart) < 0.25,
    `${spanStart.toFixed(3)} -> ${spanDragged.toFixed(3)}`);
  check(`${viewport.name} drag keeps the lane band`, dragged.laneHeight === 64, JSON.stringify(dragged));
  check(`${viewport.name} drag stores the fraction`,
    Number(dragged.stored) > 0 && Number(dragged.stored) < 1, String(dragged.stored));
  const resetProbe = await page.evaluate(() => {
    const chart = window.Chart.getChart(document.getElementById("chart-action"));
    const cursor = chart.$replayCursorElement;
    const rect = cursor.getBoundingClientRect();
    const scaleSelect = document.getElementById("chart-scale");
    const scaleSeconds = Number(scaleSelect && scaleSelect.value) || 10;
    const future = Math.min(8, Math.max(1, scaleSeconds * 0.25));
    return {
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
      expected: scaleSeconds / (scaleSeconds + future),
    };
  });
  await page.mouse.dblclick(resetProbe.x, resetProbe.y);
  await page.waitForTimeout(250);
  const reset = await page.evaluate(() => {
    const chart = window.Chart.getChart(document.getElementById("chart-action"));
    return {
      min: chart.scales.x.min,
      max: chart.scales.x.max,
      now: chart.$nowTime,
      stored: localStorage.getItem("lerobot-monitor-rollout-now-fraction"),
    };
  });
  const fractionReset = (reset.now - reset.min) / (reset.max - reset.min);
  check(`${viewport.name} double-click restores the default split`,
    Math.abs(fractionReset - resetProbe.expected) < 0.04 && reset.stored === null,
    `${fractionReset.toFixed(3)} vs ${resetProbe.expected.toFixed(3)} stored=${reset.stored}`);

  await page.locator("#chart-action").evaluate((element) => element.scrollIntoView({ block: "center" }));
  await page.waitForTimeout(120);
  const tooltipState = await page.evaluate(() => {
    const canvas = document.getElementById("chart-action");
    const chart = window.Chart.getChart(canvas);
    const lanes = chart && chart.$rolloutLanes;
    const segment = chart.$ribbonSegments?.find(s => s.phase === "action");
    if (!segment) return null;
    return { x: (segment.x1 + segment.x2) / 2, y: (segment.y1 + segment.y2) / 2 };

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

  await page.locator("#chart-freeze").click();
  check(`${viewport.name} freeze button active`, await page.locator("#chart-freeze").getAttribute("aria-pressed") === "true");
  const frozenBefore = await page.evaluate(() => {
    const charts = ["chart-action","chart-state"].map(id => window.Chart.getChart(document.getElementById(id)));
    return charts.map(c => c && ({ now:c.$nowTime, data:JSON.stringify(c.data.datasets.map(ds=>ds.data)), lanes:JSON.stringify(c.$rolloutLanes), live:c.$liveSource?.[0]?.$raw.length }));
  });
  const futureFrame = structuredClone(frames[frames.length-1]);
  futureFrame.ts += 1; futureFrame.rollout_timeline.blocks[0].stages[0].gpu_ms = 999;
  await page.evaluate(frame => window.__rolloutEmit(frame), futureFrame);
  await page.waitForTimeout(150);
  const frozenAfter = await page.evaluate(() => ["chart-action","chart-state"].map(id => {
    const c=window.Chart.getChart(document.getElementById(id));
    return c && ({ now:c.$nowTime, data:JSON.stringify(c.data.datasets.map(ds=>ds.data)), lanes:JSON.stringify(c.$rolloutLanes), live:c.$liveSource?.[0]?.$raw.length });
  }));
  check(`${viewport.name} both frozen charts retain time curves and lanes`, JSON.stringify(frozenBefore.map(c=>c&&[c.now,c.data,c.lanes])) === JSON.stringify(frozenAfter.map(c=>c&&[c.now,c.data,c.lanes])));
  check(`${viewport.name} frozen live cache still ingests`, frozenAfter[0].live > frozenBefore[0].live);
  const stageToggle=page.locator("#action-legend .chart-legend-item").filter({hasText:"Inference stages"}).locator("input");
  await stageToggle.check();
  check(`${viewport.name} frozen stage expansion works`, await page.evaluate(()=>window.Chart.getChart(document.getElementById("chart-action")).$ribbonSegments.some(s=>s.stage==='model')));
  await page.locator("#chart-scale").selectOption("10");
  check(`${viewport.name} frozen scale preserves captured stage data`, await page.evaluate(()=>window.Chart.getChart(document.getElementById("chart-action")).$rolloutLanes.ribbons[0].block.stages[0].gpu_ms!==999));
  await page.locator("#chart-freeze").click();
  await page.waitForTimeout(150);
  check(`${viewport.name} unfreeze restores current timeline`, await page.evaluate(()=>window.Chart.getChart(document.getElementById("chart-action")).$rolloutLanes.ribbons[0].block.stages[0].gpu_ms===999));

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


  // A fresh two-chunk scene makes phase labels and full-card layout inspectable.
  const detail = structuredClone(frames[frames.length - 1]);
  detail.rollout_timeline.run_id = "detail-run";
  detail.rollout_timeline.blocks = [
    { id: 501, kind:"rtc", start:detail.rollout_timeline.t_s-1.5, end:detail.rollout_timeline.t_s-.9,
      accepted_at:detail.rollout_timeline.t_s-.89, active:detail.rollout_timeline.t_s-.8,
      steps:45, original_steps:50, prefix_trimmed:5, accepted_steps:45, consumed_steps:24,
      dispatched_steps:24, remaining_steps:0, replaced_steps:21, replaced_by:502,
      replaced_at:detail.rollout_timeline.t_s-.4, action_end:detail.rollout_timeline.t_s-.3,
      step_s:1/30,status:"replaced", stages:[{name:"model",start:detail.rollout_timeline.t_s-1.48,end:detail.rollout_timeline.t_s-.92,gpu_ms:301}] },
    { id:502,kind:"rtc",start:detail.rollout_timeline.t_s-1.96,end:detail.rollout_timeline.t_s-.02,
      accepted_at:detail.rollout_timeline.t_s-.05,active:detail.rollout_timeline.t_s-.3,
      steps:45,original_steps:51,prefix_trimmed:6,accepted_steps:45,consumed_steps:9,dispatched_steps:9,
      remaining_steps:36,replaced_steps:0,step_s:1/30,status:"active",
      // Cloud RTC chunk: the round trip is split into its transfer and compute legs.
      stages:[
        {name:"cloud_encode",start:detail.rollout_timeline.t_s-1.94,end:detail.rollout_timeline.t_s-1.90,gpu_ms:null},
        {name:"cloud_upload",start:detail.rollout_timeline.t_s-1.90,end:detail.rollout_timeline.t_s-.50,gpu_ms:null},
        {name:"cloud_compute",start:detail.rollout_timeline.t_s-.50,end:detail.rollout_timeline.t_s-.06,gpu_ms:288},
        {name:"cloud_download",start:detail.rollout_timeline.t_s-.06,end:detail.rollout_timeline.t_s-.03,gpu_ms:null}] }
  ];
  detail.prediction = { ...detail.prediction,id:1,t_s:detail.rollout_timeline.blocks[1].active,actions:detail.prediction.actions.slice(0,2) };
  // Deliver at the current browser-relative wall time while preserving relative event times.
  await page.evaluate(frame => {
    const chart=window.Chart.getChart(document.getElementById("chart-action"));
    const now=chart.$nowTime;
    frame.ts=now;
    frame.rollout_timeline.epoch_ts=now-frame.rollout_timeline.t_s;
    window.__rolloutEmitAbsolute(frame);
  }, detail);
  await page.locator("#chart-scale").selectOption("2");
  await page.waitForTimeout(120);
  check(`${viewport.name} new run clears old predictions`,await page.evaluate(()=>window.Chart.getChart(document.getElementById("chart-action")).$predictions.every(ds=>ds.data.length===2)));
  const cloudLane=await page.evaluate(()=>{
    const c=window.Chart.getChart(document.getElementById("chart-action"));
    const stages=(c.$ribbonSegments||[]).filter(s=>s.ribbon.id===502&&s.stage);
    const ribbon=(c.$rolloutLanes.ribbons||[]).find(r=>r.id===502);
    return {
      names:stages.map(s=>s.stage),
      blockStages:(ribbon?.block.stages||[]).map(s=>s.name),
      labels:c.$rolloutLaneDrawStats?.stageLabels||[],
    };
  });
  // The local encode phase is sub-second, so the chart may already have scrolled past its
  // window: the block must still carry it (tooltip), and the legs must keep their segments.
  check(`${viewport.name} cloud transfer phases render as separate segments`,
    ["cloud_upload","cloud_compute","cloud_download"].every(name=>cloudLane.names.includes(name))
      && cloudLane.blockStages.includes("cloud_encode"),
    JSON.stringify(cloudLane));
  check(`${viewport.name} cloud phases carry duration labels`,cloudLane.labels.length>0,JSON.stringify(cloudLane.labels));
  check(`${viewport.name} upload leg labelled in its own window`,
    cloudLane.labels.some(text=>text.startsWith("up ")),
    JSON.stringify(cloudLane.labels));
  const cloudHover=await page.evaluate(()=>{
    const c=window.Chart.getChart(document.getElementById("chart-action"));
    const segment=(c.$ribbonSegments||[]).find(s=>s.ribbon.id===502&&s.stage==="cloud_upload");
    return segment?{x:(segment.x1+segment.x2)/2,y:(segment.y1+segment.y2)/2}:null;
  });
  if (cloudHover) {
    await page.locator("#chart-action").hover({position:cloudHover,force:true});
    await page.waitForTimeout(120);
  }
  const cloudTooltip=await page.evaluate(()=>{
    const c=window.Chart.getChart(document.getElementById("chart-action"));
    const pointer=c?.$pointerPosition||null;
    const laneHovered=pointer?Boolean(window.pointerYInRolloutLanes(c,pointer.y)):false;
    const target=pointer?c.scales.x.getValueForPixel(pointer.x):null;
    const model=Number.isFinite(Number(target))?window.chartTooltipModel(c,target,laneHovered):null;
    return model?.lane?.text||"";
  });
  check(`${viewport.name} cloud hover splits transfer from compute`,
    /cloud_upload/.test(cloudTooltip)&&/Round trip/.test(cloudTooltip)&&/Cloud legs/.test(cloudTooltip),
    cloudTooltip);
  await page.mouse.move(1,1);
  await page.waitForTimeout(60);
  const stableBefore=await page.evaluate(()=>{
    const c=window.Chart.getChart(document.getElementById("chart-action"));
    return {start:c.$rolloutLanes.ribbons[0].block.start,pred:c.$predictions[0]?.data[0]?.x};
  });
  await page.evaluate(()=>{
    const c=window.Chart.getChart(document.getElementById("chart-action"));
    // Re-delivery with a later relative snapshot time must not re-anchor events.
    const timeline=structuredClone(c.$displayTimeline); timeline.t_s+=.37;
    const lanes=window.RolloutLanes.buildRolloutLanes({timeline,chartNow:c.$nowTime,windowS:2,lookaheadS:2});
    window.__alignmentError=Math.abs(c.scales.x.getPixelForValue(lanes.ribbons[0].block.start)-c.scales.x.getPixelForValue(timeline.epoch_ts+timeline.blocks[0].start));
    window.__stableEvent=lanes.ribbons[0].block.start;
  });
  check(`${viewport.name} delayed snapshot aligns within one pixel`,await page.evaluate(()=>window.__alignmentError<=1&&window.__stableEvent===window.Chart.getChart(document.getElementById("chart-action")).$rolloutLanes.ribbons[0].block.start));
  check(`${viewport.name} prediction and action epoch alignment within one pixel`,await page.evaluate(()=>{
    const c=window.Chart.getChart(document.getElementById("chart-action"));
    const active=c.$rolloutLanes.ribbons.find(r=>r.id===502).block.active;
    const x=c.$predictions[0].data[0].x;
    return Math.abs(c.scales.x.getPixelForValue(x)-c.scales.x.getPixelForValue(active+1/30))<=1;
  }));
  await page.evaluate(frame=>window.__rolloutEmitAbsolute({...frame,rollout_timeline:null,prediction:null}),detail);
  await page.waitForTimeout(80);
  check(`${viewport.name} missing timeline retains run epoch and predictions`,await page.evaluate(expected=>{
    const c=window.Chart.getChart(document.getElementById("chart-action"));
    return c.$rolloutLanes.ribbons[0].block.start===expected.start&&c.$predictions[0].data[0].x===expected.pred;
  },stableBefore));
  await page.evaluate(frame=>window.__rolloutEmitAbsolute({...frame,rollout_timeline:{...frame.rollout_timeline,epoch_ts:frame.rollout_timeline.epoch_ts+17}}),detail);
  await page.waitForTimeout(80);
  check(`${viewport.name} same run cannot reanchor after reconnect`,await page.evaluate(expected=>window.Chart.getChart(document.getElementById("chart-action")).$rolloutLanes.ribbons[0].block.start===expected.start,stableBefore));
  await page.locator("#chart-freeze").click();
  await page.locator("#chart-action").evaluate(el=>el.scrollIntoView({block:"center"}));
  fs.mkdirSync(SHOTS,{recursive:true});
  await page.mouse.move(1,1);
  await page.screenshot({path:path.join(SHOTS,`rollout-ribbon-phases-${viewport.name}.png`)});
  for (const phase of ["inference","wait","action","replaced"]) {
    const location=await page.evaluate(phase=>{
      const c=window.Chart.getChart(document.getElementById("chart-action"));
      const segment=c.$ribbonSegments.find(s=>s.ribbon.id===501&&s.phase===phase);
      return segment?{x:(segment.x1+segment.x2)/2,y:(segment.y1+segment.y2)/2}:null;
    },phase);
    if(location) await page.locator("#chart-action").hover({position:location,force:true});
    await page.waitForTimeout(50);
    const content=await page.evaluate(()=>{
      const c=window.Chart.getChart(document.getElementById("chart-action"));
      return c.$hoverTooltipElement.hidden?"":c.$hoverTooltipElement.textContent;
    });
    check(`${viewport.name} ${phase} hover returns entire ribbon`,!!location&&content.includes("chunk #501")&&content.includes("trimmed 5")&&content.includes("GPU 301.00 ms"),content);
  }
  const tooltipBounds=await page.evaluate(()=>{
    const e=window.Chart.getChart(document.getElementById("chart-action")).$hoverTooltipElement;
    const r=e.getBoundingClientRect();return {visible:!e.hidden,left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:innerWidth,height:innerHeight,clipped:e.scrollHeight>e.clientHeight};
  });
  check(`${viewport.name} complete analysis card stays in viewport`,tooltipBounds.visible&&tooltipBounds.left>=0&&tooltipBounds.right<=tooltipBounds.width&&tooltipBounds.top>=0&&tooltipBounds.bottom<=tooltipBounds.height&&!tooltipBounds.clipped,JSON.stringify(tooltipBounds));
  fs.mkdirSync(SHOTS,{recursive:true});
  await page.screenshot({path:path.join(SHOTS,`rollout-ribbon-details-${viewport.name}.png`)});
  await page.locator("#chart-freeze").click();
  const longDetails=structuredClone(detail);
  longDetails.rollout_timeline.blocks[0].stages=Array.from({length:64},(_,i)=>({name:`stage_${i}`,start:detail.rollout_timeline.blocks[0].start+i*.005,end:detail.rollout_timeline.blocks[0].start+(i+1)*.005}));
  await page.evaluate(frame=>window.__rolloutEmitAbsolute(frame),longDetails);
  await page.waitForTimeout(100);
  await page.locator("#chart-freeze").click();
  await page.locator("#chart-action").evaluate(el=>el.scrollIntoView({block:"center"}));
  const longHover=await page.evaluate(()=>{
    const c=window.Chart.getChart(document.getElementById("chart-action"));
    const segment=c.$ribbonSegments.find(s=>s.ribbon.id===501&&s.phase==='replaced');
    return {x:(segment.x1+segment.x2)/2,y:(segment.y1+segment.y2)/2};
  });
  await page.locator("#chart-action").hover({position:longHover,force:true});
  await page.waitForTimeout(60);
  await page.mouse.wheel(0,350);
  await page.waitForTimeout(60);
  check(`${viewport.name} long whole-ribbon analysis scrolls with wheel`,await page.evaluate(()=>window.Chart.getChart(document.getElementById("chart-action")).$hoverTooltipElement.scrollTop>0));
  await page.evaluate(frame=>window.__rolloutEmit({...frame,mode:"idle",display_mode:"idle",rollout_timeline:null}),frames[frames.length-1]);
  await page.waitForTimeout(100);
  check(`${viewport.name} stopped rollout retains frozen ribbon and legend`,await page.evaluate(()=>window.Chart.getChart(document.getElementById("chart-action")).$rolloutLanes.ribbons[0].id===501&&document.querySelector('#action-legend').textContent.includes('Inference stages')));
  await page.locator("#chart-freeze").click();
  await page.waitForTimeout(150);
  const idleBand = await page.evaluate(() => {
    const chart = window.Chart.getChart(document.getElementById("chart-action"));
    return {
      laneHeight: Number(chart.$laneHeight) || 0,
      paddingBottom: chart.options.layout.padding.bottom,
      lanes: chart.$rolloutLanes ? 1 : 0,
      storedFraction: localStorage.getItem("lerobot-monitor-rollout-now-fraction"),
    };
  });
  check(`${viewport.name} idle chart reclaims the lane band`,
    idleBand.laneHeight === 0 && idleBand.paddingBottom === 18 && idleBand.lanes === 0,
    JSON.stringify(idleBand));
  await page.evaluate(()=>window.enterReplay());
  check(`${viewport.name} replay clears freeze`,await page.locator("#chart-freeze").getAttribute("aria-pressed")==="false");
  await page.evaluate(()=>window.leaveReplay());
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
