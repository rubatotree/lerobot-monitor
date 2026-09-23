// One owner for all preview frames. Visibility and dirty flags are independent
// because the wrist camera lives outside the arm preview panel.
export function createPreviewScheduler({
  requestFrame = requestAnimationFrame,
  cancelFrame = cancelAnimationFrame,
  updatePose,
  renderMain,
  renderWrist,
}) {
  let handle = null;
  let running = false;
  let enabled = false;
  let mainVisible = false;
  let wristVisible = false;
  let mainDirty = true;
  let wristDirty = true;
  let poseActive = false;
  let mainAt = -Infinity;
  let wristAt = -Infinity;
  let poseAt = -Infinity;
  const counts = { mainFrames: 0, wristFrames: 0, poseSteps: 0 };
  const due = (now, last, fps) => now - last >= 1000 / fps - 0.5;

  function hasWork() {
    return enabled && ((mainVisible && mainDirty)
      || (wristVisible && wristDirty)
      || ((mainVisible || wristVisible) && poseActive));
  }

  function schedule() {
    if (handle === null && !running && hasWork()) handle = requestFrame(tick);
  }

  function tick(now) {
    handle = null;
    if (!hasWork()) return;
    running = true;
    try {
      if (poseActive && due(now, poseAt, 30)) {
        // Cap the first step after an idle/hidden interval to avoid a jump.
        const dt = Number.isFinite(poseAt) ? Math.min(now - poseAt, 100) : 1000 / 30;
        poseAt = now;
        poseActive = updatePose(dt);
        counts.poseSteps += 1;
        mainDirty = wristDirty = true;
      }
      if (mainVisible && mainDirty && due(now, mainAt, 60)) {
        mainDirty = false;
        mainAt = now;
        renderMain();
        counts.mainFrames += 1;
      }
      if (wristVisible && wristDirty && due(now, wristAt, 10)) {
        wristDirty = false;
        wristAt = now;
        renderWrist();
        counts.wristFrames += 1;
      }
    } finally {
      running = false;
      schedule();
    }
  }

  return {
    invalidate({ main = false, wrist = false, pose = false } = {}) {
      mainDirty ||= main;
      wristDirty ||= wrist;
      poseActive ||= pose;
      schedule();
    },
    setVisibility({ enabled: nextEnabled, main, wrist }) {
      if (main && (!mainVisible || !enabled)) mainDirty = true;
      if (wrist && (!wristVisible || !enabled)) wristDirty = true;
      enabled = nextEnabled;
      mainVisible = main;
      wristVisible = wrist;
      if (!hasWork() && handle !== null) {
        cancelFrame(handle);
        handle = null;
      }
      schedule();
    },
    dispose() {
      enabled = false;
      poseActive = false;
      if (handle !== null) cancelFrame(handle);
      handle = null;
    },
    debugInfo() {
      return { ...counts, pendingFrames: Number(handle !== null), poseActive };
    },
  };
}

export function advancePose(display, target, elapsedMs, applyJoint) {
  // A 55 ms response stays visibly smooth at 30 Hz while settling in roughly
  // half a second. Time-based interpolation remains independent of frame rate.
  const alpha = 1 - Math.exp(-elapsedMs / 55);
  let active = false;
  for (const [name, value] of Object.entries(target)) {
    const previous = display[name];
    const next = Number.isFinite(previous) ? previous + (value - previous) * alpha : value;
    display[name] = Math.abs(value - next) <= 0.01 ? value : next;
    active ||= display[name] !== value;
    if (display[name] !== previous) applyJoint(name, display[name]);
  }
  return active;
}
