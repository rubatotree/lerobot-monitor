// Playwright smoke test for the shared arm output rate panel.
//
// Regression target: the editor used to live in an absolutely positioned
// <details> popover inside the top bar and was completely cropped on wide
// screens (.topbar is overflow:hidden above 1400px). It is now a body-level
// fixed panel anchored to whichever trigger opened it, so this script checks
// reachability, viewport containment and un-occluded hit testing at several
// widths, plus the apply/close paths.
//
// PLAYWRIGHT_MODULE may point at an installed Playwright package.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const BASE = process.env.RATE_PANEL_URL || "http://127.0.0.1:8094/lerobot/";
const SHOTS = process.env.RATE_PANEL_SHOTS || "";

const VIEWPORTS = [
  { name: "2560x1440", width: 2560, height: 1440 },
  { name: "1920x1080", width: 1920, height: 1080 },
  { name: "1440x900", width: 1440, height: 900 },
  { name: "1024x900", width: 1024, height: 900 },
  { name: "390x844", width: 390, height: 844 },
  { name: "768x500", width: 768, height: 500 },
].filter((viewport) => !process.env.RATE_PANEL_VIEWPORTS
  || process.env.RATE_PANEL_VIEWPORTS.split(",").includes(viewport.name));

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok: !!ok, detail });
  if (!ok) console.log(`FAIL  ${name}${detail ? ` — ${detail}` : ""}`);
}

function step(tag, message) {
  console.log(`      [${tag}] ${message}`);
}

async function panelState(page) {
  return page.evaluate(() => {
    const panel = document.getElementById("rate-panel");
    if (!panel) return { exists: false };
    const rect = panel.getBoundingClientRect();
    const hidden = panel.classList.contains("hidden");
    const inside = (x, y) => {
      const node = document.elementFromPoint(x, y);
      return !!node && (node === panel || panel.contains(node));
    };
    const inset = 3;
    const points = hidden ? [] : [
      [rect.left + rect.width / 2, rect.top + rect.height / 2],
      [rect.left + inset, rect.top + inset],
      [rect.right - inset, rect.top + inset],
      [rect.left + inset, rect.bottom - inset],
      [rect.right - inset, rect.bottom - inset],
    ];
    return {
      exists: true,
      hidden,
      rect: { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom, width: rect.width, height: rect.height },
      hit: points.map(([x, y]) => inside(x, y)),
      viewport: { width: window.innerWidth, height: window.innerHeight },
      scrollWidth: document.documentElement.scrollWidth,
    };
  });
}

function assertContained(tag, state, label) {
  const { rect, viewport } = state;
  check(`${tag} ${label}: panel not hidden`, !state.hidden);
  check(
    `${tag} ${label}: panel inside viewport`,
    rect.top >= -0.5 && rect.left >= -0.5 && rect.bottom <= viewport.height + 0.5 && rect.right <= viewport.width + 0.5,
    JSON.stringify(rect),
  );
  check(`${tag} ${label}: panel hit not occluded`, state.hit.every(Boolean), JSON.stringify(state.hit));
  check(`${tag} ${label}: no horizontal overflow`, state.scrollWidth <= viewport.width + 1, `${state.scrollWidth} > ${viewport.width}`);
}

async function headerOverlap(page) {
  return page.evaluate(() => {
    const fps = document.getElementById("fps");
    const clock = document.getElementById("clock");
    if (!fps || !clock) return { overlap: false, missing: true };
    const a = fps.getBoundingClientRect();
    const b = clock.getBoundingClientRect();
    const overlap = a.right > b.left && b.right > a.left && a.bottom > b.top && b.bottom > a.top;
    return { overlap, gap: b.left - a.right };
  });
}

async function openVia(page, selector) {
  await page.locator(selector).click();
  await page.waitForTimeout(120);
}

