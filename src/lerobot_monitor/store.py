"""Persistent JSON store for cameras, form state, and named presets."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .types import RELAX_POSE, ZERO_POSE

PRESET_KINDS = ("record", "rollout", "pose")
DEFAULT_POSE_PRESETS: dict[str, dict[str, float]] = {
    "relax": dict(RELAX_POSE),
    "zero": dict(ZERO_POSE),
}


class JsonStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "cameras": {},
            "ui": {},
            "presets": {kind: {} for kind in PRESET_KINDS},
        }
        self._load()
        self._ensure_default_poses()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        if isinstance(raw.get("cameras"), dict):
            self._data["cameras"] = raw["cameras"]
        if isinstance(raw.get("ui"), dict):
            self._data["ui"] = raw["ui"]
        presets = raw.get("presets")
        if isinstance(presets, dict):
            for kind in PRESET_KINDS:
                group = presets.get(kind)
                if isinstance(group, dict):
                    self._data["presets"][kind] = group

    def _ensure_default_poses(self) -> None:
        pose = self._data["presets"].setdefault("pose", {})
        changed = False
        for name, joints in DEFAULT_POSE_PRESETS.items():
            if name not in pose:
                pose[name] = dict(joints)
                changed = True
        if changed and self.path.parent:
            try:
                self._write()
            except OSError:
                pass

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def camera_settings(self, name: str) -> dict[str, Any]:
        with self._lock:
            saved = self._data["cameras"].get(str(name))
            return dict(saved) if isinstance(saved, dict) else {}

    def save_camera(self, name: str, settings: dict[str, Any]) -> None:
        with self._lock:
            prev = self._data["cameras"].get(str(name))
            merged = dict(prev) if isinstance(prev, dict) else {}
            merged.update(settings)
            self._data["cameras"][str(name)] = merged
            self._write()

    def ui(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data["ui"])

    def save_ui(self, ui: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._data["ui"] = dict(ui)
            self._write()
            return dict(self._data["ui"])

    def presets(self, kind: str | None = None) -> dict[str, Any]:
        with self._lock:
            if kind is None:
                return {k: dict(v) for k, v in self._data["presets"].items()}
            if kind not in PRESET_KINDS:
                raise KeyError(kind)
            return dict(self._data["presets"][kind])

    def put_preset(self, kind: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        key = name.strip()
        if not key:
            raise ValueError("preset name is empty")
        if kind not in PRESET_KINDS:
            raise KeyError(kind)
        with self._lock:
            self._data["presets"][kind][key] = dict(payload)
            self._write()
            return dict(self._data["presets"][kind][key])

    def delete_preset(self, kind: str, name: str) -> None:
        if kind not in PRESET_KINDS:
            raise KeyError(kind)
        if kind == "pose" and name == "relax":
            raise ValueError("cannot delete the relax preset")
        with self._lock:
            self._data["presets"][kind].pop(name, None)
            self._write()
