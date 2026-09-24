from pathlib import Path

from lerobot_monitor.config import MonitorConfig, RecordingConfig


def test_default_config() -> None:
    cfg = MonitorConfig()
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8090
    assert cfg.server.base_path == "/lerobot"
    assert cfg.robot.auto_connect is False
    assert cfg.robot.type == "so101_follower"
    assert cfg.snapshots_root() == Path("data/snapshots")


def test_public_example_stays_local_and_does_not_select_serial_ports() -> None:
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    cfg = MonitorConfig.load(example)
    assert cfg.server.host == "127.0.0.1"
    assert cfg.robot.port == ""
    assert cfg.leader.port == ""
    assert cfg.robot.auto_connect is False
    assert cfg.leader.auto_connect is False


def test_load_yaml(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "server:\n  port: 9999\nrobot:\n  port: COM9\n"
        "library:\n  snapshots_root: custom/snapshots\n",
        encoding="utf-8",
    )
    cfg = MonitorConfig.load(path)
    assert cfg.server.port == 9999
    assert cfg.robot.port == "COM9"
    assert cfg.snapshots_root() == Path("custom/snapshots")


def test_load_huggingface_and_calibration_paths(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "huggingface_home: D:/Cache/huggingface\n"
        "robot:\n  calibration_dir: D:/datasets/lerobot/calibration/robots/so_follower\n"
        "leader:\n  calibration_dir: D:/datasets/lerobot/calibration/teleoperators/so_leader\n",
        encoding="utf-8",
    )

    cfg = MonitorConfig.load(path)
    assert cfg.huggingface_home == Path("D:/Cache/huggingface")
    assert cfg.robot.calibration_dir == Path("D:/datasets/lerobot/calibration/robots/so_follower")
    assert cfg.leader.calibration_dir == Path("D:/datasets/lerobot/calibration/teleoperators/so_leader")


def test_recording_rates_default_and_legacy_fps() -> None:
    current = RecordingConfig()
    assert (current.action_fps, current.video_fps) == (15, 30)
    legacy = RecordingConfig(fps=12)
    assert (legacy.action_fps, legacy.video_fps) == (12, 12)
