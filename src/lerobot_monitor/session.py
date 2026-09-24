"""Session / episode writers: MP4 per camera + optional merged mosaic + CSV.

Monitor-owned format so teleop / record / rollout capture does not depend on
LeRobotDataset being in the same process.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
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
    fill the timeline. Calls made before the next target slot legitimately
    return zero; a later call fills the complete elapsed gap.
    """
    rate = max(1.0, float(fps))
    desired = max(0, int(max(0.0, elapsed_s) * rate + 0.5))
    return max(0, desired - int(frames_written))


def open_video_writer(path: Path, fps: float, size: tuple[int, int], fmt: str = "mp4") -> cv2.VideoWriter:
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = (fmt or "mp4").lower().lstrip(".")
    if fmt == "avi":
        codes = ("XVID", "MJPG")
    else:
        # Browsers can play H.264 MP4, while the common mp4v fallback is often
        # downloadable but cannot be displayed in the monitor's video element.
        codes = ("avc1", "H264", "mp4v")
    last_error = ""
    for code in codes:
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*code), float(fps), size)
        if writer.isOpened():
            return writer
        writer.release()
        last_error = code
    raise RuntimeError(f"cannot open video writer for {path} (tried {last_error})")


class StreamingVideoWriter:
    """Feed FFmpeg from a bounded queue without blocking the control loop."""

    def __init__(self, path: Path, fps: float, size: tuple[int, int], threads: int) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("FFmpeg is required when streaming encoding is enabled")
        if not 1 <= threads <= 32:
            raise ValueError("encoder_threads must be between 1 and 32")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._size = size
        is_avi = path.suffix.lower() == ".avi"
        codec = "libxvid" if is_avi else "libx264"
        command = [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s:v", f"{size[0]}x{size[1]}",
            "-r", str(fps), "-i", "pipe:0", "-an", "-c:v", codec,
            "-threads", str(threads),
        ]
        if not is_avi:
            command += [
                "-preset", "ultrafast", "-crf", "22", "-vf",
                "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
            ]
        command.append(str(path))
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        if os.name == "nt":
            flags |= subprocess.BELOW_NORMAL_PRIORITY_CLASS
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=flags,
        )
        self._pending: deque[tuple[np.ndarray, int]] = deque()
        self._condition = threading.Condition()
        self._closed = False
        self._error: Exception | None = None
        self._worker = threading.Thread(target=self._encode, name=f"video-encoder-{path.stem}", daemon=True)
        self._worker.start()

    def write(self, frame: np.ndarray) -> None:
        self.write_repeated(frame, 1)

    def write_repeated(self, frame: np.ndarray, count: int) -> None:
        """Queue one frame for several timeline slots; newer frames replace stale pending images."""
        if frame.shape != (self._size[1], self._size[0], 3):
            raise ValueError("video frame shape changed during recording")
        if count <= 0:
            return
        owned_frame = np.ascontiguousarray(frame).copy()
        with self._condition:
            if self._error is not None:
                raise RuntimeError("video encoder failed") from self._error
            if self._closed:
                raise RuntimeError("video encoder input is closed")
            if len(self._pending) >= 2:
                _, stale_count = self._pending.popleft()
                next_frame, next_count = self._pending.popleft()
                self._pending.append((next_frame, stale_count + next_count))
            self._pending.append((owned_frame, count))
            self._condition.notify()

    def _encode(self) -> None:
        try:
            if self._process.stdin is None:
                raise RuntimeError("video encoder input is closed")
            while True:
                with self._condition:
                    while not self._pending and not self._closed:
                        self._condition.wait()
                    if not self._pending:
                        break
                    frame, count = self._pending.popleft()
                raw_frame = memoryview(frame).cast("B")
                for _ in range(count):
                    self._process.stdin.write(raw_frame)
        except Exception as exc:  # noqa: BLE001
            with self._condition:
                self._error = exc
                self._pending.clear()
                self._condition.notify_all()
        finally:
            if self._process.stdin is not None and not self._process.stdin.closed:
                try:
                    self._process.stdin.close()
                except OSError as exc:
                    with self._condition:
                        if self._error is None:
                            self._error = exc

    def release(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._worker.join(timeout=120)
        if self._worker.is_alive():
            self._process.kill()
            self._worker.join()
            self._process.wait()
            raise RuntimeError("video encoder did not finish in time")
        try:
            self._process.wait(timeout=120)
        except subprocess.TimeoutExpired as exc:
            self._process.kill()
            self._process.wait()
            raise RuntimeError("video encoder did not finish in time") from exc
        error = self._process.stderr.read().decode("utf-8", errors="replace") if self._process.stderr else ""
        if self._process.stderr is not None:
            self._process.stderr.close()
        if self._error is not None:
            raise RuntimeError(f"video encoder failed: {error.strip()}") from self._error
        if self._process.returncode:
            raise RuntimeError(f"video encoder failed: {error.strip()}")


class EpisodeWriter:
    """One episode: joints.csv, per-camera MP4, merged mosaic, preview.jpg."""

    def __init__(
        self,
        folder: Path,
        *,
        index: int,
        fps: int | None = None,
        action_fps: int | None = None,
        video_fps: int | None = None,
        video_format: str = "mp4",
        streaming_encoding: bool = False,
        encoder_threads: int = 2,
        merge: bool = True,
        video: bool = True,
        kind: str = "record",
        task: str = "",
    ) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.index = int(index)
        legacy_fps = int(fps) if fps is not None else None
        self.action_fps = int(action_fps if action_fps is not None else legacy_fps if legacy_fps is not None else 15)
        self.video_fps = int(
            video_fps
            if video_fps is not None
            else legacy_fps if legacy_fps is not None else self.action_fps
        )
        if self.action_fps <= 0 or self.video_fps <= 0:
            raise ValueError("action_fps and video_fps must be positive")
        self.fps = self.action_fps
        self.video_format = video_format
        self.streaming_encoding = bool(streaming_encoding)
        self.encoder_threads = int(encoder_threads)
        self.merge = merge
        self.video = bool(video)
        if self.video:
            (self.folder / "videos").mkdir(exist_ok=True)
        self.kind = kind
        self.task = str(task or "")
        self.closed = False
        self.action_frames = 0
        self.video_frames = 0
        self.requested_video_frames = 0
        self.frames = 0  # Compatibility alias for action samples.
        self._last_action_elapsed = 0.0
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
        self._videos: dict[str, cv2.VideoWriter | StreamingVideoWriter] = {}
        self._video_size: dict[str, tuple[int, int]] = {}
        self._video_frames: dict[str, int] = {}
        self._video_start_frame: dict[str, int] = {}
        self._last_video_frame: dict[str, np.ndarray] = {}
        self._t0 = time.perf_counter()
        self._lock = threading.Lock()

    def _ensure_video(self, name: str, bgr: np.ndarray) -> cv2.VideoWriter | StreamingVideoWriter:
        key = safe_cam_name(name)
        if key in self._videos:
            return self._videos[key]
        h, w = bgr.shape[:2]
        path = self.folder / "videos" / f"{key}{self._ext}"
        writer = (
            StreamingVideoWriter(path, self.video_fps, (w, h), self.encoder_threads)
            if self.streaming_encoding
            else open_video_writer(path, self.video_fps, (w, h), self.video_format)
        )
        self._videos[key] = writer
        self._video_size[key] = (w, h)
        self._video_frames[key] = 0
        return writer

    def _write_video_until(self, name: str, bgr: np.ndarray, target_frames: int) -> None:
        """Extend one stream to the episode timeline using its current frame."""
        key = safe_cam_name(name)
        writer = self._ensure_video(key, bgr)
        width, height = self._video_size[key]
        if (bgr.shape[1], bgr.shape[0]) != (width, height):
            bgr = cv2.resize(bgr, (width, height), interpolation=cv2.INTER_AREA)
        frame = np.ascontiguousarray(bgr)
        missing = max(0, int(target_frames) - self._video_frames[key])
        if isinstance(writer, StreamingVideoWriter):
            writer.write_repeated(frame, missing)
        else:
            for _ in range(missing):
                writer.write(frame)
        self._video_frames[key] += missing
        # Camera adapters commonly reuse their buffers, so retain an owned copy
        # for future dropout padding.
        self._last_video_frame[key] = frame.copy()

    def _elapsed(self, elapsed_s: float | None) -> float:
        return max(0.0, time.perf_counter() - self._t0 if elapsed_s is None else float(elapsed_s))

    def add_action(
        self,
        observation: Mapping[str, float],
        action: Mapping[str, float] | None,
        *,
        kind: str | None = None,
        frame_index: int | None = None,
        elapsed_s: float | None = None,
    ) -> None:
        """Append one real control sample; missed action deadlines are never synthesized."""
        if self.closed:
            return
        elapsed = self._elapsed(elapsed_s)
        row: dict[str, Any] = {
            "t": f"{elapsed:.4f}",
            "frame": self.action_frames if frame_index is None else frame_index,
            "episode": self.index,
            "kind": kind or self.kind,
        }
        for name in JOINT_ORDER:
            row[f"obs.{name}"] = f"{float(observation.get(name, float('nan'))):.4f}"
            act_val = (action or {}).get(name, float("nan"))
            row[f"act.{name}"] = f"{float(act_val):.4f}"
        with self._lock:
            self._writer.writerow(row)
            self._csv_file.flush()
            self.action_frames += 1
            self.frames = self.action_frames
            self._last_action_elapsed = max(self._last_action_elapsed, elapsed)

    def add_video(self, images_bgr: Mapping[str, np.ndarray], *, elapsed_s: float | None = None) -> None:
        """Advance camera streams to the elapsed-time target without rewriting history."""
        if self.closed:
            return
        if not self.video:
            return
        elapsed = self._elapsed(elapsed_s)
        target_frames = max(0, int(elapsed * self.video_fps + 0.5))
        if images_bgr:
            target_frames = max(1, target_frames)
        self.requested_video_frames = max(self.requested_video_frames, target_frames)
        with self._lock:
            labeled = {safe_cam_name(k): v for k, v in images_bgr.items() if v is not None}
            current_frames = dict(labeled)
            merged = mosaic_bgr(labeled) if self.merge else None
            if merged is not None:
                current_frames["merged"] = merged

            preview = merged if merged is not None else (next(iter(labeled.values())) if labeled else None)
            preview_path = self.folder / "preview.jpg"
            if preview is not None and not preview_path.is_file():
                cv2.imwrite(str(preview_path), preview)

            max_catchup = max(1, self.video_fps)
            for name, frame in current_frames.items():
                key = safe_cam_name(name)
                if key not in self._videos:
                    # Record a stream offset instead of synchronously cloning
                    # its first frame over the entire pre-camera history.
                    self._ensure_video(key, frame)
                    self._video_start_frame[key] = max(0, target_frames - 1)
                absolute_end = self._video_start_frame[key] + self._video_frames[key]
                bounded_end = min(target_frames, absolute_end + max_catchup)
                if bounded_end > absolute_end:
                    last_frame = self._last_video_frame.get(key)
                    if bounded_end < target_frames and last_frame is not None:
                        # This tick is still catching up: never place a fresh
                        # image into a historical slot. Drop it until the
                        # stream reaches the current target; the old frame is
                        # the only valid source for the missing interval.
                        self._write_video_until(key, last_frame, bounded_end - self._video_start_frame[key])
                    else:
                        historical_end = max(absolute_end, bounded_end - 1)
                        if last_frame is not None:
                            self._write_video_until(key, last_frame, historical_end - self._video_start_frame[key])
                        self._write_video_until(key, frame, bounded_end - self._video_start_frame[key])
                else:
                    width, height = self._video_size[key]
                    if (frame.shape[1], frame.shape[0]) != (width, height):
                        frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
                    self._last_video_frame[key] = np.ascontiguousarray(frame).copy()
            for name, last_frame in tuple(self._last_video_frame.items()):
                if name in current_frames:
                    continue
                absolute_end = self._video_start_frame[name] + self._video_frames[name]
                bounded_end = min(target_frames, absolute_end + max_catchup)
                self._write_video_until(name, last_frame, bounded_end - self._video_start_frame[name])
            self.video_frames = max(self._video_frames.values(), default=0)

    def add_frame(
        self,
        observation: Mapping[str, float],
        action: Mapping[str, float] | None,
        images_bgr: Mapping[str, np.ndarray],
        *,
        kind: str | None = None,
        frame_index: int | None = None,
    ) -> None:
        elapsed = self._elapsed(None)
        self.add_action(
            observation,
            action,
            kind=kind,
            frame_index=frame_index,
            elapsed_s=elapsed,
        )
        self.add_video(images_bgr, elapsed_s=elapsed)

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
        duration_s = max(
            self._last_action_elapsed,
            self.action_frames / self.action_fps,
            max(
                (self._video_start_frame.get(name, 0) + count) / self.video_fps
                for name, count in self._video_frames.items()
            )
            if self._video_frames
            else 0.0,
        )
        return {
            "index": self.index,
            "frames": self.action_frames,
            "action_frames": self.action_frames,
            "video_frames": self.video_frames,
            "requested_video_frames": self.requested_video_frames,
            "effective_video_frames": max(
                (self._video_start_frame.get(name, 0) + count for name, count in self._video_frames.items()),
                default=0,
            ),
            "actual_video_frames": self.video_frames,
            "per_camera_video_frames": dict(sorted(self._video_frames.items())),
            "video_start_frames": dict(sorted(self._video_start_frame.items())),
            "duration_s": round(duration_s, 4),
            "wall_s": round(duration_s, 3),
            "fps": self.action_fps,
            "action_fps": self.action_fps,
            "video_fps": self.video_fps,
            "requested_video_fps": self.video_fps,
            "encoded_video_fps": self.video_fps if self._video_frames else 0,
            "effective_encoding_fps": self.video_fps if self._video_frames else 0,
            # Compatibility alias for the container playback rate.
            "effective_video_fps": self.video_fps if self._videos or self._video_frames else 0,
            "video": self.video,
            "task": self.task,
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
        # SessionWriter is the legacy coupled API: each call historically
        # produced at least one video frame.
        copies = max(1, video_copies(elapsed, self.fps, self.frame_index))
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
