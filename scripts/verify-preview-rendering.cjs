// Isolated browser regression: real HTML/CSS/Three.js/URDF, mocked hardware API.
// PLAYWRIGHT_MODULE may point at an installed Playwright package.
// PREVIEW_BASELINE_REF compares a previous Git revision without checking it out.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync, execFile } = require("node:child_process");
const { promisify } = require("node:util");
const execFileAsync = promisify(execFile);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "..");
const output = path.resolve(process.env.PREVIEW_ARTIFACTS || path.join(root, "data/preview-verification"));
const web = "src/lerobot_monitor/web/static/";
const models = "src/lerobot_monitor/robot_models/so101/";
const fixture = JSON.parse(fs.readFileSync(path.join(root, models, "robot_model.json")));
const pose = fixture.default_pose;
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const debug = (page) => page.evaluate(() => window.RobotPreview.debugInfo());

async function openFixture(browser, { revision, dpr = 1, width = 1680, meshDelay = 0, waitForModel = true } = {}) {
  const page = await browser.newPage({ viewport: { width, height: 1000 }, deviceScaleFactor: dpr });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const read = (file) => revision
    ? execFileSync("git", ["show", `${revision}:${file}`], { cwd: root, maxBuffer: 32 * 1024 * 1024 })
    : fs.readFileSync(path.join(root, file));
  await page.addInitScript(() => {
    window.previewMeasurements = { main: [], wrist: [] };
    window.previewProbe = (view) => {
      const entries = window.previewMeasurements[view];
      if (entries.length < 100000) entries.push(performance.now());
    };
  });
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    const json = (value) => route.fulfill({ json: value });
    if (url.hostname !== "preview.test") return route.fulfill({ body: "" });
    if (pathname === "/lerobot/api/robot-models") {
      return json([{ ...fixture, builtin: true, active: true }, { ...fixture, id: "external", builtin: false }]);
    }
    if (pathname === "/lerobot/api/robot-models/active") {
      return json({ ...fixture, id: route.request().postDataJSON().id });
    }
    if (pathname === "/lerobot/api/status") return json({ joints: pose, robot: { connected: false } });
    if (pathname.startsWith("/lerobot/api/virtual-follower/")) return json({});
    const asset = pathname.match(/^\/lerobot\/api\/robot-models\/(?:so101|external)\/files\/(.+)$/);
    if (asset) {
      if (meshDelay && asset[1].endsWith(".stl")) await delay(meshDelay);
      return route.fulfill({ body: fs.readFileSync(path.join(root, models, asset[1])), contentType: "application/octet-stream" });
    }
    let file;
    if (pathname === "/lerobot/") file = `${web}index.html`;
    else if (pathname.startsWith("/lerobot/static/")) file = web + pathname.slice("/lerobot/static/".length);
    else throw new Error(`Unexpected fixture request: ${pathname}`);
    let body = read(file);
    if (file.endsWith("index.html")) {
      // Keep the full page layout, but never run the hardware controller.
      body = body.toString().replace(/<script src="[^\"]*(?:app\.js|chart\.umd\.min\.js)[^\"]*"><\/script>/g, "");
    }
    if (file.endsWith("robot-preview.js")) {
      // Count actual render calls, including the uninstrumented baseline.
      body = body.toString()
        .replaceAll("state.renderer.render(state.scene, state.camera)", '(window.previewProbe("main"), state.renderer.render(state.scene, state.camera))')
        .replaceAll("state.wristRenderer.render(state.scene, state.wristCamera)", '(window.previewProbe("wrist"), state.wristRenderer.render(state.scene, state.wristCamera))')
        + "\nwindow.previewTestState = state;\n";
    }
    const type = file.endsWith(".html") ? "text/html" : file.endsWith(".css") ? "text/css" : "application/javascript";
    await route.fulfill({ body, contentType: type });
  });
  await page.goto("http://preview.test/lerobot/");
  if (waitForModel) {
    await page.waitForFunction(() => window.RobotPreview?.debugInfo().joints > 0);
    await page.waitForTimeout(500);
  } else await page.waitForSelector("#preview-stage canvas");
  return { page, errors };
}

