from pathlib import Path

from lerobot_monitor.config import MonitorConfig


def test_default_config() -> None:
    cfg = MonitorConfig()
    assert cfg.server.port == 8088
    assert cfg.robot.type == "so101_follower"


def test_load_yaml(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text("server:\n  port: 9999\nrobot:\n  port: COM9\n", encoding="utf-8")
    cfg = MonitorConfig.load(path)
    assert cfg.server.port == 9999
    assert cfg.robot.port == "COM9"
