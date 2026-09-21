"""Persistent JSON store for cameras, form state, and named presets."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .types import RELAX_POSE, ZERO_POSE

PRESET_KINDS = ("record", "rollout", "pose", "debug")
EPISODE_KINDS = ("video", "dataset")
LIBRARY_KINDS = ("video", "dataset")
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
            "episode_overrides": {kind: {} for kind in EPISODE_KINDS},
            "library_overrides": {kind: {} for kind in LIBRARY_KINDS},
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
        overrides = raw.get("episode_overrides")
        if isinstance(overrides, dict):
            for kind in EPISODE_KINDS:
                group = overrides.get(kind)
                if isinstance(group, dict):
                    self._data["episode_overrides"][kind] = group
        library_overrides = raw.get("library_overrides")
        if isinstance(library_overrides, dict):
            for kind in LIBRARY_KINDS:
                group = library_overrides.get(kind)
                if isinstance(group, dict):
                    self._data["library_overrides"][kind] = group

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

    def episode_overrides(self, kind: str, source_id: str) -> dict[str, dict[str, Any]]:
        """Per-episode name / task / note for one video or dataset, keyed by episode index."""
        if kind not in EPISODE_KINDS:
            raise KeyError(kind)
        with self._lock:
            saved = self._data["episode_overrides"][kind].get(str(source_id))
            if not isinstance(saved, dict):
                return {}
            return {str(index): dict(row) for index, row in saved.items() if isinstance(row, dict)}

    def save_episode_override(
        self,
        kind: str,
        source_id: str,
        episode: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if kind not in EPISODE_KINDS:
            raise KeyError(kind)
        key = str(source_id).strip()
        if not key:
            raise ValueError("episode source id is empty")
        if int(episode) < 0:
            raise ValueError("episode index must be >= 0")
        with self._lock:
            group = self._data["episode_overrides"][kind].setdefault(key, {})
            entry = dict(group.get(str(int(episode))) or {})
            entry.update(payload)
            group[str(int(episode))] = entry
            self._write()
            return dict(entry)

    def remap_episode_overrides(
        self,
        kind: str,
        source_id: str,
        index_map: dict[int | str, int],
    ) -> dict[str, dict[str, Any]]:
        """Move overrides after episode reorder/delete and drop removed episodes."""
        if kind not in EPISODE_KINDS:
            raise KeyError(kind)
        key = str(source_id)
        normalized = {str(int(old)): str(int(new)) for old, new in index_map.items()}
        with self._lock:
            saved = self._data["episode_overrides"][kind].get(key)
            if not isinstance(saved, dict):
                return {}
            remapped = {
                normalized[old]: dict(payload)
                for old, payload in saved.items()
                if old in normalized and isinstance(payload, dict)
            }
            if remapped:
                self._data["episode_overrides"][kind][key] = remapped
            else:
                self._data["episode_overrides"][kind].pop(key, None)
            self._write()
            return {index: dict(payload) for index, payload in remapped.items()}

    def delete_episode_overrides(self, kind: str, source_id: str) -> None:
        if kind not in EPISODE_KINDS:
            raise KeyError(kind)
        with self._lock:
            self._data["episode_overrides"][kind].pop(str(source_id), None)
            self._write()

    def library_override(self, kind: str, source_id: str) -> dict[str, str]:
        """Return the monitor-owned note and description for one library source."""
        if kind not in LIBRARY_KINDS:
            raise KeyError(kind)
        with self._lock:
            saved = self._data["library_overrides"][kind].get(str(source_id))
            if not isinstance(saved, dict):
                return {}
            return {
                key: str(saved[key])
                for key in ("note", "description")
                if saved.get(key) is not None
            }

    def save_library_override(
        self,
        kind: str,
        source_id: str,
        payload: dict[str, Any],
    ) -> dict[str, str]:
        if kind not in LIBRARY_KINDS:
            raise KeyError(kind)
        key = str(source_id).strip()
        if not key:
            raise ValueError("library source id is empty")
        with self._lock:
            group = self._data["library_overrides"][kind].setdefault(key, {})
            entry = dict(group) if isinstance(group, dict) else {}
            for field in ("note", "description"):
                if field in payload:
                    entry[field] = str(payload[field])
            self._data["library_overrides"][kind][key] = entry
            self._write()
            return {
                field: str(entry[field])
                for field in ("note", "description")
                if entry.get(field) is not None
            }

    def delete_library_override(self, kind: str, source_id: str) -> None:
        if kind not in LIBRARY_KINDS:
            raise KeyError(kind)
        with self._lock:
            self._data["library_overrides"][kind].pop(str(source_id), None)
            self._write()
