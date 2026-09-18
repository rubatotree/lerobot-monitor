"""Enumerate serial adapters that look like SO-101 leader/follower buses."""

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
        return ports
    for item in comports():
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
    ports.sort(key=lambda row: (not row["likely"], row["port"]))
    return ports
