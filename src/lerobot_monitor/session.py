"""Session / episode writers: MP4 per camera + optional merged mosaic + CSV.

Monitor-owned format so teleop / record / rollout capture does not depend on
LeRobotDataset being in the same process.
"""

from __future__ import annotations

import csv
import json
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import cv2
import numpy as np

from .types import JOINT_ORDER

_CAM_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def safe_cam_name(name: str) -> str:
    text = _CAM_SAFE.sub("_", (name or "cam").strip()).strip("._-")
    return text or "cam"


def mosaic_bgr(images_bgr: Mapping[str, np.ndarray], gap: int = 4) -> np.ndarray | None:
    """Tile main-view frames into one BGR image (row-major, up to 3 columns)."""
    frames = [np.ascontiguousarray(frame) for frame in images_bgr.values() if frame is not None]
    if not frames:
        return None
    if len(frames) == 1:
        return frames[0]
    cols = 2 if len(frames) <= 4 else 3
    rows = int(math.ceil(len(frames) / cols))
    target_h = min(int(f.shape[0]) for f in frames)
    resized: list[np.ndarray] = []
    for frame in frames:
        if frame.shape[0] != target_h:
            scale = target_h / max(1, frame.shape[0])
            width = max(1, int(frame.shape[1] * scale))
            frame = cv2.resize(frame, (width, target_h), interpolation=cv2.INTER_AREA)
        resized.append(frame)
    cell_w = max(int(f.shape[1]) for f in resized)
    cell_h = target_h
    canvas_h = rows * cell_h + gap * (rows - 1)
    canvas_w = cols * cell_w + gap * (cols - 1)
    canvas = np.full((canvas_h, canvas_w, 3), 12, dtype=np.uint8)
    for i, frame in enumerate(resized):
        r, c = divmod(i, cols)
        y = r * (cell_h + gap)
        x = c * (cell_w + gap)
        canvas[y : y + frame.shape[0], x : x + frame.shape[1]] = frame
    return canvas


def video_copies(elapsed_s: float, fps: float, frames_written: int) -> int:
    """How many container frames to write so playback duration tracks wall time.

    OpenCV VideoWriter plays at the fps given at open. If we insert fewer unique
    captures than that rate, the file runs fast. Duplicate the latest image to
    fill the timeline; never drop below one frame, and cap a stall at 5s.
    """
    rate = max(1.0, float(fps))
    desired = max(int(frames_written) + 1, int(elapsed_s * rate + 0.5))
    return max(1, min(desired - int(frames_written), int(rate * 5)))


def open_video_writer(path: Path, fps: float, size: tuple[int, int], fmt: str = "mp4") -> cv2.VideoWriter:
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = (fmt or "mp4").lower().lstrip(".")
    if fmt == "avi":
        codes = ("XVID", "MJPG")
    else:
        codes = ("avc1", "H264", "mp4v")
    last_error = ""
    for code in codes:
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*code), float(fps), size)
        if writer.isOpened():
            return writer
        writer.release()
        last_error = code
    raise RuntimeError(f"cannot open video writer for {path} (tried {last_error})")


