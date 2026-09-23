"""In-memory follower used when no physical arm is connected.

The control loop already owns the transition between idle, jogging, teleop,
recording and rollout.  Keeping the virtual arm behind the same narrow adapter
interface means those workflows need no simulation-specific branches.
"""

from __future__ import annotations

import threading
from typing import Any

from .types import JOINT_ORDER, RELAX_POSE, clamp_pose

VIRTUAL_PORT = "virtual://preview"


class VirtualFollowerArm:
    """Ideal position-tracking follower with torque and E-STOP semantics."""

    def __init__(
        self,
        *,
        model_id: str = "so101",
        robot_type: str = "so101_follower",
        arm_id: str = "virtual_preview",
        initial_pose: dict[str, float] | None = None,
    ) -> None:
        self.model_id = str(model_id or "so101")
        self.robot_type = str(robot_type or "so101_follower")
        self.arm_id = str(arm_id or "virtual_preview")
        self.connected = False
        self.error: str | None = None
        self._lock = threading.Lock()
        self._pose = clamp_pose(initial_pose or RELAX_POSE)
        self._torque_enabled = True

    def connect(self) -> None:
        with self._lock:
            self.connected = True
            self.error = None
            self._torque_enabled = True

    def disconnect(self) -> None:
        with self._lock:
            self.connected = False

    def set_model(self, model_id: str, robot_type: str = "so101_follower") -> None:
        with self._lock:
            self.model_id = str(model_id or "so101")
            self.robot_type = str(robot_type or "so101_follower")

    def get_pose(self) -> dict[str, float]:
        with self._lock:
            return dict(self._pose)

    def send_pose(self, pose: dict[str, float]) -> dict[str, float]:
        with self._lock:
            if not self.connected:
                raise RuntimeError("virtual follower not connected")
            if self._torque_enabled:
                self._pose = clamp_pose(pose)
            return dict(self._pose)

    def disable_torque(self) -> None:
        with self._lock:
            self._torque_enabled = False

    def enable_torque(self) -> None:
        with self._lock:
            self._torque_enabled = True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "connected": self.connected,
                "virtual": True,
                "type": self.robot_type,
                "port": VIRTUAL_PORT,
                "id": self.arm_id,
                "error": self.error,
                "joints": list(JOINT_ORDER),
                "model_id": self.model_id,
                "model_match": True,
                "torque_enabled": self._torque_enabled,
            }
