"""Persistent JSON store for cameras, form state, and named presets."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .hardware import DEFAULT_HARDWARE_PRESETS, SYSTEM_HARDWARE_PRESET_NAME
from .types import RELAX_POSE, ZERO_POSE

PRESET_KINDS = ("record", "rollout", "pose", "debug", "hardware")
EPISODE_KINDS = ("video", "dataset")
LIBRARY_KINDS = ("video", "dataset", "model")
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
            "models": [],
            "datasets": [],
            "episode_views": {kind: {} for kind in EPISODE_KINDS},
            "robot_models": [],
            "active_robot_model": "",
        }
        self._load()
        self._ensure_default_poses()
        self._ensure_default_hardware_presets()

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
                    self._data["library_overrides"][kind] = {
                        str(source_id): self._normalize_library_entry(entry)
                        for source_id, entry in group.items()
                        if isinstance(entry, dict)
                    }
        models = raw.get("models")
        if isinstance(models, list):
            self._data["models"] = [dict(entry) for entry in models if isinstance(entry, dict)]
        datasets = raw.get("datasets")
        if isinstance(datasets, list):
            self._data["datasets"] = [dict(entry) for entry in datasets if isinstance(entry, dict)]
        robot_models = raw.get("robot_models")
        if isinstance(robot_models, list):
            self._data["robot_models"] = [
                dict(entry) for entry in robot_models if isinstance(entry, dict)
            ]
        self._data["active_robot_model"] = str(raw.get("active_robot_model") or "")
        episode_views = raw.get("episode_views")
        if isinstance(episode_views, dict):
            for kind in EPISODE_KINDS:
                group = episode_views.get(kind)
                if isinstance(group, dict):
                    self._data["episode_views"][kind] = {
                        str(source_id): dict(view)
                        for source_id, view in group.items()
                        if isinstance(view, dict)
                    }

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

    def _ensure_default_hardware_presets(self) -> None:
        hardware = self._data["presets"].setdefault("hardware", {})
        changed = False
        legacy_name = "Nothing connected"
        if legacy_name in hardware and SYSTEM_HARDWARE_PRESET_NAME not in hardware:
            hardware[SYSTEM_HARDWARE_PRESET_NAME] = hardware.pop(legacy_name)
            if self._data["ui"].get("active_hardware_preset") == legacy_name:
                self._data["ui"]["active_hardware_preset"] = SYSTEM_HARDWARE_PRESET_NAME
            changed = True
        for name, preset in DEFAULT_HARDWARE_PRESETS.items():
            if name not in hardware:
                hardware[name] = json.loads(json.dumps(preset))
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
            existing = self._data["presets"][kind].get(key)
            if isinstance(existing, dict) and existing.get("system"):
                raise ValueError("cannot overwrite a system preset")
            saved = dict(payload)
            saved.pop("system", None)
            self._data["presets"][kind][key] = saved
            self._write()
            return dict(saved)

    def delete_preset(self, kind: str, name: str) -> None:
        if kind not in PRESET_KINDS:
            raise KeyError(kind)
        if kind == "pose" and name == "relax":
            raise ValueError("cannot delete the relax preset")
        with self._lock:
            existing = self._data["presets"][kind].get(name)
            if isinstance(existing, dict) and existing.get("system"):
                raise ValueError("cannot delete a system preset")
            self._data["presets"][kind].pop(name, None)
            if kind == "hardware" and self._data["ui"].get("active_hardware_preset") == name:
                self._data["ui"].pop("active_hardware_preset", None)
            self._write()

    def rename_preset(self, kind: str, name: str, new_name: str) -> dict[str, Any]:
        if kind not in PRESET_KINDS:
            raise KeyError(kind)
        key = str(new_name).strip()
        if not key:
            raise ValueError("preset name is empty")
        if kind == "pose" and name == "relax":
            raise ValueError("cannot rename the relax preset")
        with self._lock:
            presets = self._data["presets"][kind]
            existing = presets.get(name)
            if not isinstance(existing, dict):
                raise KeyError(name)
            if existing.get("system"):
                raise ValueError("cannot rename a system preset")
            if key != name and key in presets:
                raise ValueError("preset name already exists")
            presets[key] = presets.pop(name)
            if kind == "hardware" and self._data["ui"].get("active_hardware_preset") == name:
                self._data["ui"]["active_hardware_preset"] = key
            self._write()
            return dict(presets[key])

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
        """Return monitor-owned display metadata for one library source."""
        if kind not in LIBRARY_KINDS:
            raise KeyError(kind)
        with self._lock:
            saved = self._data["library_overrides"][kind].get(str(source_id))
            if not isinstance(saved, dict):
                return {}
            entry = self._normalize_library_entry(saved)
            result = {
                key: str(entry[key])
                for key in ("name", "description", "task", "repo_id", "path", "source", "remote", "revision")
                if entry.get(key) is not None
            }
            if isinstance(entry.get("metadata"), dict):
                result["metadata"] = dict(entry["metadata"])
            return result

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
            entry = self._normalize_library_entry(group) if isinstance(group, dict) else {}
            for field in ("name", "description", "task", "repo_id", "path", "source", "remote", "revision"):
                if field in payload:
                    entry[field] = str(payload[field])
            if "metadata" in payload and isinstance(payload["metadata"], dict):
                entry["metadata"] = dict(payload["metadata"])
            if "notes" in payload and "description" not in payload:
                entry["description"] = "\n".join(self._normalize_notes(payload["notes"]))
            elif "note" in payload and "description" not in payload:
                entry["description"] = str(payload["note"]).strip()
            entry.pop("notes", None)
            entry.pop("note", None)
            self._data["library_overrides"][kind][key] = entry
            self._write()
            result = {
                field: str(entry[field])
                for field in ("name", "description", "task", "repo_id", "path", "source", "remote", "revision")
                if entry.get(field) is not None
            }
            if isinstance(entry.get("metadata"), dict):
                result["metadata"] = dict(entry["metadata"])
            return result

    def delete_library_override(self, kind: str, source_id: str) -> None:
        if kind not in LIBRARY_KINDS:
            raise KeyError(kind)
        with self._lock:
            self._data["library_overrides"][kind].pop(str(source_id), None)
            self._write()

    @staticmethod
    def _normalize_notes(value: Any) -> list[str]:
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        if not isinstance(value, (list, tuple)):
            return []
        return [str(note).strip() for note in value if str(note).strip()]

    @classmethod
    def _normalize_library_entry(cls, entry: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(entry)
        if "description" not in normalized:
            if "notes" in normalized:
                normalized["description"] = "\n".join(cls._normalize_notes(normalized.pop("notes")))
            elif "note" in normalized:
                normalized["description"] = str(normalized.pop("note") or "").strip()
        normalized.pop("notes", None)
        normalized.pop("note", None)
        return normalized

    def models(self) -> list[dict[str, Any]]:
        """User-registered models; the scan of local caches stays separate."""
        with self._lock:
            return [dict(entry) for entry in self._data["models"]]

    def model(self, model_id: str) -> dict[str, Any] | None:
        key = str(model_id)
        with self._lock:
            for entry in self._data["models"]:
                if str(entry.get("id") or "") == key:
                    return dict(entry)
        return None

    def put_model(self, entry: dict[str, Any]) -> dict[str, Any]:
        key = str(entry.get("id") or "").strip()
        if not key:
            raise ValueError("model id is empty")
        payload = dict(entry)
        payload["id"] = key
        with self._lock:
            for index, saved in enumerate(self._data["models"]):
                if str(saved.get("id") or "") == key:
                    self._data["models"][index] = payload
                    break
            else:
                self._data["models"].append(payload)
            self._write()
        return dict(payload)

    def delete_model(self, model_id: str) -> None:
        key = str(model_id)
        with self._lock:
            self._data["models"] = [
                entry for entry in self._data["models"] if str(entry.get("id") or "") != key
            ]
            self._write()

    def robot_models(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(entry) for entry in self._data["robot_models"]]

    def robot_model(self, model_id: str) -> dict[str, Any] | None:
        key = str(model_id)
        with self._lock:
            for entry in self._data["robot_models"]:
                if str(entry.get("id") or "") == key:
                    return dict(entry)
        return None

    def put_robot_model(self, entry: dict[str, Any]) -> dict[str, Any]:
        key = str(entry.get("id") or "").strip()
        if not key:
            raise ValueError("robot model id is empty")
        payload = dict(entry)
        payload["id"] = key
        with self._lock:
            for index, saved in enumerate(self._data["robot_models"]):
                if str(saved.get("id") or "") == key:
                    self._data["robot_models"][index] = payload
                    break
            else:
                self._data["robot_models"].append(payload)
            self._write()
        return dict(payload)

    def delete_robot_model(self, model_id: str) -> None:
        key = str(model_id)
        with self._lock:
            self._data["robot_models"] = [
                entry
                for entry in self._data["robot_models"]
                if str(entry.get("id") or "") != key
            ]
            if str(self._data.get("active_robot_model") or "") == key:
                self._data["active_robot_model"] = ""
            self._write()

    def active_robot_model(self) -> str:
        with self._lock:
            return str(self._data.get("active_robot_model") or "")

    def set_active_robot_model(self, model_id: str) -> str:
        with self._lock:
            self._data["active_robot_model"] = str(model_id or "")
            self._write()
            return str(self._data["active_robot_model"])

    def datasets(self) -> list[dict[str, Any]]:
        """User-registered datasets; scan results stay separate."""
        with self._lock:
            return [dict(entry) for entry in self._data["datasets"]]

    def dataset(self, dataset_id: str) -> dict[str, Any] | None:
        key = str(dataset_id)
        with self._lock:
            for entry in self._data["datasets"]:
                if str(entry.get("id") or "") == key:
                    return dict(entry)
        return None

    def put_dataset(self, entry: dict[str, Any]) -> dict[str, Any]:
        key = str(entry.get("id") or "").strip()
        if not key:
            raise ValueError("dataset id is empty")
        payload = dict(entry)
        payload["id"] = key
        with self._lock:
            for index, saved in enumerate(self._data["datasets"]):
                if str(saved.get("id") or "") == key:
                    self._data["datasets"][index] = payload
                    break
            else:
                self._data["datasets"].append(payload)
            self._write()
        return dict(payload)

    def delete_dataset(self, dataset_id: str) -> None:
        key = str(dataset_id)
        with self._lock:
            self._data["datasets"] = [
                entry for entry in self._data["datasets"] if str(entry.get("id") or "") != key
            ]
            self._write()

    def episode_view(self, kind: str, source_id: str) -> dict[str, Any]:
        if kind not in EPISODE_KINDS:
            raise KeyError(kind)
        with self._lock:
            view = self._data["episode_views"][kind].get(str(source_id))
            return dict(view) if isinstance(view, dict) else {}

    def save_episode_view(self, kind: str, source_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind not in EPISODE_KINDS:
            raise KeyError(kind)
        key = str(source_id).strip()
        if not key:
            raise ValueError("episode source id is empty")
        with self._lock:
            entry = dict(self._data["episode_views"][kind].get(key) or {})
            if "order" in payload:
                entry["order"] = [int(index) for index in payload["order"]]
            if "hidden" in payload:
                entry["hidden"] = sorted({int(index) for index in payload["hidden"]})
            self._data["episode_views"][kind][key] = entry
            self._write()
            return dict(entry)

    def delete_episode_view(self, kind: str, source_id: str) -> None:
        if kind not in EPISODE_KINDS:
            raise KeyError(kind)
        with self._lock:
            self._data["episode_views"][kind].pop(str(source_id), None)
            self._write()