// Phases that only exercise closing behaviour must not start from a panel that
// a previous step left open (it would cover the chip they need to click).
async function ensureClosed(page, tag, where) {
  const isOpen = await page.evaluate(() => !document.getElementById("rate-panel").classList.contains("hidden"));
  if (!isOpen) return true;
  step(tag, `panel was still open before ${where}; forcing closed`);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(100);
  const afterEscape = await page.evaluate(() => document.getElementById("rate-panel").classList.contains("hidden"));
  if (afterEscape) {
    check(`${tag} escape closed a left-over panel (${where})`, true);
    return false;
  }
  await page.locator("#rate-close").click({ force: true });
  await page.waitForTimeout(100);
  return false;
}

const PANE_OF_CHIP = { joints: "joints", teleop: "record", record: "record", rollout: "rollout" };

async function selectSideTab(page, tab) {
  const active = await page.locator(`#side-tab-${tab}`).getAttribute("aria-selected");
  if (active === "true") return;
  await page.locator(`#side-tab-${tab}`).click();
  await page.waitForTimeout(120);
}

async function outsidePoint(page) {
  return page.evaluate(() => {
    const panel = document.getElementById("rate-panel");
    const rect = panel.getBoundingClientRect();
    const candidates = [
      [4, 4],
      [window.innerWidth - 4, 4],
      [4, window.innerHeight - 4],
      [window.innerWidth - 4, window.innerHeight - 4],
      [window.innerWidth / 2, window.innerHeight - 4],
    ];
    for (const [x, y] of candidates) {
      const inside = x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
      if (inside) continue;
      const node = document.elementFromPoint(x, y);
      if (!node) continue;
      if (node.closest("#rate-panel, [data-rate-mode], #rate-open")) continue;
      return { x, y };
    }
    return null;
  });
}

async function closePaths(page, tag) {
  step(tag, "close paths");
  const wasClosed = await ensureClosed(page, tag, "close paths");
  if (!wasClosed) check(`${tag} close paths started from a closed panel`, false, "panel left open by previous phase");
  await selectSideTab(page, "joints");
  // Escape closes and returns focus to the trigger.
  await openVia(page, "#joints-rate");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(80);
  let state = await panelState(page);
  check(`${tag} escape closes panel`, state.hidden);
  const focused = await page.evaluate(() => document.activeElement?.id || "");
  check(`${tag} escape restores trigger focus`, focused === "joints-rate", focused);

  // Clicking outside closes it.
  await openVia(page, "#joints-rate");
  const point = await outsidePoint(page);
  if (point) {
    await page.mouse.click(point.x, point.y);
    await page.waitForTimeout(80);
    state = await panelState(page);
    check(`${tag} outside click closes panel`, state.hidden);
  } else {
    check(`${tag} outside click closes panel`, false, "no outside point available");
  }

  // The close button and the header trigger both toggle.
  await openVia(page, "#joints-rate");
  await page.locator("#rate-close").click();
  await page.waitForTimeout(80);
  state = await panelState(page);
  check(`${tag} close button closes panel`, state.hidden);

  await openVia(page, "#rate-open");
  await page.locator("#rate-open").click();
  await page.waitForTimeout(80);
  state = await panelState(page);
  check(`${tag} header trigger toggles panel`, state.hidden);
}

async function applyPaths(page, tag, putRequests) {
  step(tag, "apply paths");
  await ensureClosed(page, tag, "apply paths");
  await selectSideTab(page, "joints");
  await openVia(page, "#rate-open");
  await page.selectOption("#rate-kind", "hz");
  await page.fill("#rate-hz", "42");
  const before = putRequests.count;
  await page.locator("#rate-apply").click();
  await page.waitForFunction(
    () => document.getElementById("rate-error")?.textContent === "Applied",
    null,
    { timeout: 10000 },
  );
  check(`${tag} apply sends one PUT`, putRequests.count === before + 1, `${before} -> ${putRequests.count}`);
  const fps = await page.locator("#fps").textContent();
  check(`${tag} header shows applied rate`, fps.includes("42.0"), fps);
  const chip = await page.locator("#joints-rate .rate-chip-value").textContent();
  check(`${tag} joints chip shows applied rate`, chip.includes("42.0"), chip);

  // Invalid input is reported inline and never reaches the API.
  await page.fill("#rate-default", "0");
  await page.fill("#rate-hz", "999");
  const after = putRequests.count;
  await page.locator("#rate-apply").click();
  await page.waitForTimeout(200);
  const error = await page.locator("#rate-error").textContent();
  check(`${tag} invalid input reported inline`, error.includes("between 1 and 240"), error);
  check(`${tag} invalid input sends no request`, putRequests.count === after, `${after} -> ${putRequests.count}`);

  // Restore the default so repeated runs stay idempotent.
  await page.fill("#rate-default", "30");
  await page.selectOption("#rate-kind", "inherit");
  await page.locator("#rate-apply").click();
  await page.waitForFunction(
    () => document.getElementById("rate-error")?.textContent === "Applied",
    null,
    { timeout: 10000 },
  );
  const restored = await page.locator("#fps").textContent();
  check(`${tag} inherit restores global default`, restored.includes("30.0"), restored);
  await page.locator("#rate-close").click();
}

