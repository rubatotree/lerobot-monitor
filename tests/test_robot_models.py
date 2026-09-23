from __future__ import annotations

import json
from pathlib import Path

import pytest

from lerobot_monitor.robot_models import (
    MANIFEST_NAME,
    SCHEMA,
    RobotModelError,
    RobotModelRegistry,
)
from lerobot_monitor.store import JsonStore


def _write_model(root: Path, model_id: str = "test-arm") -> Path:
    root.mkdir(parents=True)
    (root / "model.urdf").write_text(
        '<robot name="test"><link name="base"/></robot>',
        encoding="utf-8",
    )
    (root / MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "id": model_id,
                "name": "Test Arm",
                "robot_types": ["test_follower"],
                "urdf": "model.urdf",
                "joint_map": {"shoulder_pan": "shoulder_pan"},
            }
        ),
        encoding="utf-8",
    )
    return root


def test_registry_lists_builtin_and_registers_local_model(tmp_path: Path) -> None:
    registry = RobotModelRegistry(JsonStore(tmp_path / "store.json"), tmp_path / "models")
    assert [row["id"] for row in registry.list()] == ["so101"]
    assert registry.active() == "so101"

    source = _write_model(tmp_path / "source")
    row = registry.register(remote=str(source))
    assert row["id"] == "test-arm"
    assert row["source"] == "local"
    assert registry.match_robot_type("test_follower")["id"] == "test-arm"


def test_registry_rejects_path_escape_and_invalid_manifest(tmp_path: Path) -> None:
    registry = RobotModelRegistry(JsonStore(tmp_path / "store.json"), tmp_path / "models")
    source = _write_model(tmp_path / "source")
    row = registry.register(remote=str(source))
    with pytest.raises(RobotModelError):
        registry.resolve_file(row["id"], "../outside.txt")

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / MANIFEST_NAME).write_text("{}", encoding="utf-8")
    with pytest.raises(RobotModelError):
        registry.register(remote=str(broken))


def test_builtin_model_cannot_be_deleted(tmp_path: Path) -> None:
    registry = RobotModelRegistry(JsonStore(tmp_path / "store.json"), tmp_path / "models")
    with pytest.raises(RobotModelError):
        registry.delete("so101")
