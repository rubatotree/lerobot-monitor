// Shared validation for the rollout editor and its startup payload.
(function exposeRolloutSpeed(root) {
  const MIN_HZ = 0.1;
  const MAX_HZ = 240;
  function number(value) {
    if (value == null || value === '' || typeof value === 'boolean') return NaN;
    return Number(value);
  }
  function inspect(base, speed) {
    const baseHz = number(base);
    const multiplier = number(speed);
    const effectiveHz = baseHz * multiplier;
    let error = '';
    if (!Number.isInteger(baseHz) || baseHz < 1 || baseHz > MAX_HZ) error = 'Base policy FPS must be an integer between 1 and 240.';
    else if (!Number.isFinite(multiplier) || multiplier <= 0) error = 'Execution speed must be a positive, finite multiplier.';
    else if (!Number.isFinite(effectiveHz) || effectiveHz < MIN_HZ || effectiveHz > MAX_HZ) {
      error = `Effective execution rate must be between ${MIN_HZ} and ${MAX_HZ} Hz. Adjust the base FPS or multiplier.`;
    }
    return { baseHz, multiplier, effectiveHz, error };
  }
  function armRate(rates, baseHz) {
    const setting = rates?.modes?.rollout || { kind: 'inherit' };
    const hz = setting.kind === 'hz' ? Number(setting.value)
      : setting.kind === 'multiplier' ? baseHz * Number(setting.value)
        : Number(rates?.default_hz ?? 30);
    return Number.isFinite(hz) ? hz : null;
  }
  function startError(base, speed, rates) {
    const inspected = inspect(base, speed);
    if (inspected.error) return inspected.error;
    const armHz = armRate(rates, inspected.baseHz);
    if (armHz == null || armHz < 1 || armHz > MAX_HZ) {
      return 'Rollout Arm rate must be between 1 and 240 Hz. Adjust Arm rate before starting.';
    }
    if (inspected.effectiveHz > armHz + 1e-9) {
      return `Execution needs ${format(inspected.effectiveHz)} Hz; Arm rate is ${format(armHz)} Hz. Raise the rollout Arm rate or lower execution speed before starting.`;
    }
    return '';
  }
  function format(value) {
    return Number.isFinite(value) ? String(Number(value.toPrecision(6))) : '—';
  }
  const api = { inspect, armRate, startError, format, MIN_HZ, MAX_HZ };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.RolloutSpeed = api;
}(globalThis));
