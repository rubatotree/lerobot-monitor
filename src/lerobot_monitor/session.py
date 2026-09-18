"""Session writer: MP4 per camera + CSV of joint/action samples.

This format is owned by the monitor so rollout/teleop recording does not
depend on LeRobotDataset being in the same process.
"""

from __future__ import annotations

import csv
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import cv2
import numpy as np

from .types import JOINT_ORDER


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


class SessionWriter:
    def __init__(
        self,
        root: Path,
        kind: str,
        fps: int,
        extra_meta: dict[str, Any] | None = None,
    ) -> None:
        self.kind = kind
        self.fps = fps
        self.session_id = f"{_utc_stamp()}_{kind}"
        self.dir = Path(root) / self.session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "videos").mkdir(exist_ok=True)

        self._csv_path = self.dir / "joints.csv"
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
        self._lock = threading.Lock()
        self._t0 = time.perf_counter()
        self.frame_index = 0
        self.episode_index = 0
        self.closed = False
        self.meta: dict[str, Any] = {
            "session_id": self.session_id,
            "kind": kind,
            "fps": fps,
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
        path = self.dir / "videos" / f"{name}.mp4"
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(path), fourcc, float(self.fps), (w, h))
        if not writer.isOpened():
            raise RuntimeError(f"cannot open video writer for {path}")
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
        with self._lock:
            self._writer.writerow(row)
            self._csv_file.flush()
            for cam, bgr in images_bgr.items():
                writer = self._ensure_video(cam, bgr)
                w, h = self._video_size[cam]
                if (bgr.shape[1], bgr.shape[0]) != (w, h):
                    bgr = cv2.resize(bgr, (w, h), interpolation=cv2.INTER_AREA)
                writer.write(bgr)
        self.frame_index += 1

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
        meta["path"] = str(path)
        sessions.append(meta)
    return sessions
