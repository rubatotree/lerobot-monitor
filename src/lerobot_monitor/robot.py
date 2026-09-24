"""Follower-arm adapter. Cameras are deliberately omitted from the robot object
so a camera failure cannot take the bus down, and vice versa.
"""

from __future__ import annotations

from typing import Any

from .config import RobotConfig
from .pathutil import ensure_lerobot_on_path
from .sim import install_socket_transport
from .types import JOINT_ORDER, observation_to_pose, pose_to_action
from .virtual_follower import VIRTUAL_PORT, VirtualFollowerArm


class FollowerArm:
    def __init__(self, config: RobotConfig) -> None:
        self.config = config
        self.robot: Any = None
        self.virtual: VirtualFollowerArm | None = None
        self.connected = False
        self.error: str | None = None

    def connect(self) -> None:
        if self.connected and self.virtual is None:
            return
        if self.virtual is not None:
            self.disconnect()

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

        cfg = SO101FollowerConfig(
            port=self.config.port,
            id=self.config.id,
            calibration_dir=self.config.calibration_dir,
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

    def connect_virtual(
        self,
        *,
        model_id: str = "so101",
        robot_type: str | None = None,
    ) -> None:
        """Replace any physical connection with the in-process follower."""
        if self.connected and self.virtual is not None:
            self.virtual.set_model(model_id, robot_type or self.config.type)
            return
        if self.connected:
            self.disconnect()
        backend = VirtualFollowerArm(
            model_id=model_id,
            robot_type=robot_type or self.config.type,
            arm_id=self.config.id,
        )
        try:
            backend.connect()
        except Exception as exc:  # pragma: no cover - the backend is deterministic
            self.error = str(exc)
            self.connected = False
            raise
        self.virtual = backend
        self.robot = None
        self.connected = True
        self.error = None

    def disconnect(self) -> None:
        if self.virtual is not None:
            try:
                self.virtual.disconnect()
            finally:
                self.virtual = None
                self.connected = False
            return
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
        if self.virtual is not None:
            return self.virtual.get_pose()
        if self.robot is None:
            raise RuntimeError("follower not connected")
        obs = self.robot.get_observation()
        return observation_to_pose(obs)

    def send_pose(self, pose: dict[str, float]) -> dict[str, float]:
        if self.virtual is not None:
            return self.virtual.send_pose(pose)
        if self.robot is None:
            raise RuntimeError("follower not connected")
        sent = self.robot.send_action(pose_to_action(pose))
        return observation_to_pose(sent)

    def disable_torque(self) -> None:
        if self.virtual is not None:
            self.virtual.disable_torque()
            return
        if self.robot is None:
            return
        self.robot.bus.disable_torque()

    def enable_torque(self) -> None:
        if self.virtual is not None:
            self.virtual.enable_torque()
            return
        if self.robot is None:
            return
        self.robot.bus.enable_torque()

    def set_virtual_model(self, model_id: str, robot_type: str | None = None) -> None:
        if self.virtual is None:
            return
        self.virtual.set_model(model_id, robot_type or self.config.type)

    @property
    def is_virtual(self) -> bool:
        return self.virtual is not None

    def snapshot(self) -> dict[str, Any]:
        if self.virtual is not None:
            data = self.virtual.snapshot()
            data["id"] = self.config.id
            return data
        return {
            "connected": self.connected,
            "virtual": False,
            "type": self.config.type,
            "port": self.config.port,
            "id": self.config.id,
            "error": self.error,
            "joints": list(JOINT_ORDER),
            "model_id": "",
            "model_match": False,
        }