async function rotate(page, duration = 2000) {
  // Run inputs on animation frames to avoid transport latency dominating results.
  return page.evaluate(async (duration) => {
    const canvas = document.querySelector("#preview-stage canvas");
    const bounds = canvas.getBoundingClientRect();
    const start = performance.now();
    const inputTimes = [];
    const event = (type, x, y, buttons) => canvas.dispatchEvent(new PointerEvent(type, {
      bubbles: true, pointerId: 1, pointerType: "mouse", button: 0, buttons,
      clientX: bounds.x + x, clientY: bounds.y + y,
    }));
    // Synthetic pointer capture is not available; exercise the same listeners.
    const original = canvas.setPointerCapture;
    canvas.setPointerCapture = () => {};
    event("pointerdown", 80, 60, 1);
    await new Promise((resolve) => {
      function step(now) {
        inputTimes.push(now);
        event("pointermove", 80 + Math.sin((now - start) / 400) * 65,
          60 + Math.cos((now - start) / 600) * 25, 1);
        if (now - start < duration) requestAnimationFrame(step);
        else resolve();
      }
      requestAnimationFrame(step);
    });
    event("pointerup", 80, 60, 0);
    canvas.setPointerCapture = original;
    return inputTimes;
  }, duration);
}

function timing(times) {
  const intervals = times.slice(1).map((t, index) => t - times[index]).sort((a, b) => a - b);
  return {
    frames: times.length,
    fps: times.length > 1 ? (times.length - 1) * 1000 / (times.at(-1) - times[0]) : 0,
    p95Ms: intervals[Math.floor(intervals.length * 0.95)] || 0,
    duplicateIntervals: intervals.filter((dt) => dt < 8).length,
  };
}

async function measure(browser, revision) {
  const { page, errors } = await openFixture(browser, { revision });
  try {
    await page.locator("#preview-wrist-toggle").click();
    await page.waitForTimeout(500);
    await page.evaluate(() => { window.previewMeasurements = { main: [], wrist: [] }; });
    let sampling = process.env.PREVIEW_GPU_SAMPLE === "1";
    const gpuSamples = [];
    const sampler = (async () => {
      while (sampling) {
        try {
          const { stdout } = await execFileAsync("nvidia-smi", ["--query-gpu=name,utilization.gpu", "--format=csv,noheader,nounits"], { windowsHide: true, timeout: 5000 });
          gpuSamples.push(stdout.trim());
        } catch { sampling = false; }
        if (sampling) await delay(400);
      }
    })();
    let inputs;
    try { inputs = await rotate(page); }
    finally { sampling = false; await sampler; }
    const measurements = await page.evaluate(() => window.previewMeasurements);
    const hardware = await page.locator("#preview-stage canvas").evaluate((canvas) => {
      const gl = canvas.getContext("webgl2");
      const extension = gl.getExtension("WEBGL_debug_renderer_info");
      return {
        renderer: extension ? gl.getParameter(extension.UNMASKED_RENDERER_WEBGL) : "unavailable",
        antialias: gl.getContextAttributes().antialias,
      };
    });
    await page.screenshot({ path: path.join(output, revision ? "baseline.png" : "optimized.png") });
    assert.deepEqual(errors, []);
    return {
      revision: revision || "working-tree", main: timing(measurements.main),
      wrist: timing(measurements.wrist), inputCadence: timing(inputs), hardware,
      systemWideGpuSamples: gpuSamples,
      gpuCaveat: "System-wide utilization includes other apps; render submissions are not displayed frames.",
    };
  } finally { await page.close(); }
}

