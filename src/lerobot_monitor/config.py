"""YAML configuration for the monitor daemon."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8088


class RobotConfig(BaseModel):
    type: str = "so101_follower"
    port: str = "COM6"
    id: str = "my_awesome_follower_arm"
    use_degrees: bool = True
    auto_connect: bool = True
    disable_torque_on_disconnect: bool = True
    calibrate: bool = False


class LeaderConfig(BaseModel):
    type: str = "so101_leader"
    port: str = "COM5"
    id: str = "my_awesome_leader_arm"
    use_degrees: bool = True
    auto_connect: bool = False
    calibrate: bool = False


class CameraConfig(BaseModel):
    source: str | int
    width: int = 640
    height: int = 480
    fps: int = 25
    jpeg_quality: int = 80

    @field_validator("source", mode="before")
    @classmethod
    def _coerce_source(cls, value: Any) -> str | int:
        if isinstance(value, int):
            return value
        text = str(value)
        return int(text) if text.isdigit() else text


class ControlConfig(BaseModel):
    fps: float = 30.0
    hold_when_idle: bool = True
    jog_duration_s: float = 1.5


class RecordingConfig(BaseModel):
    root: Path = Path("data/sessions")
    fps: int = 15
    default_episode_time_s: float = 20.0
    default_reset_time_s: float = 5.0


class RolloutConfig(BaseModel):
    device: str = "cuda"
    default_duration_s: float = 60.0
    rename_map: dict[str, str] = Field(default_factory=dict)


class MonitorConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    robot: RobotConfig = Field(default_factory=RobotConfig)
    leader: LeaderConfig = Field(default_factory=LeaderConfig)
    cameras: dict[str, CameraConfig] = Field(default_factory=dict)
    control: ControlConfig = Field(default_factory=ControlConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    rollout: RolloutConfig = Field(default_factory=RolloutConfig)

    @classmethod
    def load(cls, path: str | Path | None) -> MonitorConfig:
        if path is None:
            return cls()
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)
