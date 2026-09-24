"""Robot-description registry for the embedded 3D arm preview.

A robot model is a directory containing ``robot_model.json`` plus a URDF and
its mesh assets.  The manifest is data only: it never contains executable code.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .store import JsonStore

SCHEMA = "lerobot.monitor.robot_model/v1"
MANIFEST_NAME = "robot_model.json"
HF_HOSTS = {"huggingface.co", "www.huggingface.co", "hf.co"}
_REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


class RobotModelError(RuntimeError):
    """Raised when a robot model package cannot be parsed or installed."""


@dataclass(frozen=True)
class RobotModelSource:
    source: str
    remote: str
    repo_id: str = ""
    revision: str = ""
    path: str = ""


def _slug(value: str) -> str:
    text = _SLUG.sub("-", str(value or "").strip()).strip("-.")
    return text[:120] or "robot-model"


def _path_key(path: str | Path) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(path)


def parse_robot_model_source(remote: str, *, revision: str = "") -> RobotModelSource:
    text = str(remote or "").strip()
    if not text:
        raise RobotModelError("model source is empty")
    if text.lower().startswith("hf:"):
        text = text[3:].strip()
    local = Path(text).expanduser()
    if local.exists():
        return RobotModelSource(source="local", remote=str(remote), path=str(local))
    if text.startswith(("http://", "https://")):
        parsed = urlparse(text)
        if parsed.netloc.lower() not in HF_HOSTS:
            raise RobotModelError("only huggingface.co URLs are supported")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            raise RobotModelError(f"not a Hugging Face repository URL: {text}")
        repo_id = "/".join(parts[:2])
        found_revision = revision.strip()
        if not found_revision and len(parts) >= 4 and parts[2] in {"tree", "resolve"}:
            found_revision = parts[3]
        return RobotModelSource(
            source="huggingface",
            remote=text,
            repo_id=repo_id,
            revision=found_revision,
        )
    if _REPO_ID.match(text):
        return RobotModelSource(
            source="huggingface",
            remote=text,
            repo_id=text,
            revision=revision.strip(),
        )
    raise RobotModelError(
        "source must be a Hugging Face repo id, huggingface.co URL, or existing local directory"
    )


def _hub_module() -> Any:
    try:
        import huggingface_hub  # noqa: PLC0415 - optional dependency
    except ImportError as exc:
        raise RobotModelError(
            "huggingface_hub is not installed; use a local robot-model directory"
        ) from exc
    return huggingface_hub


def search_hf_robot_models(query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    text = str(query or "").strip()
    if not text:
        raise RobotModelError("search query is empty")
    api = _hub_module().HfApi()
    try:
        # huggingface_hub 1.x dropped the `direction` argument; the Hub API
        # already returns `sort="downloads"` in descending order.
        rows = list(
            api.list_models(
                search=text,
                filter="robotics",
                limit=limit,
                sort="downloads",
            )
        )
        if not rows:
            rows = list(api.list_models(search=text, limit=limit, sort="downloads"))
    except Exception as exc:  # noqa: BLE001 - Hub errors are user-facing
        raise RobotModelError(f"Hugging Face search failed: {exc}") from exc
    return [
        {
            "repo_id": str(row.id),
            "downloads": int(getattr(row, "downloads", 0) or 0),
            "likes": int(getattr(row, "likes", 0) or 0),
            "last_modified": str(getattr(row, "last_modified", "") or ""),
            "tags": [
                tag
                for tag in (getattr(row, "tags", None) or [])
                if not str(tag).startswith("license:")
            ][:6],
        }
        for row in rows
    ]


class RobotModelRegistry:
    def __init__(
        self,
        store: JsonStore,
        root: Path,
        *,
        builtin_root: Path | None = None,
        max_file_mb: int = 128,
        max_bundle_mb: int = 512,
    ) -> None:
        self.store = store
        self.root = Path(root)
        self.builtin_root = builtin_root or (Path(__file__).resolve().parent / "robot_models")
        self.max_file_bytes = max(1, int(max_file_mb)) * 1024 * 1024
        self.max_bundle_bytes = max(1, int(max_bundle_mb)) * 1024 * 1024

    def list(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in self._builtin_entries():
            seen.add(str(entry["id"]))
            rows.append(self._decorate(entry))
        for saved in self.store.robot_models():
            model_id = str(saved.get("id") or "")
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            rows.append(self._decorate(saved))
        return rows

    def get(self, model_id: str) -> dict[str, Any]:
        key = str(model_id or "").strip()
        builtin = next((row for row in self._builtin_entries() if row["id"] == key), None)
        if builtin is not None:
            return self._decorate(builtin)
        entry = self.store.robot_model(key)
        if entry is None:
            raise KeyError(key)
        return self._decorate(entry)

    def active(self) -> str:
        active = self.store.active_robot_model()
        if active and any(row["id"] == active for row in self.list()):
            return active
        rows = self.list()
        return str(rows[0]["id"]) if rows else ""

    def activate(self, model_id: str) -> dict[str, Any]:
        row = self.get(model_id)
        if row.get("missing"):
            raise RobotModelError(f"robot model files are missing: {row.get('path')}")
        self.store.set_active_robot_model(str(row["id"]))
        return row

    def register(
        self,
        *,
        remote: str,
        name: str = "",
        revision: str = "",
        download: bool = True,
    ) -> dict[str, Any]:
        source = parse_robot_model_source(remote, revision=revision)
        if source.source == "local":
            root = Path(source.path)
            if root.is_file() and root.name == MANIFEST_NAME:
                root = root.parent
            manifest = self._read_manifest(root)
            model_id = self._manifest_id(manifest)
            entry = self._entry_from_manifest(
                root=root,
                manifest=manifest,
                source=source,
                model_id=model_id,
                name=name,
            )
            return self._decorate(self.store.put_robot_model(entry))

        hub = _hub_module()
        with tempfile.TemporaryDirectory(prefix="lerobot-robot-model-") as temp_dir:
            kwargs: dict[str, Any] = {"repo_id": source.repo_id, "local_dir": temp_dir}
            if source.revision:
                kwargs["revision"] = source.revision
            try:
                hub.snapshot_download(**kwargs)
            except Exception as exc:  # noqa: BLE001
                raise RobotModelError(f"could not download {source.repo_id}: {exc}") from exc
            downloaded = Path(temp_dir)
            manifest = self._read_manifest(downloaded)
            model_id = self._manifest_id(manifest)
            target = self._model_dir(model_id)
            if target.exists():
                if not download:
                    raise RobotModelError(f"robot model '{model_id}' is already installed")
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            staged = target.with_name(f".{target.name}.installing")
            if staged.exists():
                shutil.rmtree(staged)
            shutil.copytree(
                downloaded,
                staged,
                symlinks=False,
                ignore=shutil.ignore_patterns(".cache"),
            )
            self._validate_bundle(staged)
            staged.replace(target)
            manifest = self._read_manifest(target)
            entry = self._entry_from_manifest(
                root=target,
                manifest=manifest,
                source=source,
                model_id=model_id,
                name=name,
            )
            return self._decorate(self.store.put_robot_model(entry))

    def update(self, model_id: str) -> dict[str, Any]:
        entry = self.get(model_id)
        if entry.get("builtin"):
            return entry
        source = parse_robot_model_source(
            str(entry.get("remote") or ""),
            revision=str(entry.get("revision") or ""),
        )
        if source.source == "local":
            root = Path(source.path)
            manifest = self._read_manifest(root)
            updated = self._entry_from_manifest(
                root=root,
                manifest=manifest,
                source=source,
                model_id=str(entry["id"]),
                name=str(entry.get("name") or ""),
            )
            return self._decorate(self.store.put_robot_model(updated))
        return self.register(
            remote=str(entry.get("remote") or ""),
            revision=str(entry.get("revision") or ""),
            name=str(entry.get("name") or ""),
        )

    def delete(self, model_id: str) -> None:
        entry = self.get(model_id)
        if entry.get("builtin"):
            raise RobotModelError("built-in robot models cannot be deleted")
        path = Path(str(entry.get("path") or ""))
        root = self.root.resolve()
        if path.exists():
            resolved = path.resolve()
            if resolved != root and root in resolved.parents:
                shutil.rmtree(resolved)
        self.store.delete_robot_model(str(entry["id"]))

    def match_robot_type(self, robot_type: str) -> dict[str, Any] | None:
        wanted = str(robot_type or "").strip()
        if not wanted:
            return None
        for row in self.list():
            if wanted in (row.get("robot_types") or []):
                return row
        return None

    def resolve_file(self, model_id: str, relative_path: str) -> Path:
        row = self.get(model_id)
        root = Path(str(row.get("path") or "")).resolve()
        candidate = (root / str(relative_path)).resolve()
        if candidate != root and root not in candidate.parents:
            raise RobotModelError("robot-model file path escapes the package root")
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        return candidate

    def _builtin_entries(self) -> list[dict[str, Any]]:
        if not self.builtin_root.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for manifest_path in sorted(self.builtin_root.glob(f"*/{MANIFEST_NAME}")):
            try:
                manifest = self._read_manifest(manifest_path.parent)
                rows.append(
                    self._entry_from_manifest(
                        root=manifest_path.parent,
                        manifest=manifest,
                        source=RobotModelSource(
                            source="builtin",
                            remote="",
                            path=str(manifest_path.parent),
                        ),
                        model_id=self._manifest_id(manifest),
                        name="",
                        builtin=True,
                    )
                )
            except (OSError, RobotModelError, json.JSONDecodeError):
                continue
        return rows

    def _decorate(self, entry: dict[str, Any]) -> dict[str, Any]:
        row = dict(entry)
        root = Path(str(row.get("path") or ""))
        row["missing"] = not (root / MANIFEST_NAME).is_file()
        row["active"] = self.store.active_robot_model() == row.get("id")
        return row

    def _entry_from_manifest(
        self,
        *,
        root: Path,
        manifest: dict[str, Any],
        source: RobotModelSource,
        model_id: str,
        name: str,
        builtin: bool = False,
    ) -> dict[str, Any]:
        self._validate_manifest(root, manifest)
        return {
            "id": model_id,
            "name": str(name or manifest.get("name") or model_id),
            "path": str(root.resolve()),
            "source": source.source,
            "remote": source.remote,
            "repo_id": source.repo_id,
            "revision": source.revision,
            "builtin": bool(builtin),
            "robot_types": [str(value) for value in manifest.get("robot_types") or []],
            "urdf": str(manifest.get("urdf") or ""),
            "packages": dict(manifest.get("packages") or {}),
            "joint_map": dict(manifest.get("joint_map") or {}),
            "wrist_camera": dict(manifest.get("wrist_camera") or {}),
            "default_pose": dict(manifest.get("default_pose") or {}),
        }

    def _read_manifest(self, root: Path) -> dict[str, Any]:
        manifest_path = root / MANIFEST_NAME
        if not manifest_path.is_file():
            raise RobotModelError(f"{MANIFEST_NAME} not found in {root}")
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RobotModelError(f"invalid {MANIFEST_NAME}: {exc}") from exc
        if not isinstance(payload, dict):
            raise RobotModelError(f"{MANIFEST_NAME} must contain a JSON object")
        if payload.get("schema") != SCHEMA:
            raise RobotModelError(f"unsupported robot-model schema: {payload.get('schema')!r}")
        return payload

    def _manifest_id(self, manifest: dict[str, Any]) -> str:
        raw_id = str(manifest.get("id") or "").strip()
        if not raw_id:
            raise RobotModelError("robot-model id is empty")
        return _slug(raw_id)

    def _validate_manifest(self, root: Path, manifest: dict[str, Any]) -> None:
        robot_types = manifest.get("robot_types")
        if not isinstance(robot_types, list) or not robot_types:
            raise RobotModelError("robot_types must be a non-empty list")
        urdf = str(manifest.get("urdf") or "").strip()
        if not urdf:
            raise RobotModelError("urdf is required")
        candidate = (root / urdf).resolve()
        if root.resolve() not in candidate.parents or not candidate.is_file():
            raise RobotModelError(f"URDF is missing or outside the package: {urdf}")
        joint_map = manifest.get("joint_map")
        if not isinstance(joint_map, dict) or not joint_map:
            raise RobotModelError("joint_map must be a non-empty object")
        packages = manifest.get("packages") or {}
        if not isinstance(packages, dict):
            raise RobotModelError("packages must be an object")
        self._validate_bundle(root)

    def _validate_bundle(self, root: Path) -> None:
        total = 0
        for path in root.rglob("*"):
            if path.is_symlink():
                raise RobotModelError(f"robot-model packages may not contain symlinks: {path}")
            if not path.is_file():
                continue
            size = path.stat().st_size
            if size > self.max_file_bytes:
                raise RobotModelError(f"robot-model file exceeds the size limit: {path.name}")
            total += size
        if total > self.max_bundle_bytes:
            raise RobotModelError("robot-model package exceeds the total size limit")

    def _model_dir(self, model_id: str) -> Path:
        root = self.root.resolve()
        target = (root / model_id).resolve()
        if target != root and root not in target.parents:
            raise RobotModelError("robot-model id escapes the managed root")
        return target
