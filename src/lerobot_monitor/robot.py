"""Follower-arm adapter. Cameras are deliberately omitted from the robot object
so a camera failure cannot take the bus down, and vice versa.
"""

from __future__ import annotations

from typing import Any

from .config import RobotConfig
from .pathutil import ensure_lerobot_on_path
from .sim import install_socket_transport
from .types import JOINT_ORDER, observation_to_pose, pose_to_action


class FollowerArm:
    def __init__(self, config: RobotConfig) -> None:
        self.config = config
        self.robot: Any = None
        self.connected = False
        self.error: str | None = None

    def connect(self) -> None:
        ensure_lerobot_on_path()
        try:
            from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig
        except ImportError as exc:
            self.error = f"lerobot not importable: {exc}"
            self.connected = False
            raise RuntimeError(self.error) from exc

        # 必须在构造 SO101Follower 之前——FeetechMotorsBus 在 __init__ 里就把
        # scs.PortHandler 抓走了。真机串口名会被原样透传，不受影响。
        install_socket_transport()

        if self.connected:
            return
        cfg = SO101FollowerConfig(
            port=self.config.port,
            id=self.config.id,
            use_degrees=self.config.use_degrees,
            disable_torque_on_disconnect=self.config.disable_torque_on_disconnect,
            cameras={},
        )
        robot = SO101Follower(cfg)
        try:
            # Skip the interactive calibration prompt; the file on disk is already loaded.
            robot.connect(calibrate=self.config.calibrate)
        except Exception as exc:
            self.error = str(exc)
            self.connected = False
            raise
        self.robot = robot
        self.connected = True
        self.error = None

    def disconnect(self) -> None:
        if self.robot is None:
            self.connected = False
            return
        try:
            self.robot.disconnect()
        except Exception as exc:  # noqa: BLE001 — hardware teardown must not raise into the loop
            self.error = str(exc)
        finally:
            self.robot = None
            self.connected = False

    def get_pose(self) -> dict[str, float]:
        if self.robot is None:
            raise RuntimeError("follower not connected")
        obs = self.robot.get_observation()
        return observation_to_pose(obs)

    def send_pose(self, pose: dict[str, float]) -> dict[str, float]:
        if self.robot is None:
            raise RuntimeError("follower not connected")
        sent = self.robot.send_action(pose_to_action(pose))
        return observation_to_pose(sent)

    def disable_torque(self) -> None:
        if self.robot is None:
            return
        self.robot.bus.disable_torque()

    def enable_torque(self) -> None:
        if self.robot is None:
            return
        self.robot.bus.enable_torque()

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "type": self.config.type,
            "port": self.config.port,
            "id": self.config.id,
            "error": self.error,
            "joints": list(JOINT_ORDER),
        }