async function sideTabClose(page, tag) {
  step(tag, "side tab close");
  await ensureClosed(page, tag, "side tab close");
  await selectSideTab(page, "joints");
  await openVia(page, "#joints-rate");
  await page.locator("#side-tab-record").click();
  await page.waitForTimeout(120);
  const state = await panelState(page);
  check(`${tag} tab switch closes panel`, state.hidden);
  await page.locator("#side-tab-joints").click();
  await page.waitForTimeout(80);
}

async function scrollFollow(page, tag) {
  step(tag, "scroll follow");
  await ensureClosed(page, tag, "scroll follow");
  await selectSideTab(page, "joints");
  await openVia(page, "#joints-rate");
  await page.locator("#joint-panel").evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await page.waitForTimeout(150);
  const state = await panelState(page);
  if (state.hidden) {
    check(`${tag} panel closes or follows when its pane scrolls`, true, "closed");
    return;
  }
  assertContained(tag, state, "after pane scroll");
  await page.locator("#rate-close").click();
}

async function multiplierPresets(page, tag) {
  step(tag, "multiplier presets");
  await ensureClosed(page, tag, "multiplier presets");
  await selectSideTab(page, "rollout");
  await openVia(page, "#rollout-rate");

  const offered = await page.locator("#rate-kind option").evaluateAll((nodes) => nodes.map((node) => node.value));
  for (const preset of ["multiplier:1", "multiplier:2", "multiplier:4", "multiplier:6", "multiplier:8"]) {
    check(`${tag} ${preset} offered`, offered.includes(preset), offered.join(","));
  }

  const enabledFor = async () => page.locator("#rate-kind option").evaluateAll(
    (nodes) => nodes.filter((node) => !node.disabled).map((node) => node.value).filter((value) => value.startsWith("multiplier")),
  );
  await page.selectOption("#rate-mode", "rollout");
  const rolloutOptions = await enabledFor();
  check(`${tag} rollout enables every multiplier`, rolloutOptions.length === 5, rolloutOptions.join(","));
  await page.selectOption("#rate-mode", "joints");
  const jointsOptions = await enabledFor();
  check(`${tag} joints disables every multiplier`, jointsOptions.length === 0, jointsOptions.join(","));

  // 8× is accepted for rollout and shows up on the chip while rollout is idle.
  await page.selectOption("#rate-mode", "rollout");
  await page.selectOption("#rate-kind", "multiplier:8");
  await page.locator("#rate-apply").click();
  await page.waitForFunction(
    () => document.getElementById("rate-error")?.textContent === "Applied",
    null,
    { timeout: 10000 },
  );
  const chip = await page.locator("#rollout-rate .rate-chip-value").textContent();
  check(`${tag} 8× chip shows the multiplier`, chip.includes("8×"), chip);

  // Restore the shared default so repeated runs stay idempotent.
  await page.selectOption("#rate-kind", "inherit");
  await page.locator("#rate-apply").click();
  await page.waitForFunction(
    () => document.getElementById("rate-error")?.textContent === "Applied",
    null,
    { timeout: 10000 },
  );
  const restored = await page.locator("#rollout-rate .rate-chip-value").textContent();
  check(`${tag} rollout chip restored to the global default`, restored.includes("30"), restored);
  await page.locator("#rate-close").click();
}

