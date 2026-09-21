"""Leader-arm adapter used only during teleop / recording."""

from __future__ import annotations

from typing import Any

from .config import LeaderConfig
from .pathutil import ensure_lerobot_on_path
from .sim import install_socket_transport
from .types import observation_to_pose


class LeaderArm:
    def __init__(self, config: LeaderConfig) -> None:
        self.config = config
        self.teleop: Any = None
        self.connected = False
        self.error: str | None = None

    def connect(self) -> None:
        ensure_lerobot_on_path()
        try:
            from lerobot.teleoperators.so_leader import SO101Leader, SO101LeaderConfig
        except ImportError as exc:
            self.error = f"lerobot not importable: {exc}"
            self.connected = False
            raise RuntimeError(self.error) from exc

        # 与 FollowerArm 同理：socket:// 的分发必须在 FeetechMotorsBus 构造前装好。
        install_socket_transport()

        if self.connected:
            return
        cfg = SO101LeaderConfig(
            port=self.config.port,
            id=self.config.id,
            use_degrees=self.config.use_degrees,
        )
        teleop = SO101Leader(cfg)
        try:
            teleop.connect(calibrate=self.config.calibrate)
        except Exception as exc:
            self.error = str(exc)
            self.connected = False
            raise
        self.teleop = teleop
        self.connected = True
        self.error = None

    def disconnect(self) -> None:
        if self.teleop is None:
            self.connected = False
            return
        try:
            self.teleop.disconnect()
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
        finally:
            self.teleop = None
            self.connected = False

    def get_action_pose(self) -> dict[str, float]:
        if self.teleop is None:
            raise RuntimeError("leader not connected")
        action = self.teleop.get_action()
        return observation_to_pose(action)

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "type": self.config.type,
            "port": self.config.port,
            "id": self.config.id,
            "error": self.error,
        }
