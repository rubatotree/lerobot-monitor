"""Enumerate serial adapters that look like SO-101 leader/follower buses.

Virtual buses published by a running Blender simulation are merged in as ordinary
rows, so the UI needs no notion of "real vs simulated" — a ``socket://`` port is
just another choice in the same dropdown.
"""

from __future__ import annotations

from typing import Any

_LIKELY = (
    "ch340",
    "ch341",
    "ch343",
    "ch34",
    "cp210",
    "ft232",
    "ftin",
    "wch.cn",
    "usb serial",
    "usb-serial",
    "enhanced-serial",
    "usb uart",
    "feetech",
    "acm",
    "usb2.0-serial",
)


def _likely(description: str, hwid: str, manufacturer: str = "") -> bool:
    blob = f"{description} {hwid} {manufacturer}".lower()
    return any(token in blob for token in _LIKELY)


def list_serial_ports() -> list[dict[str, Any]]:
    ports: list[dict[str, Any]] = []
    try:
        from serial.tools.list_ports import comports
    except ImportError:
        comports = None
    for item in comports() if comports else []:
        description = item.description or item.device
        hwid = getattr(item, "hwid", "") or ""
        ports.append(
            {
                "port": item.device,
                "name": item.name or item.device,
                "description": description,
                "hwid": hwid,
                "manufacturer": getattr(item, "manufacturer", "") or "",
                "likely": _likely(description, hwid, getattr(item, "manufacturer", "") or ""),
            }
        )
    ports.extend(_virtual_ports())
    ports.sort(key=lambda row: (not row["likely"], row["port"]))
    return ports


def _virtual_ports() -> list[dict[str, Any]]:
    """仿真机械臂的端点。任何一步失败都只是"没有仿真在跑"，不该影响真机枚举。"""
    try:
        from .sim import list_virtual_ports
    except ImportError:
        return []
    try:
        return list_virtual_ports()
    except Exception:  # noqa: BLE001 — 发现逻辑再怎么样也不能拖垮端口列表
        return []
