"""YAML configuration for the monitor daemon."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8090
    base_path: str = "/lerobot"


class RobotConfig(BaseModel):
    type: str = "so101_follower"
    port: str = "COM6"
    id: str = "my_awesome_follower_arm"
    use_degrees: bool = True
    auto_connect: bool = False
    disable_torque_on_disconnect: bool = True
    calibrate: bool = False


class LeaderConfig(BaseModel):
    type: str = "so101_leader"
    port: str = "COM5"
    id: str = "my_awesome_leader_arm"
    use_degrees: bool = True
    auto_connect: bool = False
    calibrate: bool = False


class CamerasConfig(BaseModel):
    probe: bool = True
    max_probe: int = 8
    default_width: int = 640
    default_height: int = 480
    default_port_base: int = 5000
    jpeg_quality: int = 80


class ControlConfig(BaseModel):
    fps: float = 30.0
    hold_when_idle: bool = True
    jog_duration_s: float = 2.5
    # Unused for idle timeout. Relax-then-release only runs when COM is being
    # dropped and no later mode will hold the pose (disconnect / process stop).
    bus_release_s: float = 0.0


class RecordingConfig(BaseModel):
    root: Path = Path("data/videos")
    fps: int = 15
    action_fps: int = 15
    video_fps: int = 30
    default_episode_time_s: float = 20.0
    default_reset_time_s: float = 5.0
    default_num_episodes: int = 50
    video_format: str = "mp4"
    merge: bool = True
    streaming_encoding: bool = True
    encoder_threads: int = 2
    video: bool = True

    @model_validator(mode="before")
    @classmethod
    def legacy_fps_sets_both_rates(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "fps" not in value:
            return value
        data = dict(value)
        data.setdefault("action_fps", data["fps"])
        data.setdefault("video_fps", data["fps"])
        return data

    @model_validator(mode="after")
    def validate_rates(self) -> "RecordingConfig":
        if self.fps <= 0 or self.action_fps <= 0 or self.video_fps <= 0:
            raise ValueError("recording fps, action_fps, and video_fps must be positive")
        return self


class LibraryConfig(BaseModel):
    videos_root: Path | None = None
    datasets_root: Path | None = None
    snapshots_root: Path = Path("data/snapshots")
    dataset_roots: list[Path] = Field(default_factory=list)
    models_roots: list[Path] = Field(default_factory=lambda: [Path("data/models"), Path("../outputs")])


class RolloutConfig(BaseModel):
    device: str = "cuda"
    default_duration_s: float = 60.0
    default_fps: int = 15
    prediction_interval_s: float = 0.5
    prediction_chunk_size: int = 16
    rename_map: dict[str, str] = Field(default_factory=dict)


class MonitorConfig(BaseModel):
    store_path: Path = Path("data/monitor_store.json")
    server: ServerConfig = Field(default_factory=ServerConfig)
    robot: RobotConfig = Field(default_factory=RobotConfig)
    leader: LeaderConfig = Field(default_factory=LeaderConfig)
    cameras: CamerasConfig = Field(default_factory=CamerasConfig)

    @field_validator("cameras", mode="before")
    @classmethod
    def _coerce_cameras(cls, value: Any) -> Any:
        if value is None:
            return CamerasConfig()
        if isinstance(value, dict) and "probe" not in value and "default_width" not in value:
            return CamerasConfig()
        return value
    control: ControlConfig = Field(default_factory=ControlConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    library: LibraryConfig = Field(default_factory=LibraryConfig)
    rollout: RolloutConfig = Field(default_factory=RolloutConfig)

    def videos_root(self) -> Path:
        return Path(self.library.videos_root or self.recording.root)

    def datasets_root(self) -> Path:
        """Backward-compatible alias for the local video session root."""
        return self.videos_root()

    def snapshots_root(self) -> Path:
        return Path(self.library.snapshots_root)

    @classmethod
    def load(cls, path: str | Path | None) -> MonitorConfig:
        if path is None:
            return cls()
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)
