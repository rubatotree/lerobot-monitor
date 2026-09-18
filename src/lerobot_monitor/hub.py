"""Process-wide runtime: cameras + control loop + latest snapshot."""

from __future__ import annotations

import threading
from typing import Any

from .cameras import CameraHub
from .config import MonitorConfig
from .leader import LeaderArm
from .loop import ControlLoop
from .robot import FollowerArm
from .session import list_sessions
from .types import JOINT_LIMITS, JOINT_ORDER, PRESETS


class RuntimeHub:
    def __init__(self, config: MonitorConfig) -> None:
        self.config = config
        self.cameras = CameraHub(config.cameras)
        self.follower = FollowerArm(config.robot)
        self.leader = LeaderArm(config.leader)
        self._snapshot: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.loop = ControlLoop(
            config,
            self.cameras,
            self.follower,
            self.leader,
            on_snapshot=self._store_snapshot,
        )

    def _store_snapshot(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._snapshot = snapshot

    def start(self) -> None:
        self.cameras.start()
        self.loop.start()

    def stop(self) -> None:
        self.loop.stop()
        self.cameras.stop()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._snapshot:
                return dict(self._snapshot)
        return self.loop.snapshot()

    def static_meta(self) -> dict[str, Any]:
        return {
            "joints": list(JOINT_ORDER),
            "limits": {name: {"min": lo, "max": hi} for name, (lo, hi) in JOINT_LIMITS.items()},
            "presets": PRESETS,
            "cameras": list(self.config.cameras.keys()),
            "recording_root": str(self.config.recording.root),
            "control_fps": self.config.control.fps,
        }

    def sessions(self) -> list[dict[str, Any]]:
        return list_sessions(self.config.recording.root)
