from pathlib import Path

from lerobot_monitor.store import JsonStore


def test_default_pose_presets(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "store.json")
    pose = store.presets("pose")
    assert "relax" in pose
    assert "zero" in pose
    assert pose["zero"]["gripper"] == 0.0
    assert pose["relax"]["shoulder_lift"] == -103.0


def test_presets_roundtrip(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "store.json")
    store.put_preset("pose", "fold", {"gripper": 0.0, "shoulder_pan": -4.0})
    store.put_preset("record", "blocks", {"task": "sort", "episode_time_s": 20})
    again = JsonStore(tmp_path / "store.json")
    assert again.presets("pose")["fold"]["gripper"] == 0.0
    assert again.presets("record")["blocks"]["task"] == "sort"
    again.delete_preset("record", "blocks")
    assert "blocks" not in JsonStore(tmp_path / "store.json").presets("record")


def test_camera_settings_merge(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "store.json")
    store.save_camera("0", {"label": "front", "show_main": True})
    store.save_camera("0", {"feed_robot": True})
    saved = JsonStore(tmp_path / "store.json").camera_settings("0")
    assert saved["label"] == "front"
    assert saved["show_main"] is True
    assert saved["feed_robot"] is True