async function regression(browser) {
  const { page, errors } = await openFixture(browser);
  try {
    await page.locator("#preview-wrist-toggle").click();
    await page.waitForTimeout(250);
    const wristOrientation = await page.evaluate(() => {
      const model = window.previewTestState.model;
      const mount = window.previewTestState.robot.links[model.wrist_camera.link];
      const frame = window.previewTestState.robot.links[model.wrist_camera.look_at_link];
      const mountPosition = mount.getWorldPosition(new window.previewTestState.camera.position.constructor());
      const framePosition = frame.getWorldPosition(new window.previewTestState.camera.position.constructor());
      const outward = framePosition.clone().sub(mountPosition).normalize();
      const view = window.previewTestState.wristCamera.getWorldDirection(
        new window.previewTestState.camera.position.constructor(),
      );
      const frameUp = new window.previewTestState.camera.position.constructor(0, 1, 0)
        .applyQuaternion(frame.getWorldQuaternion(new window.previewTestState.camera.quaternion.constructor()))
        .normalize();
      return {
        outwardDot: outward.dot(view),
        upDot: frameUp.dot(window.previewTestState.wristCamera.up),
        positionError: framePosition.distanceTo(window.previewTestState.wristCamera.position),
      };
    });
    assert.ok(wristOrientation.outwardDot > 0.999);
    assert.ok(wristOrientation.upDot > 0.999);
    assert.ok(wristOrientation.positionError < 1e-6);
    await page.locator("#preview-wrist").screenshot({ path: path.join(output, "wrist-outward.png") });
    const idle = await debug(page);
    await page.waitForTimeout(5000);
    const idleEnd = await debug(page);
    assert.equal(idleEnd.mainFrames, idle.mainFrames);
    assert.equal(idleEnd.wristFrames, idle.wristFrames);
    assert.equal(idleEnd.pendingFrames, 0);
    await rotate(page, 1000);
    await page.waitForTimeout(100);
    assert.equal((await debug(page)).wristFrames, idle.wristFrames);
    assert.equal((await debug(page)).pendingFrames, 0);
    for (const name of ["iso", "front", "side", "top", "reset"]) {
      await page.locator(`#preview-view-${name}`).click();
      await page.waitForTimeout(50);
      assert.equal((await debug(page)).wristFrames, idle.wristFrames);
    }
    // Real pointer pan and wheel zoom, alongside a changing pose.
    await page.evaluate(() => window.RobotPreview.setFocusContext("joint_chart", { pose: { shoulder_pan: 50 } }));
    const canvas = page.locator("#preview-stage canvas");
    const box = await canvas.boundingBox();
    await page.mouse.move(box.x + 90, box.y + 55);
    await page.mouse.down({ button: "right" });
    await page.mouse.move(box.x + 110, box.y + 75, { steps: 10 });
    await page.mouse.up({ button: "right" });
    await page.mouse.wheel(0, 60);
    await page.waitForFunction(() => window.RobotPreview.debugInfo().pendingFrames === 0);
    assert.equal((await debug(page)).displayPose.shoulder_pan, 50);
    assert.ok((await debug(page)).wristFrames > idle.wristFrames);
    // Main panel and wrist visibility must be independent.
    await page.locator("#preview-stage").evaluate((node) => { node.style.visibility = "hidden"; node.style.transform = "translateX(-5000px)"; });
    await page.waitForTimeout(100);
    const hidden = await debug(page);
    await page.evaluate(() => window.RobotPreview.setFocusContext("joint_chart", { pose: { shoulder_pan: -25 } }));
    await page.waitForTimeout(2000);
    assert.equal((await debug(page)).mainFrames, hidden.mainFrames);
    assert.ok((await debug(page)).wristFrames > hidden.wristFrames);
    await page.locator("#preview-stage").evaluate((node) => { node.style.visibility = ""; node.style.transform = ""; });
    await page.waitForTimeout(100);
    assert.ok((await debug(page)).mainFrames > hidden.mainFrames);
    await page.locator("#preview-model").selectOption("external");
    await page.waitForFunction(() => window.previewTestState.model?.id === "external");
    const imported = await page.evaluate(() => {
      const materials = new Set();
      window.previewTestState.robot.traverse((node) => { if (node.material) materials.add(node.material.type); });
      return [...materials];
    });
    assert.deepEqual(imported, ["MeshPhongMaterial"]);
    await page.locator("#preview-model").selectOption("so101");
    await page.waitForFunction(() => window.previewTestState.model?.id === "so101");
    const bundled = await page.evaluate(() => {
      const materials = new Set();
      window.previewTestState.robot.traverse((node) => { if (node.material) materials.add(node.material); });
      return [...materials].map((m) => ({ type: m.type, color: m.color.getHexString(), side: m.side }));
    });
    assert.equal(bundled.length, 2);
    assert.ok(bundled.every((m) => m.type === "MeshLambertMaterial" && m.side === 0));
    assert.deepEqual(bundled.map((m) => m.color).sort(), ["343b48", "aeb8c8"]);
    await page.locator("#preview-power").click();
    assert.equal((await debug(page)).pendingFrames, 0);
    assert.equal(await page.locator("#preview-stage canvas").count(), 0);
    await page.locator("#preview-power").click();
    await page.waitForFunction(() => window.RobotPreview.debugInfo().joints > 0);
    await page.locator("#preview-wrist-toggle").click();
    await page.waitForFunction(() => window.RobotPreview.debugInfo().wristFrames > 0);
    assert.deepEqual(errors, []);
    return {
      idleFiveSeconds: true, cameraIndependent: true, poseConverged: true,
      visibilityIndependent: true, powerCycle: true, wristOrientation,
    };
  } finally { await page.close(); }
}

