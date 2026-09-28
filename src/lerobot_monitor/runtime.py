"""Process identity: which Python, which lerobot, whether CUDA is real."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import Any

from .pathutil import ensure_lerobot_on_path

_CACHED: dict[str, Any] | None = None


def prepare_policy_runtime(log: Callable[[str, str], None]) -> dict[str, Any]:
    """Best-effort startup imports; missing optional policy packages stay nonfatal."""
    from .policy import import_policy_dependencies

    started = time.perf_counter()
    phase_started = started
    phase = ""
    timings: dict[str, float] = {}

    def finish_phase() -> None:
        if phase:
            timings[phase] = (time.perf_counter() - phase_started) * 1000.0
            log("info", f"policy dependencies DONE {phase} in {timings[phase] / 1000.0:.3f}s")

    def progress(next_phase: str, _: int) -> None:
        nonlocal phase, phase_started
        finish_phase()
        phase = next_phase
        phase_started = time.perf_counter()
        log("info", f"policy dependencies START {phase}")

    try:
        import_policy_dependencies(progress)
    except Exception as exc:  # Optional LeRobot installs must still support monitoring.
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        error = f"{type(exc).__name__}: {exc}"
        log("error", f"policy dependencies FAILED {phase} after {elapsed_ms / 1000.0:.3f}s: {error}")
        return {
            "state": "error", "phase": phase, "error": error,
            "elapsed_ms": elapsed_ms, "stage_durations_ms": timings,
        }
    finish_phase()
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    log("info", f"policy dependencies ready in {elapsed_ms / 1000.0:.3f}s")
    return {"state": "ready", "elapsed_ms": elapsed_ms, "stage_durations_ms": timings}


def probe_runtime() -> dict[str, Any]:
    global _CACHED
    if _CACHED is not None:
        return dict(_CACHED)
    src = ensure_lerobot_on_path()
    info: dict[str, Any] = {
        "python": sys.executable,
        "version": sys.version.split()[0],
        "prefix": sys.prefix,
        "lerobot_src": str(src) if src else None,
        "lerobot_file": None,
        "torch": None,
        "cuda": False,
        "cuda_built": None,
        "gpu": None,
    }
    try:
        import lerobot

        info["lerobot_file"] = getattr(lerobot, "__file__", None)
    except Exception as exc:  # noqa: BLE001
        info["lerobot_error"] = str(exc)
    try:
        import torch

        info["torch"] = torch.__version__
        info["cuda"] = bool(torch.cuda.is_available())
        info["cuda_built"] = torch.version.cuda
        if info["cuda"]:
            info["gpu"] = torch.cuda.get_device_name(0)
    except Exception as exc:  # noqa: BLE001
        info["torch_error"] = str(exc)
    _CACHED = info
    return dict(info)


def format_runtime(info: dict[str, Any] | None = None) -> str:
    data = info or probe_runtime()
    torch_v = data.get("torch") or "no-torch"
    if data.get("cuda"):
        gpu = data.get("gpu") or "cuda"
        return f"{data['version']}  {torch_v}  {gpu}"
    built = data.get("cuda_built")
    if torch_v and torch_v != "no-torch":
        tag = "cpu-wheel" if not built else "cuda-unavailable"
        return f"{data['version']}  {torch_v}  {tag}"
    return f"{data['version']}  no-torch"
