"""Local video sessions, Hugging Face dataset cache, and model scan."""

from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp_name).replace(path)
    finally:
        if tmp_name is not None:
            Path(tmp_name).unlink(missing_ok=True)


def episode_dir(root: Path, index: int) -> Path:
    return root / "episodes" / f"{int(index):06d}"


def _episode_duration_s(folder: Path, info: dict[str, Any], root_meta: dict[str, Any]) -> float:
    saved = info.get("duration_s")
    try:
        duration = float(saved)
        if math.isfinite(duration) and duration > 0:
            return duration
    except (TypeError, ValueError):
        pass
    joints = folder / "joints.csv"
    if joints.is_file():
        last_t: float | None = None
        try:
            with joints.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    try:
                        value = float(row.get("t") or "")
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(value):
                        last_t = value
        except OSError:
            last_t = None
        if last_t is not None:
            return round(last_t, 4)
    frames = int(info.get("action_frames") or info.get("frames") or 0)
    fps = float(
        info.get("action_fps")
        or info.get("fps")
        or root_meta.get("action_fps")
        or root_meta.get("fps")
        or 0
    )
    return round(frames / fps, 4) if frames > 0 and fps > 0 else 0.0


class DatasetRecorder:
    """Append frames to a local dataset, rotating files at episode boundaries."""

    def __init__(
        self,
        root: Path,
        *,
        fps: int | None = None,
        action_fps: int | None = None,
        video_fps: int | None = None,
        kind: str,
        extra_meta: dict[str, Any] | None = None,
        resume: bool = False,
        video_format: str | None = None,
        merge: bool = True,
        video: bool | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        meta_path = self.root / "meta.json"
        meta = _read_json(meta_path) if resume and meta_path.is_file() else {}
        legacy_fps = int(fps) if fps is not None else None
        requested_action_fps = int(
            action_fps
            if action_fps is not None
            else legacy_fps
            if legacy_fps is not None
            else meta.get("action_fps") or meta.get("fps") or 15
        )
        requested_video_fps = int(
            video_fps
            if video_fps is not None
            else legacy_fps
            if legacy_fps is not None
            else meta.get("video_fps") or meta.get("fps") or requested_action_fps
        )
        self.action_fps = requested_action_fps
        self.video_fps = requested_video_fps
        if self.action_fps <= 0 or self.video_fps <= 0:
            raise ValueError("action_fps and video_fps must be positive")
        self.fps = self.action_fps
        self.kind = kind
        self.task = str((extra_meta or {}).get("task") or "")
        explicit_video_format = video_format is not None
        stored_video_format = str(meta.get("format") or "")
        self.video_format = str(video_format or stored_video_format or "mp4")
        self.merge = merge
        self.dataset_id = self.root.name
        self.session_id = self.dataset_id
        self.dir = self.root
        self.closed = False
        self.action_frames = 0
        self.video_frames = 0
        self.frame_index = 0  # Compatibility alias for action samples.
        self.episode_index = 0
        self._lock = threading.Lock()
        self._on_close = on_close
        self._episode: EpisodeWriter | None = None
        if resume and meta:
            stored_action_fps = int(meta.get("action_fps") or meta.get("fps") or self.action_fps)
            stored_video_fps = int(meta.get("video_fps") or meta.get("fps") or self.video_fps)
            if stored_action_fps != self.action_fps or stored_video_fps != self.video_fps:
                raise ValueError(
                    "resume frame-rate mismatch: "
                    f"dataset uses action_fps={stored_action_fps}, video_fps={stored_video_fps}; "
                    f"requested action_fps={self.action_fps}, video_fps={self.video_fps}"
                )
            if explicit_video_format and stored_video_format and stored_video_format != self.video_format:
                raise ValueError(
                    f"resume format mismatch: dataset uses {stored_video_format}; requested {self.video_format}"
                )
        configured_video = (extra_meta or {}).get("video")
        if video is None:
            configured_video = meta.get("video", True) if configured_video is None else configured_video
            video = bool(configured_video)
        self.video = bool(video)
        episodes = list(meta.get("episodes") or [])
        if resume:
            recorded_indices = [int(e.get("index", i)) for i, e in enumerate(episodes)]
            episodes_root = self.root / "episodes"
            if episodes_root.is_dir():
                for child in episodes_root.iterdir():
                    if child.is_dir() and child.name.isdigit():
                        recorded_indices.append(int(child.name))
            if recorded_indices:
                self.episode_index = max(recorded_indices) + 1
            self.action_frames = int(meta.get("action_frames") or meta.get("frames") or 0)
            self.video_frames = int(
                meta.get("video_frames")
                or sum(int(episode.get("video_frames") or episode.get("frames") or 0) for episode in episodes)
            )
            self.frame_index = self.action_frames
        self.meta: dict[str, Any] = {
            "id": self.dataset_id,
            "kind": kind,
            "fps": self.action_fps,
            "action_fps": self.action_fps,
            "video_fps": self.video_fps,
            "format": self.video_format,
            "created_utc": meta.get("created_utc") or datetime.now(timezone.utc).isoformat(),
            "episodes": episodes,
            "frames": self.action_frames,
            "action_frames": self.action_frames,
            "video_frames": self.video_frames,
            **(extra_meta or {}),
        }
        self.meta["video"] = self.video
        if not resume:
            self.meta["episodes"] = []
            self.episode_index = 0
            self.action_frames = 0
            self.video_frames = 0
            self.frame_index = 0
        self._write_meta()

    def _write_meta(self) -> None:
        for field in ("camera_samples", "camera_sample_durations_s", "actual_camera_fps"):
            self.meta.pop(field, None)
        self.frame_index = self.action_frames
        self.meta["fps"] = self.action_fps
        self.meta["frames"] = self.action_frames
        self.meta["action_fps"] = self.action_fps
        self.meta["video_fps"] = self.video_fps
        self.meta["action_frames"] = self.action_frames
        self.meta["video_frames"] = self.video_frames
        per_camera: dict[str, int] = {}
        requested_video_frames = 0
        effective_video_frames = 0
        for episode in self.meta.get("episodes") or []:
            for field in ("camera_samples", "camera_sample_durations_s", "actual_camera_fps"):
                episode.pop(field, None)
            requested_video_frames += int(episode.get("requested_video_frames") or 0)
            effective_video_frames += int(episode.get("effective_video_frames") or 0)
            for name, count in (episode.get("per_camera_video_frames") or {}).items():
                per_camera[str(name)] = per_camera.get(str(name), 0) + int(count)
        self.meta["requested_video_frames"] = requested_video_frames
        self.meta["effective_video_frames"] = effective_video_frames
        self.meta["actual_video_frames"] = self.video_frames
        self.meta["per_camera_video_frames"] = dict(sorted(per_camera.items()))
        self.meta["requested_action_fps"] = self.action_fps
        self.meta["requested_video_fps"] = self.video_fps
        self.meta["effective_action_fps"] = self.action_fps
        self.meta["effective_video_fps"] = self.video_fps if per_camera else 0
        self.meta["encoded_video_fps"] = self.video_fps if per_camera else 0
        self.meta["effective_encoding_fps"] = self.video_fps if per_camera else 0
        self.meta["duration_s"] = round(
            sum(float(episode.get("duration_s") or 0.0) for episode in self.meta.get("episodes") or []),
            4,
        )
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
            action_fps=self.action_fps,
            video_fps=self.video_fps,
            video_format=self.video_format,
            merge=self.merge,
            video=self.video,
            kind=self.kind,
            task=self.task,
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
            previous_video_frames = writer.video_frames
            writer.add_frame(observation, action, images_bgr, kind=kind or self.kind, frame_index=self.frame_index)
            self.action_frames += 1
            self.video_frames += writer.video_frames - previous_video_frames
            self.frame_index = self.action_frames

    def add_action(
        self,
        observation: dict[str, float],
        action: dict[str, float] | None,
        *,
        episode_index: int | None = None,
        kind: str | None = None,
        elapsed_s: float | None = None,
    ) -> None:
        if self.closed:
            return
        index = self.episode_index if episode_index is None else int(episode_index)
        with self._lock:
            writer = self._ensure_episode(index)
            writer.add_action(
                observation,
                action,
                kind=kind or self.kind,
                frame_index=self.action_frames,
                elapsed_s=elapsed_s,
            )
            self.action_frames += 1
            self.frame_index = self.action_frames

    def add_video(
        self,
        images_bgr: dict[str, Any],
        *,
        episode_index: int | None = None,
        elapsed_s: float | None = None,
    ) -> None:
        if self.closed:
            return
        index = self.episode_index if episode_index is None else int(episode_index)
        with self._lock:
            writer = self._ensure_episode(index)
            previous_video_frames = writer.video_frames
            writer.add_video(images_bgr, elapsed_s=elapsed_s)
            self.video_frames += writer.video_frames - previous_video_frames

    def finish_episode(self, index: int | None = None) -> dict[str, Any] | None:
        """Commit the active episode before a reset or explicit transition."""
        if self.closed:
            return None
        with self._lock:
            if self._episode is None:
                return None
            if index is not None and self._episode.index != int(index):
                raise ValueError(f"active episode is {self._episode.index}, not {int(index)}")
            info = self._episode.close()
            self._remember_episode(info)
            self._episode = None
            return info

    def close(self) -> Path:
        if self.closed:
            return self.root
        try:
            with self._lock:
                if self.closed:
                    return self.root
                if self._episode is not None:
                    info = self._episode.close()
                    self._remember_episode(info)
                    self._episode = None
                self.meta["frames"] = self.action_frames
                self.meta["closed_utc"] = datetime.now(timezone.utc).isoformat()
                self._write_meta()
        finally:
            self.closed = True
            on_close, self._on_close = self._on_close, None
            if on_close is not None:
                on_close()
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
        action_fps: int | None = None,
        video_fps: int | None = None,
        task: str = "",
        repo_id: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved_action_fps = int(action_fps if action_fps is not None else fps)
        resolved_video_fps = int(video_fps if video_fps is not None else fps)
        if resolved_action_fps <= 0 or resolved_video_fps <= 0:
            raise ValueError("action_fps and video_fps must be positive")
        self.root.mkdir(parents=True, exist_ok=True)
        base = slugify(name or repo_id or task or "dataset")
        base_id = f"{base}_{_utc_stamp()}"
        suffix = 1
        while True:
            dataset_id = base_id if suffix == 1 else f"{base_id}_{suffix}"
            path = self.root / dataset_id
            try:
                path.mkdir(exist_ok=False)
                break
            except FileExistsError:
                suffix += 1
        meta = {
            "id": dataset_id,
            "name": name or base,
            "task": task,
            "repo_id": repo_id,
            "fps": resolved_action_fps,
            "action_fps": resolved_action_fps,
            "video_fps": resolved_video_fps,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "episodes": [],
            "frames": 0,
            "action_frames": 0,
            "video_frames": 0,
            "duration_s": 0.0,
            **(extra or {}),
        }
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
        duration = round(sum(float(episode.get("duration_s") or 0.0) for episode in meta["episodes"]), 4)
        if meta.get("duration_s") != duration:
            meta["duration_s"] = duration
            _write_json(meta_path, meta)
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
                duration = _episode_duration_s(child, row, meta or {})
                task = str(row.get("task") or (meta or {}).get("task") or "")
                changed = row.get("duration_s") != duration or row.get("task") != task
                if changed:
                    row["duration_s"] = duration
                    row["task"] = task
                    episode_meta = child / "meta.json"
                    saved = _read_json(episode_meta)
                    saved.update(row)
                    _write_json(episode_meta, saved)
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
        with self._lock:
            shutil.rmtree(path)

    def trim_to_first_episode(self, dataset_id: str) -> dict[str, Any]:
        """Keep only the lowest-numbered episode and reindex it to episode 0."""
        path = self._dataset_dir(dataset_id)
        if not (path / "meta.json").is_file():
            raise FileNotFoundError(dataset_id)
        with self._lock:
            indices = [int(episode["index"]) for episode in self._episodes(path)]
            if len(indices) <= 1:
                return self.get(dataset_id)
            keep = min(indices)
            for index in indices:
                if index == keep:
                    continue
                shutil.rmtree(episode_dir(path, index), ignore_errors=True)
            return self._reindex(path)

    def trim_all_to_first_episode(self) -> list[dict[str, Any]]:
        trimmed: list[dict[str, Any]] = []
        for row in self.list():
            dataset_id = str(row.get("id") or "")
            indices = sorted(int(episode.get("index", 0)) for episode in row.get("episodes") or [])
            if len(indices) <= 1:
                continue
            result = self.trim_to_first_episode(dataset_id)
            result["trimmed_from_episode"] = indices[0]
            trimmed.append(result)
        return trimmed

    def duplicate(self, dataset_id: str, *, name: str | None = None) -> dict[str, Any]:
        source = self._dataset_dir(dataset_id)
        if not source.is_dir():
            raise FileNotFoundError(dataset_id)
        source_meta = _read_json(source / "meta.json")
        duplicate_name = str(name if name is not None else f"{source_meta.get('name') or dataset_id} copy")
        base = slugify(duplicate_name or dataset_id)
        base_id = f"{base}_{_utc_stamp()}"
        with self._lock:
            suffix = 1
            while True:
                duplicate_id = base_id if suffix == 1 else f"{base_id}_{suffix}"
                destination = self.root / duplicate_id
                try:
                    destination.mkdir(exist_ok=False)
                    break
                except FileExistsError:
                    suffix += 1
            try:
                shutil.copytree(source, destination, dirs_exist_ok=True)
                meta = _read_json(destination / "meta.json")
                now = datetime.now(timezone.utc).isoformat()
                meta["id"] = duplicate_id
                meta["name"] = duplicate_name
                meta["created_utc"] = now
                meta["updated_utc"] = now
                _write_json(destination / "meta.json", meta)
            except BaseException:
                shutil.rmtree(destination, ignore_errors=True)
                raise
        return self.get(duplicate_id)

    def delete_episode(self, dataset_id: str, index: int) -> dict[str, Any]:
        path = self._dataset_dir(dataset_id)
        target = episode_dir(path, index)
        if not target.exists():
            raise FileNotFoundError(f"episode {index}")
        with self._lock:
            current = [int(e["index"]) for e in self._episodes(path)]
            shutil.rmtree(target)
            mapping = {old_index: new_index for new_index, old_index in enumerate(i for i in current if i != index)}
            result = self._reindex(path)
        result["episode_index_map"] = {str(old): new for old, new in mapping.items()}
        return result

    def reorder(self, dataset_id: str, order: list[int]) -> dict[str, Any]:
        path = self._dataset_dir(dataset_id)
        current = [int(e["index"]) for e in self._episodes(path)]
        if sorted(order) != sorted(current):
            raise ValueError("order must list every episode index exactly once")
        with self._lock:
            tmp_root = path / ".reorder_tmp"
            if tmp_root.exists():
                shutil.rmtree(tmp_root)
            tmp_root.mkdir()
            mapping: dict[int, int] = {}
            for new_index, old_index in enumerate(order):
                src = episode_dir(path, old_index)
                dst = tmp_root / f"{new_index:06d}"
                if src.exists():
                    shutil.move(str(src), str(dst))
                mapping[old_index] = new_index
            ep_root = path / "episodes"
            if ep_root.exists():
                shutil.rmtree(ep_root)
            shutil.move(str(tmp_root), str(ep_root))
            result = self._reindex(path)
        result["episode_index_map"] = {str(old): new for old, new in mapping.items()}
        return result

    def _reindex(self, path: Path) -> dict[str, Any]:
        meta = _read_json(path / "meta.json")
        for field in ("camera_samples", "camera_sample_durations_s", "actual_camera_fps"):
            meta.pop(field, None)
        episodes: list[dict[str, Any]] = []
        ep_root = path / "episodes"
        if ep_root.is_dir():
            children = sorted([p for p in ep_root.iterdir() if p.is_dir()])
            for new_index, child in enumerate(children):
                dest = episode_dir(path, new_index)
                if child != dest:
                    child.rename(dest)
                episode_meta_path = dest / "meta.json"
                info = _read_json(episode_meta_path)
                for field in ("camera_samples", "camera_sample_durations_s", "actual_camera_fps"):
                    info.pop(field, None)
                info.update({"index": new_index, "dir": dest.name})
                preview = dest / "preview.jpg"
                if preview.is_file():
                    info["preview"] = preview.name
                videos = sorted((dest / "videos").glob("*")) if (dest / "videos").is_dir() else []
                info["videos"] = [v.name for v in videos]
                info["duration_s"] = _episode_duration_s(dest, info, meta)
                episodes.append(info)
                if episode_meta_path.is_file():
                    _write_json(episode_meta_path, info)
        meta["episodes"] = episodes
        action_frames = sum(int(episode.get("action_frames") or episode.get("frames") or 0) for episode in episodes)
        video_frames = sum(int(episode.get("video_frames") or 0) for episode in episodes)
        requested_video_frames = sum(int(episode.get("requested_video_frames") or 0) for episode in episodes)
        effective_video_frames = sum(int(episode.get("effective_video_frames") or 0) for episode in episodes)
        per_camera: dict[str, int] = {}
        for episode in episodes:
            for name, count in (episode.get("per_camera_video_frames") or {}).items():
                per_camera[str(name)] = per_camera.get(str(name), 0) + int(count)
        meta["frames"] = action_frames
        meta["action_frames"] = action_frames
        meta["video_frames"] = video_frames
        meta["actual_video_frames"] = video_frames
        meta["requested_video_frames"] = requested_video_frames
        meta["effective_video_frames"] = effective_video_frames
        meta["per_camera_video_frames"] = dict(sorted(per_camera.items()))
        meta["duration_s"] = round(sum(float(episode.get("duration_s") or 0.0) for episode in episodes), 4)
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


_MODEL_SKIP = {".git", ".venv", "node_modules", "__pycache__", ".uv", "wandb", "blobs", "downloads", ".cache"}


def policy_config(path: Path) -> dict[str, Any]:
    """LeRobot policy configs declare the robot interface; generic HF models do not."""
    config = _read_json(path / "config.json")
    if not config.get("input_features") or not config.get("output_features"):
        return {}
    return config


def _has_policy_weights(path: Path) -> bool:
    """Ignore processor safetensors and require the actual model body."""
    pretrained_model = path / "pretrained_model"
    if pretrained_model.is_dir():
        return _has_policy_weights(pretrained_model)
    if (
        (path / "model.safetensors").is_file()
        or (path / "adapter_model.safetensors").is_file()
        or (path / "model.pt").is_file()
        or (path / "pytorch_model.bin").is_file()
    ):
        return True
    for index_name in ("model.safetensors.index.json", "pytorch_model.bin.index.json"):
        weight_map = _read_json(path / index_name).get("weight_map")
        if not isinstance(weight_map, dict) or not weight_map:
            continue
        if all((path / str(filename)).is_file() for filename in weight_map.values()):
            return True
    return False


def is_policy_dir(path: Path) -> bool:
    """A LeRobot / HF policy folder: config.json plus weights next to it."""
    if not (path / "config.json").is_file():
        return False
    return _has_policy_weights(path) and bool(policy_config(path))


def _model_entry(
    path: Path,
    *,
    name: str,
    source: str,
    repo_id: str = "",
    policy_type: str = "",
) -> dict[str, Any]:
    stat = path.stat() if path.exists() else None
    return {
        "id": repo_id or name,
        "name": name,
        "repo_id": repo_id,
        "policy_type": policy_type,
        "path": str(path),
        "source": source,
        "mtime": int(stat.st_mtime) if stat else 0,
    }


def _scan_policy_roots(root: Path, *, max_depth: int, source: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(path: Path, depth: int) -> None:
        if depth > max_depth or not path.is_dir():
            return
        if is_policy_dir(path):
            # LeRobot training saves checkpoints as `<run>/<step>/pretrained_model`,
            # so the step folder reads better than the generic leaf name.
            name = path.parent.name if path.name == "pretrained_model" and path.parent.name else path.name
            found.append(
                _model_entry(path, name=name, source=source, policy_type=str(policy_config(path).get("type") or ""))
            )
            return
        try:
            children = list(path.iterdir())
        except OSError:
            return
        for child in children:
            if not child.is_dir() or child.name in _MODEL_SKIP or child.name.startswith("."):
                continue
            walk(child, depth + 1)

    walk(root, 0)
    return found


def _model_repo_id(dirname: str) -> str:
    rest = dirname[len("models--") :] if dirname.startswith("models--") else dirname
    return rest.replace("--", "/", 1)


def _latest_snapshot_dir(repo_dir: Path) -> Path | None:
    snaps = repo_dir / "snapshots"
    if not snaps.is_dir():
        return repo_dir if repo_dir.is_dir() else None
    try:
        children = [p for p in snaps.iterdir() if p.is_dir()]
    except OSError:
        return None
    if not children:
        return None
    children.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return children[0]


def _scan_hub_models() -> list[dict[str, Any]]:
    """One entry per cached `models--org--name` repo, pointing at its newest snapshot."""
    hub = huggingface_hub_cache()
    if not hub.is_dir():
        return []
    try:
        children = list(hub.iterdir())
    except OSError:
        return []
    found: list[dict[str, Any]] = []
    for child in children:
        if not child.is_dir() or not child.name.startswith("models--"):
            continue
        snapshot = _latest_snapshot_dir(child)
        if snapshot is None or not is_policy_dir(snapshot):
            continue
        repo_id = _model_repo_id(child.name)
        found.append(
            _model_entry(
                snapshot,
                name=repo_id,
                source="hub",
                repo_id=repo_id,
                policy_type=str(policy_config(snapshot).get("type") or ""),
            )
        )
    return found


def list_local_models(roots: list[Path], *, max_depth: int = 6) -> list[dict[str, Any]]:
    """Scan the HF hub cache, `HF_LEROBOT_HOME`, and explicit roots for policies."""
    found: dict[str, dict[str, Any]] = {}

    def add(entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            key = str(Path(entry["path"]).resolve())
            prev = found.get(key)
            if prev is None or int(entry["mtime"]) >= int(prev["mtime"]):
                found[key] = entry

    add(_scan_hub_models())
    home = lerobot_home()
    if home.is_dir():
        # Dataset cache lives under the same root, so keep this walk shallow.
        add(_scan_policy_roots(home, max_depth=3, source="lerobot"))
    for raw in roots:
        add(_scan_policy_roots(Path(raw), max_depth=max_depth, source="local"))

    rows = list(found.values())
    rows.sort(key=lambda row: row.get("mtime", 0), reverse=True)
    return rows


DatasetLibrary = VideoLibrary


def _metadata_time(row: dict[str, Any]) -> str:
    value = row.get("updated_utc") or row.get("created_utc")
    if isinstance(value, str) and value.strip():
        return value.strip()
    try:
        stamp = int(row.get("mtime") or 0)
    except (TypeError, ValueError):
        stamp = 0
    if stamp <= 0:
        return ""
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def library_metadata(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    """Structured display metadata shared by every Library resource."""
    metadata: dict[str, Any] = {
        "saved_at": _metadata_time(row),
        "source": str(row.get("source") or ""),
        "path": str(row.get("path") or ""),
        "repo_id": str(row.get("repo_id") or ""),
    }
    if kind == "model":
        metadata.update(
            {
                "policy_type": str(row.get("policy_type") or ""),
                "revision": str(row.get("revision") or ""),
                "weights": "missing" if row.get("missing") else "available",
            }
        )
    elif kind == "dataset":
        metadata.update(
            {
                "episodes": row.get("episodes"),
                "fps": row.get("fps"),
                "task": str(row.get("task") or ""),
                "robot_type": str(row.get("robot_type") or ""),
            }
        )
    elif kind == "video":
        metadata.update(
            {
                "episodes": len(row.get("episodes") or []),
                "fps": row.get("action_fps") or row.get("fps"),
                "duration_s": row.get("duration_s"),
                "task": str(row.get("task") or ""),
            }
        )
    elif kind == "snapshot":
        metadata.update(
            {
                "origin": str(row.get("origin") or ""),
                "cameras": len(row.get("cameras") or []),
                "task": str(row.get("task") or ""),
                "joints": len(row.get("joints") or {}),
            }
        )
    return {key: value for key, value in metadata.items() if value not in (None, "")}


def default_library_note(kind: str, row: dict[str, Any]) -> str:
    """Build the concise, editable metadata note used when a resource is first seen."""
    source = str(row.get("source") or "local")
    if kind == "model":
        policy_type = str(row.get("policy_type") or "policy")
        remote = str(row.get("repo_id") or row.get("path") or row.get("name") or "")
        return " · ".join(part for part in (policy_type, source, remote) if part)
    if kind == "dataset":
        episodes = row.get("episodes")
        episode_label = f"{int(episodes)} episodes" if episodes is not None else "unknown episodes"
        fps = row.get("fps")
        fps_label = f"{fps} fps" if fps else "unknown fps"
        return " · ".join((episode_label, fps_label, source))
    if kind == "video":
        episodes = len(row.get("episodes") or [])
        duration = float(row.get("duration_s") or 0.0)
        fps = row.get("action_fps") or row.get("fps")
        parts = [f"{episodes} episodes", f"{duration:.1f}s"]
        if fps:
            parts.append(f"{fps} fps")
        return " · ".join(parts)
    return ""


def huggingface_home() -> Path:
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"])
    return Path.home() / ".cache" / "huggingface"


def huggingface_hub_cache() -> Path:
    if os.environ.get("HUGGINGFACE_HUB_CACHE"):
        return Path(os.environ["HUGGINGFACE_HUB_CACHE"])
    return huggingface_home() / "hub"


def hub_cache_repo_dir(path: str | Path | None, repo_id: str = "", *, kind: str = "dataset") -> Path | None:
    """Hub cache folder that owns one dataset or model snapshot path.

    Deleting only a Hub snapshot leaves its blobs and refs
    behind, so library deletion removes the owning cache folder instead.
    """
    if not path:
        return None
    if kind not in {"dataset", "model"}:
        raise ValueError(f"unsupported Hub cache kind: {kind}")
    try:
        target = Path(path).expanduser().resolve()
    except OSError:
        return None
    cache = huggingface_hub_cache().resolve()
    for candidate in (target, *target.parents):
        if candidate.parent != cache:
            continue
        prefix = f"{kind}s--"
        if candidate.name.startswith(prefix) and (
            not repo_id or candidate.name == f"{prefix}{repo_id.replace('/', '--')}"
        ):
            return candidate
    return None


def cached_hub_snapshot(repo_id: str, revision: str = "") -> str | None:
    """Return a local HF snapshot path without calling the Hub."""
    repo_id = str(repo_id or "").strip()
    if not repo_id:
        return None
    repo_dir = huggingface_hub_cache() / f"models--{repo_id.replace('/', '--')}"
    snapshots = repo_dir / "snapshots"
    if not snapshots.is_dir():
        return None
    if revision:
        candidate = snapshots / revision
        return str(candidate.resolve()) if candidate.is_dir() else None
    try:
        children = [path for path in snapshots.iterdir() if path.is_dir()]
    except OSError:
        return None
    if not children:
        return None
    children.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return str(children[0].resolve())


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
    tasks: list[str] = []
    tasks_path = path / "meta" / "tasks.jsonl"
    if tasks_path.is_file():
        try:
            with tasks_path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle):
                    if line_number >= 100:
                        break
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    value = row.get("task") or row.get("name") or row.get("description")
                    if isinstance(value, str) and value.strip():
                        tasks.append(value.strip()[:500])
        except OSError:
            pass

    def text(key: str) -> str:
        value = info.get(key)
        return value.strip()[:2000] if isinstance(value, str) else ""

    task = text("task") or (tasks[0] if tasks else "")
    episodes = info.get("total_episodes")
    if episodes is None:
        episodes = info.get("total_episodes_in_set")
    return {
        "fps": info.get("fps"),
        "episodes": episodes,
        "title": text("title"),
        "subtitle": text("subtitle"),
        "description": text("description"),
        "task": task,
        "tasks": tasks,
        "robot_type": text("robot_type"),
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


def _has_series(path: Path) -> bool:
    data = path / "data"
    if not data.is_dir():
        return False
    try:
        return next(data.rglob("*.parquet"), None) is not None
    except OSError:
        return False


def _dataset_entry(repo_id: str, path: Path, source: str) -> dict[str, Any] | None:
    extra = _lerobot_info(path)
    if not extra.get("lerobot"):
        return None
    stat = path.stat() if path.exists() else None
    has_video = _has_videos(path)
    previewable = has_video or _has_series(path)
    return {
        "id": repo_id,
        "repo_id": repo_id,
        "name": repo_id,
        "title": extra.get("title") or repo_id,
        "subtitle": extra.get("subtitle") or extra.get("task") or "",
        "description": extra.get("description") or "",
        "task": extra.get("task") or "",
        "tasks": extra.get("tasks") or [],
        "path": str(path),
        "source": source,
        "mtime": int(stat.st_mtime) if stat else 0,
        "episodes": extra.get("episodes"),
        "fps": extra.get("fps"),
        "robot_type": extra.get("robot_type"),
        "lerobot": True,
        "has_video": has_video,
        "previewable": previewable,
        # Retain the public compatibility field while allowing series-only
        # datasets to open in the episode viewer.
        "playable": previewable,
    }


_SOURCE_RANK = {"local": 0, "lerobot": 1, "hub": 2}


def _better_dataset(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    if bool(candidate.get("has_video")) != bool(current.get("has_video")):
        return bool(candidate.get("has_video"))
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


dataset_entry = _dataset_entry