async function lateLoadRegression(browser) {
  const { page, errors } = await openFixture(browser, { meshDelay: 700, waitForModel: false });
  try {
    await page.evaluate(() => window.RobotPreview.setPower(false));
    await page.waitForTimeout(1200);
    assert.equal((await debug(page)).joints, 0);
    assert.equal((await debug(page)).pendingFrames, 0);
    assert.equal(await page.locator("#preview-stage canvas").count(), 0);
    await page.evaluate(() => window.RobotPreview.setPower(true));
    await page.waitForFunction(() => window.RobotPreview.debugInfo().joints > 0);
    // Start a slow model switch and immediately supersede it.
    await page.locator("#preview-model").selectOption("external");
    await page.waitForTimeout(80);
    await page.locator("#preview-model").selectOption("so101");
    await page.waitForTimeout(1200);
    const model = await page.evaluate(() => window.previewTestState.model.id);
    assert.equal(model, "so101");
    assert.deepEqual(errors, []);
    return { lateLoadAfterPowerOff: true, supersededModel: true };
  } finally { await page.close(); }
}

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: "chrome", headless: process.env.PREVIEW_HEADED !== "1" });
  const results = { environment: { headless: process.env.PREVIEW_HEADED !== "1", hardwareAPI: "mocked" } };
  try {
    if (process.env.PREVIEW_BASELINE_REF) results.baseline = await measure(browser, process.env.PREVIEW_BASELINE_REF);
    results.optimized = await measure(browser);
    if (process.env.PREVIEW_MEASURE_ONLY === "1") return;
    results.regression = await regression(browser);
    results.lifecycle = await lateLoadRegression(browser);
    results.layouts = [];
    for (const dpr of [1, 2]) {
      for (const width of [1680, 900]) {
        const { page, errors } = await openFixture(browser, { dpr, width });
        try {
          for (const panelWidth of [280, 480]) {
            await page.evaluate((w) => document.documentElement.style.setProperty("--preview-w", `${w}px`), panelWidth);
            await page.locator("#preview-stage").scrollIntoViewIfNeeded();
            await page.waitForTimeout(100);
            const info = await debug(page);
            assert.ok(info.mainFrames > 0);
            assert.equal(info.pixelRatio, Math.min(dpr, 1.5));
            const placement = await page.evaluate(() => {
              const stage = document.querySelector("#preview-stage").getBoundingClientRect();
              const buttons = document.querySelector(".preview-view-buttons").getBoundingClientRect();
              return { bottom: stage.bottom - buttons.bottom, right: stage.right - buttons.right, fits: buttons.left >= stage.left };
            });
            assert.ok(placement.bottom >= 6 && placement.bottom <= 8 && placement.right >= 6 && placement.right <= 8 && placement.fits);
            await page.screenshot({ path: path.join(output, `preview-dpr${dpr}-${width}-${panelWidth}.png`) });
            await page.locator("#arm-preview").screenshot({ path: path.join(output, `arm-dpr${dpr}-${width}-${panelWidth}.png`) });
            results.layouts.push({ dpr, width, panelWidth, info, placement });
          }
          assert.deepEqual(errors, []);
        } finally { await page.close(); }
      }
    }
  } finally {
    fs.writeFileSync(path.join(output, "results.json"), JSON.stringify(results, null, 2));
    await browser.close();
  }
  console.log(JSON.stringify(results, null, 2));
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
