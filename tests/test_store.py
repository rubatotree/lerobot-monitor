from pathlib import Path

import pytest

from lerobot_monitor.store import JsonStore


def test_default_pose_presets(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "store.json")
    pose = store.presets("pose")
    assert "relax" in pose
    assert "zero" in pose
    assert pose["zero"]["gripper"] == 0.0
    assert pose["relax"]["shoulder_lift"] == -103.0
    hardware = store.presets("hardware")
    assert hardware["Disconnected"]["system"] is True
    assert hardware["Disconnected"]["devices"] == {}
    assert hardware["Disconnected"]["cameras"] == {}


def test_presets_roundtrip(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "store.json")
    store.put_preset("pose", "fold", {"gripper": 0.0, "shoulder_pan": -4.0})
    store.put_preset("record", "blocks", {"task": "sort", "episode_time_s": 20})
    store.put_preset("debug", "vla", {"policy_path": "models/vla", "chunk_size": 16})
    store.put_preset("hardware", "bench", {"schema": 1, "devices": {}, "cameras": {}})
    again = JsonStore(tmp_path / "store.json")
    assert again.presets("pose")["fold"]["gripper"] == 0.0
    assert again.presets("record")["blocks"]["task"] == "sort"
    assert again.presets("debug")["vla"]["chunk_size"] == 16
    assert again.presets("hardware")["bench"]["schema"] == 1
    again.delete_preset("record", "blocks")
    assert "blocks" not in JsonStore(tmp_path / "store.json").presets("record")


def test_system_preset_protection_and_rename(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "store.json")
    with pytest.raises(ValueError, match="system preset"):
        store.put_preset("hardware", "Disconnected", {"system": False})
    with pytest.raises(ValueError, match="system preset"):
        store.delete_preset("hardware", "Disconnected")
    with pytest.raises(ValueError, match="system preset"):
        store.rename_preset("hardware", "Disconnected", "empty")

    store.put_preset("hardware", "bench", {"schema": 1, "devices": {}, "cameras": {}})
    renamed = store.rename_preset("hardware", "bench", "bench 2")
    assert renamed["schema"] == 1
    assert "bench" not in store.presets("hardware")
    assert "bench 2" in store.presets("hardware")
    with pytest.raises(ValueError, match="already exists"):
        store.rename_preset("hardware", "bench 2", "Disconnected")


def test_legacy_system_hardware_preset_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    path.write_text(
        """
{
  "presets": {
    "hardware": {
      "Nothing connected": {
        "schema": 1,
        "system": true,
        "devices": {},
        "cameras": {}
      }
    }
  },
  "ui": {"active_hardware_preset": "Nothing connected"}
}
""",
        encoding="utf-8",
    )

    store = JsonStore(path)

    assert "Nothing connected" not in store.presets("hardware")
    assert store.presets("hardware")["Disconnected"]["system"] is True
    assert store.ui()["active_hardware_preset"] == "Disconnected"


def test_camera_settings_merge(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "store.json")
    store.save_camera("0", {"label": "front", "show_main": True})
    store.save_camera("0", {"feed_robot": True})
    saved = JsonStore(tmp_path / "store.json").camera_settings("0")
    assert saved["label"] == "front"
    assert saved["show_main"] is True
    assert saved["feed_robot"] is True


def test_episode_overrides_roundtrip(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "store.json")
    store.save_episode_override("video", "blocks_2026", 3, {"name": "grasp", "note": "slipped once"})
    store.save_episode_override("video", "blocks_2026", 3, {"task": "sort blocks"})
    store.save_episode_override("dataset", "user/so101", 0, {"name": "first"})
    again = JsonStore(tmp_path / "store.json")
    assert again.episode_overrides("video", "blocks_2026")["3"] == {
        "name": "grasp",
        "note": "slipped once",
        "task": "sort blocks",
    }
    assert again.episode_overrides("dataset", "user/so101")["0"]["name"] == "first"
    assert again.episode_overrides("video", "missing") == {}


def test_episode_overrides_remap_and_delete(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "store.json")
    for index, name in enumerate(("zero", "one", "two")):
        store.save_episode_override("video", "session", index, {"name": name})

    remapped = store.remap_episode_overrides("video", "session", {0: 2, 2: 0})
    assert remapped == {"2": {"name": "zero"}, "0": {"name": "two"}}
    assert "1" not in store.episode_overrides("video", "session")

    store.delete_episode_overrides("video", "session")
    assert store.episode_overrides("video", "session") == {}


def test_library_overrides_roundtrip_and_delete(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "store.json")
    store.save_library_override(
        "video",
        "blocks_2026",
        {"note": "check grasp", "description": "local capture"},
    )
    store.save_library_override("dataset", "user/so101", {"note": "baseline"})
    store.save_library_override("video", "blocks_2026", {"note": "updated"})

    again = JsonStore(tmp_path / "store.json")
    assert again.library_override("video", "blocks_2026") == {
        "note": "updated",
        "description": "local capture",
    }
    assert again.library_override("dataset", "user/so101") == {"note": "baseline"}

    again.delete_library_override("video", "blocks_2026")
    assert again.library_override("video", "blocks_2026") == {}