async function runViewport(browser, viewport) {
  const tag = viewport.name;
  console.log(`[${tag}] start`);
  const page = await browser.newPage({ viewport: { width: viewport.width, height: viewport.height } });
  page.setDefaultTimeout(15000);
  const errors = [];
  const putRequests = { count: 0 };
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("request", (request) => {
    if (request.method() === "PUT" && request.url().includes("/api/control/rates")) putRequests.count += 1;
  });

  await page.goto(`${BASE}?v=${Date.now()}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#rate-open", { timeout: 30000 });
  // #rate-live is only written once a status snapshot arrives.
  await page.waitForFunction(
    () => !(document.getElementById("rate-live")?.textContent || "").startsWith("—"),
    null,
    { timeout: 30000 },
  );
  await page.waitForTimeout(300);
  console.log(`[${tag}] status ready`);

  const overlap = await headerOverlap(page);
  check(`${tag} header label does not overlap the clock`, !overlap.overlap, JSON.stringify(overlap));

  // The wide-screen regression: opening from the header must be visible.
  await openVia(page, "#rate-open");
  assertContained(tag, await panelState(page), "header trigger");
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/rate-panel-${tag}-header.png` });

  // The header-anchored panel covers part of the side pane; close it before
  // driving the chips so their clicks are not intercepted.
  await ensureClosed(page, tag, "chips");
  step(tag, "chips");
  for (const mode of ["joints", "teleop", "record", "rollout"]) {
    const selector = `#${mode}-rate`;
    step(tag, `chip ${mode}`);
    await selectSideTab(page, PANE_OF_CHIP[mode]);
    await page.locator(selector).scrollIntoViewIfNeeded();
    await openVia(page, selector);
    const state = await panelState(page);
    assertContained(tag, state, `${mode} chip`);
    const expanded = await page.locator(selector).getAttribute("aria-expanded");
    check(`${tag} ${mode} chip marked expanded`, expanded === "true", String(expanded));
    await page.locator("#rate-close").click();
    await page.waitForTimeout(60);
    const closed = await page.evaluate(() => document.getElementById("rate-panel").classList.contains("hidden"));
    if (!closed) {
      step(tag, `panel still open after closing the ${mode} chip`);
      check(`${tag} ${mode} chip close button closed the panel`, false);
      await ensureClosed(page, tag, `${mode} chip`);
    } else {
      check(`${tag} ${mode} chip close button closed the panel`, true);
    }
  }

  for (const phase of ["applyPaths", "closePaths", "sideTabClose", "multiplierPresets"]) {
    try {
      if (phase === "applyPaths") await applyPaths(page, tag, putRequests);
      if (phase === "closePaths") await closePaths(page, tag);
      if (phase === "sideTabClose") await sideTabClose(page, tag);
      if (phase === "multiplierPresets") await multiplierPresets(page, tag);
    } catch (error) {
      check(`${tag} ${phase}`, false, error.message.split("\n")[0]);
      console.log(`      [${tag}] ${phase} aborted`);
    }
  }
  if (viewport.width <= 1180) {
    try {
      await scrollFollow(page, tag);
    } catch (error) {
      check(`${tag} scrollFollow`, false, error.message.split("\n")[0]);
    }
  }

  check(`${tag} no page errors`, errors.length === 0, errors.join(" | "));
  await page.close();
  console.log(`[${tag}] done`);
}

async function main() {
  const browser = await chromium.launch({ channel: "chrome", headless: process.env.RATE_PANEL_HEADED !== "1" });
  for (const viewport of VIEWPORTS) {
    try {
      await runViewport(browser, viewport);
    } catch (error) {
      check(`${viewport.name} completed`, false, error.message);
      console.log(`[${viewport.name}] aborted: ${error.message}`);
    }
  }
  await browser.close();

  const failed = results.filter((result) => !result.ok);
  console.log(`${results.length - failed.length}/${results.length} checks passed`);
  process.exitCode = failed.length ? 1 : 0;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
