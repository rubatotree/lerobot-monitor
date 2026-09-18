"""Local video sessions, Hugging Face dataset cache, and model scan."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .session import EpisodeWriter, mosaic_bgr, safe_cam_name

_SLUG = re.compile(r"[^a-zA-Z0-9._-]+")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def slugify(name: str, fallback: str = "dataset") -> str:
    text = _SLUG.sub("_", (name or "").strip()).strip("._-")
    return text or fallback


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def episode_dir(root: Path, index: int) -> Path:
    return root / "episodes" / f"{int(index):06d}"


class DatasetRecorder:
    """Append frames to a local dataset, rotating files at episode boundaries."""

    def __init__(
        self,
        root: Path,
        *,
        fps: int,
        kind: str,
        extra_meta: dict[str, Any] | None = None,
        resume: bool = False,
        video_format: str = "mp4",
        merge: bool = True,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.fps = max(1, int(fps))
        self.kind = kind
        self.video_format = video_format
        self.merge = merge
        self.dataset_id = self.root.name
        self.session_id = self.dataset_id
        self.dir = self.root
        self.closed = False
        self.frame_index = 0
        self.episode_index = 0
        self._lock = threading.Lock()
        self._episode: EpisodeWriter | None = None
        meta_path = self.root / "meta.json"
        meta = _read_json(meta_path) if resume and meta_path.is_file() else {}
        episodes = list(meta.get("episodes") or [])
        if resume and episodes:
            self.episode_index = max(int(e.get("index", i)) for i, e in enumerate(episodes)) + 1
            self.frame_index = int(meta.get("frames") or 0)
        self.meta: dict[str, Any] = {
            "id": self.dataset_id,
            "kind": kind,
            "fps": self.fps,
            "format": video_format,
            "created_utc": meta.get("created_utc") or datetime.now(timezone.utc).isoformat(),
            "episodes": episodes,
            "frames": self.frame_index,
            **(extra_meta or {}),
        }
        if not resume:
            self.meta["episodes"] = []
            self.episode_index = 0
            self.frame_index = 0
        self._write_meta()

    def _write_meta(self) -> None:
        self.meta["frames"] = self.frame_index
        self.meta["updated_utc"] = datetime.now(timezone.utc).isoformat()
        _write_json(self.root / "meta.json", self.meta)

    def _ensure_episode(self, index: int) -> EpisodeWriter:
        if self._episode is not None and self._episode.index == index and not self._episode.closed:
            return self._episode
        if self._episode is not None:
            info = self._episode.close()
            self._remember_episode(info)
        self._episode = EpisodeWriter(
            episode_dir(self.root, index),
            index=index,
            fps=self.fps,
            video_format=self.video_format,
            merge=self.merge,
            kind=self.kind,
        )
        self.episode_index = index
        return self._episode

    def _remember_episode(self, info: dict[str, Any]) -> None:
        episodes = [e for e in self.meta.get("episodes") or [] if int(e.get("index", -1)) != int(info["index"])]
        episodes.append(info)
        episodes.sort(key=lambda e: int(e.get("index", 0)))
        self.meta["episodes"] = episodes
        self._write_meta()

    def add_frame(
        self,
        observation: dict[str, float],
        action: dict[str, float] | None,
        images_bgr: dict[str, Any],
        *,
        episode_index: int | None = None,
        kind: str | None = None,
    ) -> None:
        if self.closed:
            return
        index = self.episode_index if episode_index is None else int(episode_index)
        with self._lock:
            writer = self._ensure_episode(index)
            writer.add_frame(observation, action, images_bgr, kind=kind or self.kind, frame_index=self.frame_index)
            self.frame_index += 1

    def close(self) -> Path:
        if self.closed:
            return self.root
        with self._lock:
            if self._episode is not None:
                info = self._episode.close()
                self._remember_episode(info)
                self._episode = None
            self.meta["frames"] = self.frame_index
            self.meta["closed_utc"] = datetime.now(timezone.utc).isoformat()
            self._write_meta()
            self.closed = True
        return self.root


class VideoLibrary:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    def _dataset_dir(self, dataset_id: str) -> Path:
        name = Path(dataset_id).name
        if not name or name in {".", ".."}:
            raise ValueError("invalid video id")
        path = (self.root / name).resolve()
        root = self.root.resolve()
        if path != root and root not in path.parents:
            raise ValueError("video path escapes library root")
        return path

    def create(
        self,
        name: str,
        *,
        fps: int = 15,
        task: str = "",
        repo_id: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        base = slugify(name or repo_id or task or "dataset")
        dataset_id = f"{base}_{_utc_stamp()}"
        path = self.root / dataset_id
        n = 1
        while path.exists():
            n += 1
            path = self.root / f"{dataset_id}_{n}"
            dataset_id = path.name
        meta = {
            "id": dataset_id,
            "name": name or base,
            "task": task,
            "repo_id": repo_id,
            "fps": int(fps),
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "episodes": [],
            "frames": 0,
            **(extra or {}),
        }
        path.mkdir(parents=True, exist_ok=True)
        (path / "episodes").mkdir(exist_ok=True)
        _write_json(path / "meta.json", meta)
        meta["path"] = str(path)
        return meta

    def get(self, dataset_id: str) -> dict[str, Any]:
        path = self._dataset_dir(dataset_id)
        meta_path = path / "meta.json"
        if not meta_path.is_file():
            raise FileNotFoundError(dataset_id)
        meta = _read_json(meta_path)
        meta["id"] = meta.get("id") or path.name
        meta["path"] = str(path)
        meta["episodes"] = self._episodes(path, meta)
        return meta

    def _episodes(self, path: Path, meta: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        listed = list((meta or {}).get("episodes") or [])
        by_index = {int(e.get("index", i)): dict(e) for i, e in enumerate(listed)}
        ep_root = path / "episodes"
        if ep_root.is_dir():
            for child in sorted(ep_root.iterdir()):
                if not child.is_dir():
                    continue
                try:
                    index = int(child.name)
                except ValueError:
                    continue
                row = by_index.get(index, {"index": index})
                row["index"] = index
                row["dir"] = str(child)
                videos = sorted((child / "videos").glob("*")) if (child / "videos").is_dir() else []
                row["videos"] = [v.name for v in videos]
                preview = child / "preview.jpg"
                row["preview"] = preview.name if preview.is_file() else None
                by_index[index] = row
        return [by_index[k] for k in sorted(by_index)]

    def list(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        out: list[dict[str, Any]] = []
        for path in sorted(self.root.iterdir(), reverse=True):
            if not path.is_dir() or not (path / "meta.json").is_file():
                continue
            try:
                out.append(self.get(path.name))
            except (OSError, ValueError):
                continue
        return out

    def delete(self, dataset_id: str) -> None:
        path = self._dataset_dir(dataset_id)
        if not path.exists():
            raise FileNotFoundError(dataset_id)
        shutil.rmtree(path)

    def delete_episode(self, dataset_id: str, index: int) -> dict[str, Any]:
        path = self._dataset_dir(dataset_id)
        target = episode_dir(path, index)
        if not target.exists():
            raise FileNotFoundError(f"episode {index}")
        shutil.rmtree(target)
        return self._reindex(path)

    def reorder(self, dataset_id: str, order: list[int]) -> dict[str, Any]:
        path = self._dataset_dir(dataset_id)
        current = [int(e["index"]) for e in self._episodes(path)]
        if sorted(order) != sorted(current):
            raise ValueError("order must list every episode index exactly once")
        tmp_root = path / ".reorder_tmp"
        if tmp_root.exists():
            shutil.rmtree(tmp_root)
        tmp_root.mkdir()
        mapping: list[tuple[int, int]] = []
        for new_index, old_index in enumerate(order):
            src = episode_dir(path, old_index)
            dst = tmp_root / f"{new_index:06d}"
            if src.exists():
                shutil.move(str(src), str(dst))
            mapping.append((old_index, new_index))
        ep_root = path / "episodes"
        if ep_root.exists():
            shutil.rmtree(ep_root)
        shutil.move(str(tmp_root), str(ep_root))
        return self._reindex(path)

    def _reindex(self, path: Path) -> dict[str, Any]:
        meta = _read_json(path / "meta.json")
        episodes: list[dict[str, Any]] = []
        ep_root = path / "episodes"
        if ep_root.is_dir():
            children = sorted([p for p in ep_root.iterdir() if p.is_dir()])
            for new_index, child in enumerate(children):
                dest = episode_dir(path, new_index)
                if child != dest:
                    child.rename(dest)
                info = {"index": new_index, "dir": dest.name}
                preview = dest / "preview.jpg"
                if preview.is_file():
                    info["preview"] = preview.name
                videos = sorted((dest / "videos").glob("*")) if (dest / "videos").is_dir() else []
                info["videos"] = [v.name for v in videos]
                episodes.append(info)
        meta["episodes"] = episodes
        meta["updated_utc"] = datetime.now(timezone.utc).isoformat()
        _write_json(path / "meta.json", meta)
        return self.get(path.name)

    def episode_video(self, dataset_id: str, index: int, cam: str) -> Path:
        path = self._dataset_dir(dataset_id)
        folder = episode_dir(path, index) / "videos"
        if cam == "merged":
            candidate = folder / "merged.mp4"
            if candidate.is_file():
                return candidate
            mp4s = sorted(folder.glob("*.mp4"))
            if mp4s:
                return mp4s[0]
            raise FileNotFoundError("merged video")
        name = safe_cam_name(cam)
        for ext in (".mp4", ".avi"):
            candidate = folder / f"{name}{ext}"
            if candidate.is_file():
                return candidate
        raise FileNotFoundError(cam)

    def episode_preview(self, dataset_id: str, index: int) -> Path:
        path = episode_dir(self._dataset_dir(dataset_id), index) / "preview.jpg"
        if not path.is_file():
            raise FileNotFoundError("preview")
        return path


def list_local_models(roots: list[Path], *, max_depth: int = 5) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    skip = {".git", ".venv", "node_modules", "__pycache__", ".uv", "wandb"}

    def is_policy(path: Path) -> bool:
        if not (path / "config.json").is_file():
            return False
        if (path / "pretrained_model").is_dir():
            return True
        return any(path.glob("*.safetensors")) or (path / "model.pt").is_file() or (path / "pytorch_model.bin").is_file()

    def walk(root: Path, depth: int) -> None:
        if depth > max_depth or not root.is_dir():
            return
        try:
            children = list(root.iterdir())
        except OSError:
            return
        if is_policy(root):
            key = str(root.resolve())
            if key not in seen:
                seen.add(key)
                stat = root.stat()
                found.append(
                    {
                        "name": root.name,
                        "path": str(root),
                        "mtime": int(stat.st_mtime),
                    }
                )
            return
        for child in children:
            if not child.is_dir() or child.name in skip or child.name.startswith("."):
                continue
            walk(child, depth + 1)

    for raw in roots:
        walk(Path(raw), 0)
    found.sort(key=lambda row: row.get("mtime", 0), reverse=True)
    return found


DatasetLibrary = VideoLibrary


def huggingface_home() -> Path:
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"])
    return Path.home() / ".cache" / "huggingface"


def huggingface_hub_cache() -> Path:
    if os.environ.get("HUGGINGFACE_HUB_CACHE"):
        return Path(os.environ["HUGGINGFACE_HUB_CACHE"])
    return huggingface_home() / "hub"


def lerobot_home() -> Path:
    for key in ("HF_LEROBOT_HOME", "LEROBOT_HOME"):
        if os.environ.get(key):
            return Path(os.environ[key])
    return huggingface_home() / "lerobot"


def _hub_repo_id(dirname: str) -> str:
    rest = dirname[len("datasets--") :] if dirname.startswith("datasets--") else dirname
    return rest.replace("--", "/", 1)


def _lerobot_info(path: Path) -> dict[str, Any]:
    info = _read_json(path / "meta" / "info.json")
    if not info:
        return {}
    return {
        "fps": info.get("fps"),
        "episodes": info.get("total_episodes") or info.get("total_episodes_in_set"),
        "task": (info.get("features") or {}).get("task") or info.get("task"),
        "lerobot": True,
    }


def _latest_snapshot(repo_dir: Path) -> Path | None:
    snaps = repo_dir / "snapshots"
    if not snaps.is_dir():
        return repo_dir if (repo_dir / "meta" / "info.json").is_file() else None
    children = [p for p in snaps.iterdir() if p.is_dir()]
    if not children:
        refs = repo_dir / "refs" / "main"
        if refs.is_file():
            rev = refs.read_text(encoding="utf-8").strip()
            candidate = snaps / rev
            if candidate.is_dir():
                return candidate
        return None
    children.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return children[0]


def _has_videos(path: Path) -> bool:
    videos = path / "videos"
    if not videos.is_dir():
        return False
    try:
        return any(child.suffix.lower() in {".mp4", ".avi"} or child.is_dir() for child in videos.iterdir())
    except OSError:
        return False


def _dataset_entry(repo_id: str, path: Path, source: str) -> dict[str, Any] | None:
    extra = _lerobot_info(path)
    if not extra.get("lerobot"):
        return None
    stat = path.stat() if path.exists() else None
    return {
        "id": repo_id,
        "repo_id": repo_id,
        "name": repo_id,
        "path": str(path),
        "source": source,
        "mtime": int(stat.st_mtime) if stat else 0,
        "episodes": extra.get("episodes"),
        "fps": extra.get("fps"),
        "lerobot": True,
        "playable": _has_videos(path),
    }


_SOURCE_RANK = {"local": 0, "lerobot": 1, "hub": 2}


def _better_dataset(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    if bool(candidate.get("playable")) != bool(current.get("playable")):
        return bool(candidate.get("playable"))
    cr = _SOURCE_RANK.get(str(candidate.get("source")), 9)
    pr = _SOURCE_RANK.get(str(current.get("source")), 9)
    if cr != pr:
        return cr < pr
    return int(candidate.get("mtime") or 0) >= int(current.get("mtime") or 0)


def list_hf_datasets(extra_roots: list[Path] | None = None) -> list[dict[str, Any]]:
    """Scan Hugging Face hub cache, LeRobot home, and extra local roots."""
    found: dict[str, dict[str, Any]] = {}

    def add(entry: dict[str, Any] | None) -> None:
        if not entry:
            return
        key = str(entry.get("repo_id") or entry.get("id"))
        prev = found.get(key)
        if prev is None or _better_dataset(entry, prev):
            found[key] = entry

    hub = huggingface_hub_cache()
    if hub.is_dir():
        try:
            children = list(hub.iterdir())
        except OSError:
            children = []
        for child in children:
            if not child.is_dir() or not child.name.startswith("datasets--"):
                continue
            repo_id = _hub_repo_id(child.name)
            snap = _latest_snapshot(child)
            if snap is None:
                continue
            add(_dataset_entry(repo_id, snap, "hub"))

    home = lerobot_home()
    if home.is_dir():
        try:
            top = list(home.iterdir())
        except OSError:
            top = []
        skip = {".cache", "blobs", "downloads", ".git"}
        for org in top:
            if not org.is_dir() or org.name.startswith(".") or org.name in skip:
                continue
            if (org / "meta" / "info.json").is_file():
                add(_dataset_entry(org.name, org, "lerobot"))
                continue
            try:
                nested = list(org.iterdir())
            except OSError:
                continue
            for ds in nested:
                if ds.is_dir() and (ds / "meta" / "info.json").is_file():
                    add(_dataset_entry(f"{org.name}/{ds.name}", ds, "lerobot"))

    for raw in extra_roots or []:
        root = Path(raw)
        if not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        skip = {".cache", "blobs", "downloads", ".git"}
        if (root / "meta" / "info.json").is_file():
            add(_dataset_entry(root.name, root, "local"))
        for child in children:
            if not child.is_dir() or child.name in skip or child.name.startswith("."):
                continue
            if (child / "meta" / "info.json").is_file():
                add(_dataset_entry(child.name.replace("--", "/", 1), child, "local"))
                continue
            try:
                nested = list(child.iterdir())
            except OSError:
                continue
            for ds in nested:
                if not ds.is_dir() or ds.name.startswith("."):
                    continue
                if (ds / "meta" / "info.json").is_file():
                    add(_dataset_entry(f"{child.name}/{ds.name}", ds, "local"))

    rows = list(found.values())
    rows.sort(key=lambda row: row.get("mtime", 0), reverse=True)
    return rows


def mosaic_named(images_bgr: dict[str, Any]):
    return mosaic_bgr(images_bgr)
