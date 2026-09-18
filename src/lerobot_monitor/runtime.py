"""Process identity: which Python, which lerobot, whether CUDA is real."""

from __future__ import annotations

import sys
from typing import Any

from .pathutil import ensure_lerobot_on_path

_CACHED: dict[str, Any] | None = None


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
