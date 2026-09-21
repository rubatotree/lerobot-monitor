from pathlib import Path

from lerobot_monitor.config import MonitorConfig, RecordingConfig


def test_default_config() -> None:
    cfg = MonitorConfig()
    assert cfg.server.port == 8090
    assert cfg.server.base_path == "/lerobot"
    assert cfg.robot.auto_connect is False
    assert cfg.robot.type == "so101_follower"


def test_load_yaml(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text("server:\n  port: 9999\nrobot:\n  port: COM9\n", encoding="utf-8")
    cfg = MonitorConfig.load(path)
    assert cfg.server.port == 9999
    assert cfg.robot.port == "COM9"


def test_recording_rates_default_and_legacy_fps() -> None:
    current = RecordingConfig()
    assert (current.action_fps, current.video_fps) == (15, 30)
    legacy = RecordingConfig(fps=12)
    assert (legacy.action_fps, legacy.video_fps) == (12, 12)
