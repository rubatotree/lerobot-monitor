from __future__ import annotations

import queue
from pathlib import Path
from unittest.mock import MagicMock

from lerobot_monitor.config import (
    CamerasConfig,
    LibraryConfig,
    MonitorConfig,
    RecordingConfig,
    RobotConfig,
    VirtualFollowerConfig,
)
from lerobot_monitor.loop import Command, ControlLoop
from lerobot_monitor.robot import FollowerArm
from lerobot_monitor.types import JOINT_ORDER, RELAX_POSE
from lerobot_monitor.virtual_follower import VIRTUAL_PORT, VirtualFollowerArm


def test_virtual_follower_tracks_pose_and_torque() -> None:
    arm = VirtualFollowerArm()
    arm.connect()
    assert arm.snapshot()["virtual"] is True
    assert arm.snapshot()["port"] == VIRTUAL_PORT

    goal = dict(RELAX_POSE)
    goal["shoulder_pan"] = 42.0
    assert arm.send_pose(goal)["shoulder_pan"] == 42.0

    arm.disable_torque()
    blocked = dict(goal)
    blocked["shoulder_pan"] = -42.0
    assert arm.send_pose(blocked)["shoulder_pan"] == 42.0

    arm.enable_torque()
    assert arm.send_pose(blocked)["shoulder_pan"] == -42.0
    arm.disconnect()
    assert arm.snapshot()["connected"] is False


def test_follower_adapter_switches_between_virtual_backends() -> None:
    adapter = FollowerArm(RobotConfig(id="preview"))
    adapter.connect_virtual(model_id="so101")
    assert adapter.connected is True
    assert adapter.is_virtual is True
    assert adapter.snapshot()["model_id"] == "so101"
    assert set(adapter.get_pose()) == set(JOINT_ORDER)

    adapter.disconnect()
    assert adapter.connected is False
    assert adapter.is_virtual is False


def _loop(tmp_path: Path, *, auto_virtual: bool = True) -> ControlLoop:
    config = MonitorConfig(
        robot=RobotConfig(auto_connect=False),
        virtual_follower=VirtualFollowerConfig(enabled=True, auto_connect=auto_virtual),
        cameras=CamerasConfig(probe=False),
        recording=RecordingConfig(root=tmp_path / "videos"),
        library=LibraryConfig(videos_root=tmp_path / "videos"),
    )
    cameras = MagicMock()
    cameras.snapshots.return_value = []
    follower = FollowerArm(config.robot)
    leader = MagicMock()
    leader.connected = False
    leader.snapshot.return_value = {"connected": False}
    return ControlLoop(config, cameras, follower, leader)


def test_loop_connects_virtual_follower_and_keeps_physical_priority(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop._connect_virtual_follower()
    assert loop.follower.connected is True
    assert loop.follower.is_virtual is True
    assert loop.mode == "idle"

    loop.follower.disconnect()
    physical = MagicMock()
    physical.connected = True
    loop.follower = physical
    loop.follower.is_virtual = False
    reply: queue.Queue[dict] = queue.Queue(maxsize=1)
    loop._handle(Command("virtual_connect", {"model_id": "so101"}, reply))
    assert reply.get(timeout=1)["ok"] is True
    assert physical.connect_virtual.call_count == 0


def test_virtual_disconnect_disables_auto_reconnect(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop._connect_virtual_follower()
    reply: queue.Queue[dict] = queue.Queue(maxsize=1)
    loop._handle(Command("virtual_disconnect", {}, reply))
    assert reply.get(timeout=1)["ok"] is True
    assert loop.follower.connected is False
    assert loop.mode == "offline"
