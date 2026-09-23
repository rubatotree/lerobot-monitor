"""Hugging Face dataset search/download and empty LeRobot dataset creation."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .library import dataset_entry, lerobot_home, list_hf_datasets, slugify
from .store import JsonStore
from .types import JOINT_ORDER

HF_HOSTS = {"huggingface.co", "www.huggingface.co", "hf.co"}
_REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?$")
_SAFE_CAMERA = re.compile(r"[^A-Za-z0-9._-]+")


class DatasetHubError(RuntimeError):
    """Raised when a dataset remote cannot be searched, downloaded, or created."""


def _hub_module() -> Any:
    try:
        import huggingface_hub  # noqa: PLC0415 - optional, LeRobot brings it in
    except ImportError as exc:
        raise DatasetHubError(
            "huggingface_hub is not installed in this environment; local dataset scanning still works"
        ) from exc
    return huggingface_hub


def _parse_remote(remote: str, revision: str = "") -> tuple[str, str]:
    text = str(remote or "").strip()
    if not text:
        raise DatasetHubError("dataset address is empty")
    if text.lower().startswith("hf:"):
        text = text[len("hf:") :].strip()
    found_revision = str(revision or "").strip()
    if text.startswith(("http://", "https://")):
        parsed = urlparse(text)
        if parsed.netloc.lower() not in HF_HOSTS:
            raise DatasetHubError(f"only huggingface.co dataset URLs are supported, got {parsed.netloc or text}")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            raise DatasetHubError(f"not a dataset URL: {text}")
        repo_id = "/".join(parts[:2])
        if not found_revision and len(parts) >= 4 and parts[2] in {"tree", "resolve"}:
            found_revision = parts[3]
    else:
        repo_id = text
    if not _REPO_ID.fullmatch(repo_id):
        raise DatasetHubError(f"not a valid Hugging Face dataset id: {repo_id}")
    return repo_id, found_revision


def search_hf_datasets(query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    text = str(query or "").strip()
    if not text:
        raise DatasetHubError("search query is empty")
    api = _hub_module().HfApi()

    def fetch(tag: str | None) -> list[Any]:
        return list(api.list_datasets(search=text, filter=tag, limit=limit, sort="downloads", direction=-1))

    try:
        rows = fetch("lerobot")
        if not rows:
            rows = fetch(None)
    except Exception as exc:  # noqa: BLE001 - surface Hub/transport failures to the UI
        raise DatasetHubError(f"Hugging Face dataset search failed: {exc}") from exc
    return [
        {
            "repo_id": str(row.id),
            "downloads": int(getattr(row, "downloads", 0) or 0),
            "likes": int(getattr(row, "likes", 0) or 0),
            "last_modified": str(getattr(row, "last_modified", "") or ""),
            "tags": [tag for tag in (getattr(row, "tags", None) or []) if not tag.startswith("license:")][:6],
        }
        for row in rows
    ]


def download_hf_dataset(remote: str, *, revision: str = "") -> str:
    repo_id, parsed_revision = _parse_remote(remote, revision)
    hub = _hub_module()
    kwargs: dict[str, Any] = {"repo_id": repo_id, "repo_type": "dataset"}
    if parsed_revision:
        kwargs["revision"] = parsed_revision
    try:
        path = Path(hub.snapshot_download(**kwargs))
    except Exception as exc:  # noqa: BLE001 - Hub errors are many and all user-facing
        raise DatasetHubError(f"could not download {repo_id}: {exc}") from exc
    if not (path / "meta" / "info.json").is_file():
        raise DatasetHubError(f"{repo_id} is not a LeRobot dataset: meta/info.json is missing")
    return str(path)


def _normalize_camera_key(value: str, fallback: str) -> str:
    key = _SAFE_CAMERA.sub("_", str(value or "").strip()).strip("._-")
    return key or fallback


def _feature_cameras(cameras: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    features: dict[str, dict[str, Any]] = {}
    used: set[str] = set()
    for index, camera in enumerate(cameras):
        if not isinstance(camera, dict):
            continue
        base = _normalize_camera_key(str(camera.get("key") or camera.get("name") or ""), f"camera{index + 1}")
        key = base
        suffix = 2
        while key.casefold() in used:
            key = f"{base}_{suffix}"
            suffix += 1
        used.add(key.casefold())
        try:
            width = max(1, int(camera.get("width") or 640))
            height = max(1, int(camera.get("height") or 480))
        except (TypeError, ValueError):
            width, height = 640, 480
        features[f"observation.images.{key}"] = {
            "dtype": "video",
            "shape": [height, width, 3],
            "names": ["height", "width", "channels"],
        }
    return features


def create_empty_dataset(
    *,
    name: str,
    repo_id: str = "",
    fps: int = 15,
    robot_type: str = "",
    cameras: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a minimal, readable LeRobot v3 dataset with zero episodes."""
    if int(fps) <= 0:
        raise DatasetHubError("fps must be positive")
    resolved_repo = str(repo_id or "").strip() or slugify(name, fallback="dataset")
    if not _REPO_ID.fullmatch(resolved_repo):
        raise DatasetHubError(f"invalid dataset repo id: {resolved_repo}")
    destination = lerobot_home() / resolved_repo
    if destination.exists():
        raise DatasetHubError(f"dataset already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    joint_names = [f"{joint}.pos" for joint in JOINT_ORDER]
    features: dict[str, dict[str, Any]] = {
        "action": {"dtype": "float32", "shape": [len(joint_names)], "names": joint_names},
        "observation.state": {"dtype": "float32", "shape": [len(joint_names)], "names": joint_names},
        **_feature_cameras(cameras or []),
        "timestamp": {"dtype": "float32", "shape": [1], "names": None},
        "frame_index": {"dtype": "int64", "shape": [1], "names": None},
        "episode_index": {"dtype": "int64", "shape": [1], "names": None},
        "index": {"dtype": "int64", "shape": [1], "names": None},
        "task_index": {"dtype": "int64", "shape": [1], "names": None},
    }
    info = {
        "codebase_version": "v3.0",
        "fps": int(fps),
        "features": features,
        "total_episodes": 0,
        "total_frames": 0,
        "total_tasks": 0,
        "chunks_size": 1000,
        "data_files_size_in_mb": 100,
        "video_files_size_in_mb": 200,
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"
        if _feature_cameras(cameras or [])
        else None,
        "robot_type": str(robot_type or "").strip() or None,
        "splits": {},
    }
    temp_path = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    )
    try:
        (temp_path / "meta").mkdir(parents=True)
        (temp_path / "data").mkdir()
        (temp_path / "videos").mkdir()
        (temp_path / "meta" / "info.json").write_text(
            json.dumps(info, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temp_path.rename(destination)
    except BaseException:
        shutil.rmtree(temp_path, ignore_errors=True)
        raise
    return {
        "id": resolved_repo,
        "repo_id": resolved_repo,
        "name": resolved_repo,
        "title": str(name or resolved_repo),
        "path": str(destination),
        "source": "lerobot",
        "episodes": 0,
        "fps": int(fps),
        "lerobot": True,
        "has_video": bool(cameras),
        "previewable": False,
        "playable": False,
    }


def _resolve_dataset_source(remote: str, revision: str = "") -> dict[str, Any]:
    text = str(remote or "").strip()
    candidate = Path(text).expanduser()
    if candidate.is_dir():
        if not (candidate / "meta" / "info.json").is_file():
            raise DatasetHubError(f"local dataset is missing meta/info.json: {candidate}")
        return {
            "source": "local",
            "remote": str(candidate),
            "repo_id": "",
            "path": str(candidate.resolve()),
            "revision": str(revision or ""),
        }
    repo_id, parsed_revision = _parse_remote(text, revision)
    return {
        "source": "hub",
        "remote": text,
        "repo_id": repo_id,
        "path": download_hf_dataset(repo_id, revision=parsed_revision),
        "revision": parsed_revision,
    }


class DatasetRegistry:
    """Scanned datasets plus editable local/Hub registrations."""

    def __init__(self, store: JsonStore, roots: list[Path]) -> None:
        self.store = store
        self.roots = [Path(root) for root in roots]

    def _scanned(self) -> list[dict[str, Any]]:
        return list_hf_datasets(self.roots)

    def _decorate(self, entry: dict[str, Any]) -> dict[str, Any]:
        row = dict(entry)
        path = Path(str(row.get("path") or ""))
        repo_id = str(row.get("repo_id") or row.get("id") or "")
        if path.is_dir() and (path / "meta" / "info.json").is_file():
            scanned = dataset_entry(repo_id or path.name, path, str(row.get("source") or "local")) or {}
            scanned.update(row)
            row = scanned
        row["id"] = str(entry.get("id") or row.get("repo_id") or row.get("id") or "")
        row["managed"] = True
        row["upstream"] = bool(row.get("repo_id"))
        row["editable"] = True
        return row

    def list(self) -> list[dict[str, Any]]:
        registered = [self._decorate(entry) for entry in self.store.datasets()]
        claimed_ids = {str(row.get("id") or "") for row in registered}
        claimed_repos = {str(row.get("repo_id") or "") for row in registered if row.get("repo_id")}
        claimed_paths = {
            str(Path(str(row.get("path"))).resolve())
            for row in registered
            if row.get("path")
        }
        rows = list(registered)
        for entry in self._scanned():
            row = dict(entry)
            row["id"] = str(row.get("repo_id") or row.get("id") or "")
            row["managed"] = False
            row["upstream"] = bool(row.get("repo_id"))
            row["editable"] = True
            path = str(Path(str(row.get("path"))).resolve()) if row.get("path") else ""
            if row["id"] in claimed_ids or row.get("repo_id") in claimed_repos or path in claimed_paths:
                continue
            rows.append(row)
        return rows

    def get(self, dataset_id: str) -> dict[str, Any]:
        for row in self.list():
            if str(row.get("id") or "") == str(dataset_id):
                return row
        raise FileNotFoundError(dataset_id)

    def register(
        self,
        *,
        remote: str,
        name: str = "",
        revision: str = "",
        repo_id: str = "",
        download: bool = True,
    ) -> dict[str, Any]:
        del download  # Dataset registration always resolves the source before saving.
        resolved = _resolve_dataset_source(remote, revision)
        repo_id = str(repo_id or resolved.get("repo_id") or "")
        path = str(resolved.get("path") or "")
        dataset_id = repo_id or Path(path).name or slugify(name, fallback="dataset")
        entry = self.store.dataset(dataset_id) or {
            "id": dataset_id,
            "created_utc": datetime.now(timezone.utc).isoformat(),
        }
        entry.update(
            {
                "name": str(name or entry.get("name") or dataset_id),
                **resolved,
                "repo_id": repo_id,
                "updated_utc": datetime.now(timezone.utc).isoformat(),
            }
        )
        return self._decorate(self.store.put_dataset(entry))

    def save(self, dataset_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        entry = self.store.dataset(dataset_id)
        if entry is None:
            current = self.get(dataset_id)
            entry = {
                "id": dataset_id,
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "name": current.get("display_name") or current.get("title") or dataset_id,
                "path": current.get("path") or "",
                "repo_id": current.get("repo_id") or "",
                "source": current.get("source") or "",
            }
        if "name" in payload and payload["name"] is not None:
            entry["name"] = str(payload["name"]).strip() or entry.get("name") or dataset_id
        source_value = payload.get("remote", payload.get("path"))
        if source_value is not None:
            resolved = _resolve_dataset_source(str(source_value), str(payload.get("revision") or ""))
            entry.update(resolved)
        elif payload.get("revision") is not None:
            entry["revision"] = str(payload["revision"])
        entry["updated_utc"] = datetime.now(timezone.utc).isoformat()
        return self._decorate(self.store.put_dataset(entry))

    def update(self, dataset_id: str) -> dict[str, Any]:
        entry = self.store.dataset(dataset_id)
        if entry is None:
            current = self.get(dataset_id)
            entry = {
                "id": dataset_id,
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "name": current.get("display_name") or current.get("title") or dataset_id,
                "path": current.get("path") or "",
                "repo_id": current.get("repo_id") or "",
                "source": current.get("source") or "",
            }
        repo_id = str(entry.get("repo_id") or "")
        if not repo_id:
            raise DatasetHubError("dataset has no upstream Hugging Face repo_id")
        resolved = _resolve_dataset_source(repo_id, str(entry.get("revision") or ""))
        entry.update(resolved)
        entry["updated_utc"] = datetime.now(timezone.utc).isoformat()
        return self._decorate(self.store.put_dataset(entry))

    def upload(self, dataset_id: str) -> dict[str, Any]:
        entry = self.store.dataset(dataset_id)
        if entry is None:
            current = self.get(dataset_id)
            entry = {
                "id": dataset_id,
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "name": current.get("display_name") or current.get("title") or dataset_id,
                "path": current.get("path") or "",
                "repo_id": current.get("repo_id") or "",
                "source": current.get("source") or "",
            }
        repo_id = str(entry.get("repo_id") or "").strip()
        path = Path(str(entry.get("path") or ""))
        if not repo_id:
            raise DatasetHubError("dataset has no upstream Hugging Face repo_id")
        if not path.is_dir():
            raise DatasetHubError(f"dataset path not found: {path}")
        hub = _hub_module()
        try:
            hub.create_repo(repo_id, repo_type="dataset", exist_ok=True)
            hub.upload_folder(
                repo_id=repo_id,
                repo_type="dataset",
                folder_path=str(path),
                revision=str(entry.get("revision") or "") or None,
            )
        except Exception as exc:  # noqa: BLE001 - Hub/auth errors are user-facing
            raise DatasetHubError(f"could not upload {repo_id}: {exc}") from exc
        entry["updated_utc"] = datetime.now(timezone.utc).isoformat()
        return self._decorate(self.store.put_dataset(entry))

    def delete(self, dataset_id: str) -> dict[str, Any]:
        entry = self.store.dataset(dataset_id)
        if entry is None:
            current = self.get(dataset_id)
            entry = {
                "id": dataset_id,
                "path": current.get("path") or "",
                "repo_id": current.get("repo_id") or "",
            }
        self.store.delete_dataset(dataset_id)
        return dict(entry)
