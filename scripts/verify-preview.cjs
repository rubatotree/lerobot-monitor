// Playwright smoke test for the embedded 3D arm preview.
const { chromium } = require("C:/Users/Admin/AppData/Local/npm-cache/_npx/e41f203b7505f1fb/node_modules/playwright");

const BASE = process.env.PREVIEW_URL || "http://127.0.0.1:8099/lerobot/";

async function canvasStats(page, selector) {
  return page.locator(selector).evaluate((canvas) => {
    const context = canvas.getContext("webgl2") || canvas.getContext("webgl");
    const width = canvas.width;
    const height = canvas.height;
    const pixels = new Uint8Array(width * height * 4);
    context.readPixels(0, 0, width, height, context.RGBA, context.UNSIGNED_BYTE, pixels);
    const colors = new Set();
    for (let index = 0; index < pixels.length; index += 64) {
      colors.add(`${pixels[index]},${pixels[index + 1]},${pixels[index + 2]},${pixels[index + 3]}`);
    }
    return { width, height, colors: colors.size };
  });
}

async function main() {
  const browser = await chromium.launch({ channel: "chrome" });
  const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    const text = message.text();
    if (message.type() === "error" && !text.includes("ERR_NETWORK_ACCESS_DENIED")) {
      errors.push(text);
    }
  });
  await page.goto(`${BASE}?v=${Date.now()}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#arm-preview canvas", { timeout: 30000 });
  await page.waitForTimeout(2500);

  const models = await page.locator("#preview-model option").allTextContents();
  const source = await page.locator("#preview-source").evaluate((element) => element.value);
  const chartLoaded = await page.evaluate(() => typeof window.Chart === "function");
  const stats = await canvasStats(page, "#arm-preview canvas");
  const debug = await page.evaluate(() => window.RobotPreview.debugInfo());
  const startWidth = await page.locator("#arm-preview").evaluate((element) => element.getBoundingClientRect().width);

  const splitter = page.locator("#split-preview");
  const box = await splitter.boundingBox();
  if (box) {
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + 90, box.y + box.height / 2);
    await page.mouse.up();
  }
  const resizedWidth = await page.locator("#arm-preview").evaluate((element) => element.getBoundingClientRect().width);

  await page.locator("#preview-wrist-toggle").click();
  await page.waitForTimeout(500);
  const wristVisible = await page.locator("#preview-wrist").evaluate((element) => element.classList.contains("on"));
  const wristStats = wristVisible ? await canvasStats(page, "#preview-wrist-canvas") : { width: 0, height: 0, colors: 0 };
  const wristDebug = await page.evaluate(() => window.RobotPreview.debugInfo());

  await page.locator("#joint-rows input[type='range']").first().evaluate((input) => {
    input.value = String(Number(input.value) + 2);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.waitForTimeout(100);
  const sliderBadge = await page.locator("#preview-auto-badge").textContent();
  await page.waitForTimeout(900);

  await page.evaluate(() => {
    window.RobotPreview.setFocusContext("action_chart", {
      pose: {
        shoulder_pan: 12,
        shoulder_lift: -40,
        elbow_flex: 60,
        wrist_flex: 10,
        wrist_roll: -20,
        gripper: 35,
      },
      source: "prediction",
    });
  });
  await page.waitForTimeout(100);
  const badge = await page.locator("#preview-auto-badge").textContent();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  await page.screenshot({ path: ".verify-preview.png", fullPage: false });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector("#arm-preview", { timeout: 30000 });
  await page.waitForTimeout(500);
  const persistedWidth = await page.locator("#arm-preview").evaluate((element) => element.getBoundingClientRect().width);

  await page.locator("#preview-power").click();
  await page.waitForTimeout(300);
  const poweredOff = await page.evaluate(async () => ({
    preview: window.RobotPreview.debugInfo(),
    canvasRemoved: !document.querySelector("#preview-stage canvas"),
    robot: (await fetch("/lerobot/api/status").then((response) => response.json())).robot,
  }));
  await page.locator("#preview-power").click();
  await page.waitForSelector("#arm-preview canvas", { timeout: 30000 });
  await page.waitForTimeout(1200);
  const poweredOn = await page.evaluate(async () => ({
    preview: window.RobotPreview.debugInfo(),
    robot: (await fetch("/lerobot/api/status").then((response) => response.json())).robot,
  }));

  const narrow = await browser.newPage({ viewport: { width: 900, height: 900 } });
  await narrow.goto(`${BASE}?v=${Date.now()}-narrow`, { waitUntil: "domcontentloaded" });
  await narrow.waitForSelector("#arm-preview", { timeout: 30000 });
  await narrow.waitForTimeout(1500);
  const narrowState = await narrow.evaluate(() => ({
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    splitHidden: getComputedStyle(document.getElementById("split-preview")).display === "none",
    previewWidth: document.getElementById("arm-preview").getBoundingClientRect().width,
    viewport: document.documentElement.clientWidth,
  }));
  await narrow.screenshot({ path: ".verify-preview-narrow.png", fullPage: false });
  await narrow.close();

  const result = {
    models,
    source,
    chartLoaded,
    startWidth,
    resizedWidth,
    stats,
    debug,
    wristVisible,
    wristStats,
    wristDebug,
    sliderBadge,
    badge,
    overflow,
    persistedWidth,
    poweredOff,
    poweredOn,
    narrowState,
    errors,
  };
  console.log(JSON.stringify(result, null, 2));
  const ok = models.includes("SO-101 / SO-100")
    && source === "auto"
    && chartLoaded
    && debug.joints > 0
    && debug.renderCalls > 0
    && debug.canvasWidth > 0
    && debug.canvasHeight > 0
    && resizedWidth > startWidth + 20
    && Math.abs(persistedWidth - resizedWidth) < 2
    && poweredOff.preview.powered === false
    && poweredOff.canvasRemoved
    && poweredOff.robot.connected === false
    && poweredOn.preview.powered === true
    && poweredOn.robot.virtual === true
    && wristVisible
    && wristDebug.wristRenderCalls > 0
    && /joints/i.test(sliderBadge || "")
    && /prediction/i.test(badge || "")
    && !overflow
    && !narrowState.overflow
    && narrowState.splitHidden
    && narrowState.previewWidth >= narrowState.viewport - 1
    && errors.length === 0;
  await browser.close();
  process.exit(ok ? 0 : 1);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
