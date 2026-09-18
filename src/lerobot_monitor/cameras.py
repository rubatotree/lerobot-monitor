"""Independent camera capture. Does not go through the robot object.

Each camera has its own thread so a dead stream cannot stall the control loop
or the other cameras. Latest JPEG + BGR frame are published under a lock.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

import cv2
import numpy as np

from .config import CameraConfig


class CameraStream:
    def __init__(self, name: str, config: CameraConfig) -> None:
        self.name = name
        self.config = config
        self.connected = False
        self.error: str | None = None
        self.capture_fps = 0.0
        self.frame_id = 0
        self.width = config.width
        self.height = config.height

        self._jpeg = b""
        self._bgr: np.ndarray | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"cam-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def latest_jpeg(self) -> bytes:
        with self._lock:
            return self._jpeg

    def latest_bgr(self) -> np.ndarray | None:
        with self._lock:
            return None if self._bgr is None else self._bgr.copy()

    def latest_rgb(self) -> np.ndarray | None:
        bgr = self.latest_bgr()
        if bgr is None:
            return None
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "connected": self.connected,
            "error": self.error,
            "fps": round(self.capture_fps, 1),
            "width": self.width,
            "height": self.height,
            "source": str(self.config.source),
            "frame_id": self.frame_id,
        }

    def _open(self) -> cv2.VideoCapture:
        source = self.config.source
        if isinstance(source, int):
            if sys.platform == "win32":
                return cv2.VideoCapture(source, cv2.CAP_DSHOW)
            return cv2.VideoCapture(source)
        return cv2.VideoCapture(str(source))

    def _loop(self) -> None:
        while not self._stop.is_set():
            cap = self._open()
            if not cap.isOpened():
                self.connected = False
                self.error = f"cannot open {self.config.source}"
                cap.release()
                self._stop.wait(1.0)
                continue

            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
            cap.set(cv2.CAP_PROP_FPS, self.config.fps)
            self.connected = True
            self.error = None

            frames = 0
            window = time.perf_counter()
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    self.connected = False
                    self.error = "stream dropped"
                    break
                target = (self.config.width, self.config.height)
                if (frame.shape[1], frame.shape[0]) != target:
                    frame = cv2.resize(frame, target, interpolation=cv2.INTER_AREA)
                ok_j, buf = cv2.imencode(
                    ".jpg",
                    frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), self.config.jpeg_quality],
                )
                with self._lock:
                    self._bgr = frame
                    if ok_j:
                        self._jpeg = buf.tobytes()
                    self.frame_id += 1
                frames += 1
                now = time.perf_counter()
                if now - window >= 1.0:
                    self.capture_fps = frames / (now - window)
                    frames = 0
                    window = now
            cap.release()
            if not self._stop.is_set():
                self._stop.wait(0.5)


class CameraHub:
    def __init__(self, configs: dict[str, CameraConfig]) -> None:
        self.streams = {name: CameraStream(name, cfg) for name, cfg in configs.items()}

    def start(self) -> None:
        for stream in self.streams.values():
            stream.start()

    def stop(self) -> None:
        for stream in self.streams.values():
            stream.stop()

    def names(self) -> list[str]:
        return list(self.streams.keys())

    def latest_jpeg(self, name: str) -> bytes:
        stream = self.streams.get(name)
        return b"" if stream is None else stream.latest_jpeg()

    def latest_rgb_map(self) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for name, stream in self.streams.items():
            rgb = stream.latest_rgb()
            if rgb is not None:
                out[name] = rgb
        return out

    def latest_bgr_map(self) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for name, stream in self.streams.items():
            bgr = stream.latest_bgr()
            if bgr is not None:
                out[name] = bgr
        return out

    def snapshots(self) -> list[dict[str, Any]]:
        return [stream.snapshot() for stream in self.streams.values()]