class EpisodeWriter:
    """One episode: joints.csv, per-camera MP4, merged mosaic, preview.jpg."""

    def __init__(
        self,
        folder: Path,
        *,
        index: int,
        fps: int,
        video_format: str = "mp4",
        merge: bool = True,
        kind: str = "record",
    ) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "videos").mkdir(exist_ok=True)
        self.index = int(index)
        self.fps = max(1, int(fps))
        self.video_format = video_format
        self.merge = merge
        self.kind = kind
        self.closed = False
        self.frames = 0
        self._ext = ".avi" if video_format == "avi" else ".mp4"
        self._csv_path = self.folder / "joints.csv"
        self._csv_file = self._csv_path.open("w", newline="", encoding="utf-8")
        fieldnames = (
            ["t", "frame", "episode", "kind"]
            + [f"obs.{n}" for n in JOINT_ORDER]
            + [f"act.{n}" for n in JOINT_ORDER]
        )
        self._writer = csv.DictWriter(self._csv_file, fieldnames=fieldnames)
        self._writer.writeheader()
        self._videos: dict[str, cv2.VideoWriter] = {}
        self._video_size: dict[str, tuple[int, int]] = {}
        self._t0 = time.perf_counter()
        self._lock = threading.Lock()

    def _ensure_video(self, name: str, bgr: np.ndarray) -> cv2.VideoWriter:
        key = safe_cam_name(name)
        if key in self._videos:
            return self._videos[key]
        h, w = bgr.shape[:2]
        path = self.folder / "videos" / f"{key}{self._ext}"
        writer = open_video_writer(path, self.fps, (w, h), self.video_format)
        self._videos[key] = writer
        self._video_size[key] = (w, h)
        return writer

    def add_frame(
        self,
        observation: Mapping[str, float],
        action: Mapping[str, float] | None,
        images_bgr: Mapping[str, np.ndarray],
        *,
        kind: str | None = None,
        frame_index: int | None = None,
    ) -> None:
        if self.closed:
            return
        row: dict[str, Any] = {
            "t": f"{time.perf_counter() - self._t0:.4f}",
            "frame": self.frames if frame_index is None else frame_index,
            "episode": self.index,
            "kind": kind or self.kind,
        }
        for name in JOINT_ORDER:
            row[f"obs.{name}"] = f"{float(observation.get(name, float('nan'))):.4f}"
            act_val = (action or {}).get(name, float("nan"))
            row[f"act.{name}"] = f"{float(act_val):.4f}"
        labeled = {safe_cam_name(k): v for k, v in images_bgr.items() if v is not None}
        merged = mosaic_bgr(labeled) if self.merge else None
        elapsed = time.perf_counter() - self._t0
        copies = video_copies(elapsed, self.fps, self.frames)
        with self._lock:
            self._writer.writerow(row)
            self._csv_file.flush()
            resized: dict[str, np.ndarray] = {}
            for cam, bgr in labeled.items():
                writer = self._ensure_video(cam, bgr)
                w, h = self._video_size[cam]
                if (bgr.shape[1], bgr.shape[0]) != (w, h):
                    bgr = cv2.resize(bgr, (w, h), interpolation=cv2.INTER_AREA)
                resized[cam] = bgr
            merged_frame = merged
            if merged is not None:
                writer = self._ensure_video("merged", merged)
                w, h = self._video_size["merged"]
                if (merged.shape[1], merged.shape[0]) != (w, h):
                    merged_frame = cv2.resize(merged, (w, h), interpolation=cv2.INTER_AREA)
            if self.frames == 0:
                preview = merged_frame if merged_frame is not None else (next(iter(resized.values())) if resized else None)
                if preview is not None:
                    cv2.imwrite(str(self.folder / "preview.jpg"), preview)
            for _ in range(copies):
                for cam, bgr in resized.items():
                    self._videos[cam].write(bgr)
                if merged_frame is not None:
                    self._videos["merged"].write(merged_frame)
            self.frames += copies

    def close(self) -> dict[str, Any]:
        if self.closed:
            return self.info()
        with self._lock:
            self._csv_file.close()
            for writer in self._videos.values():
                writer.release()
            self._videos.clear()
        self.closed = True
        info = self.info()
        (self.folder / "meta.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
        return info

    def info(self) -> dict[str, Any]:
        videos = []
        folder = self.folder / "videos"
        if folder.is_dir():
            videos = sorted(p.name for p in folder.iterdir() if p.is_file())
        wall_s = max(0.0, time.perf_counter() - self._t0)
        return {
            "index": self.index,
            "frames": self.frames,
            "wall_s": round(wall_s, 3),
            "fps": self.fps,
            "dir": self.folder.name,
            "videos": videos,
            "preview": "preview.jpg" if (self.folder / "preview.jpg").is_file() else None,
        }


class SessionWriter:
    """Backward-compatible single-folder writer used by older tests and capture."""

    def __init__(
        self,
        root: Path,
        kind: str,
        fps: int,
        extra_meta: dict[str, Any] | None = None,
        *,
        video_format: str = "mp4",
        merge: bool = True,
        session_id: str | None = None,
        resume: bool = False,
    ) -> None:
        self.kind = kind
        self.fps = fps
        self.session_id = session_id or f"{_utc_stamp()}_{kind}"
        self.dir = Path(root) / self.session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "videos").mkdir(exist_ok=True)
        self.merge = merge
        self.video_format = video_format
        self._ext = ".avi" if video_format == "avi" else ".mp4"

        self._csv_path = self.dir / "joints.csv"
        mode = "a" if resume and self._csv_path.is_file() else "w"
        self._csv_file = self._csv_path.open(mode, newline="", encoding="utf-8")
        fieldnames = (
            ["t", "frame", "episode", "kind"]
            + [f"obs.{n}" for n in JOINT_ORDER]
            + [f"act.{n}" for n in JOINT_ORDER]
        )
        self._writer = csv.DictWriter(self._csv_file, fieldnames=fieldnames)
        if mode == "w":
            self._writer.writeheader()

        self._videos: dict[str, cv2.VideoWriter] = {}
        self._video_size: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()
        self._t0 = time.perf_counter()
        self.frame_index = 0
        self.episode_index = 0
        self.closed = False
        self.meta: dict[str, Any] = {
            "session_id": self.session_id,
            "kind": kind,
            "fps": fps,
            "format": video_format,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "joints": list(JOINT_ORDER),
            **(extra_meta or {}),
        }
        self._write_meta()

    def _write_meta(self) -> None:
        (self.dir / "meta.json").write_text(json.dumps(self.meta, indent=2) + "\n", encoding="utf-8")

    def _ensure_video(self, name: str, bgr: np.ndarray) -> cv2.VideoWriter:
        if name in self._videos:
            return self._videos[name]
        h, w = bgr.shape[:2]
        path = self.dir / "videos" / f"{name}{self._ext}"
        writer = open_video_writer(path, self.fps, (w, h), self.video_format)
        self._videos[name] = writer
        self._video_size[name] = (w, h)
        return writer

    def add_frame(
        self,
        observation: Mapping[str, float],
        action: Mapping[str, float] | None,
        images_bgr: Mapping[str, np.ndarray],
        *,
        episode_index: int | None = None,
        kind: str | None = None,
    ) -> None:
        if self.closed:
            return
        if episode_index is not None:
            self.episode_index = episode_index
        row: dict[str, Any] = {
            "t": f"{time.perf_counter() - self._t0:.4f}",
            "frame": self.frame_index,
            "episode": self.episode_index,
            "kind": kind or self.kind,
        }
        for name in JOINT_ORDER:
            row[f"obs.{name}"] = f"{float(observation.get(name, float('nan'))):.4f}"
            act_val = (action or {}).get(name, float("nan"))
            row[f"act.{name}"] = f"{float(act_val):.4f}"
        labeled = {safe_cam_name(k): v for k, v in images_bgr.items() if v is not None}
        merged = mosaic_bgr(labeled) if self.merge else None
        elapsed = time.perf_counter() - self._t0
        copies = video_copies(elapsed, self.fps, self.frame_index)
        with self._lock:
            self._writer.writerow(row)
            self._csv_file.flush()
            resized: dict[str, np.ndarray] = {}
            for cam, bgr in labeled.items():
                self._ensure_video(cam, bgr)
                w, h = self._video_size[cam]
                if (bgr.shape[1], bgr.shape[0]) != (w, h):
                    bgr = cv2.resize(bgr, (w, h), interpolation=cv2.INTER_AREA)
                resized[cam] = bgr
            merged_frame = merged
            if merged is not None:
                self._ensure_video("merged", merged)
                w, h = self._video_size["merged"]
                if (merged.shape[1], merged.shape[0]) != (w, h):
                    merged_frame = cv2.resize(merged, (w, h), interpolation=cv2.INTER_AREA)
            for _ in range(copies):
                for cam, bgr in resized.items():
                    self._videos[cam].write(bgr)
                if merged_frame is not None:
                    self._videos["merged"].write(merged_frame)
        self.frame_index += copies

    def close(self) -> Path:
        if self.closed:
            return self.dir
        with self._lock:
            self._csv_file.close()
            for writer in self._videos.values():
                writer.release()
            self._videos.clear()
        self.meta["frames"] = self.frame_index
        self.meta["episodes"] = self.episode_index + 1 if self.frame_index else 0
        self.meta["closed_utc"] = datetime.now(timezone.utc).isoformat()
        self._write_meta()
        self.closed = True
        return self.dir


def list_sessions(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    sessions: list[dict[str, Any]] = []
    for path in sorted(root.iterdir(), reverse=True):
        meta_path = path / "meta.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(meta, dict):
            continue
        meta["path"] = str(path)
        sessions.append(meta)
    return sessions
