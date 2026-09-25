"""Timing and session invariants for physical playback."""

from __future__ import annotations

from pathlib import Path

import pytest

from lerobot_monitor.control_rates import CadenceStats, RateSetting
from lerobot_monitor import loop as loop_module
from lerobot_monitor import trajectory as trajectory_module
from lerobot_monitor.loop import Command
from lerobot_monitor.trajectory import JointTrajectory

from test_loop import _dispatch, _loop


@pytest.mark.parametrize("hz", [15, 30, 50, 60])
def test_trajectory_resampling_preserves_duration_and_endpoint(hz: int) -> None:
    times = tuple(index / 15 for index in range(151))
    poses = tuple({"gripper": 10.0 * t} for t in times)
    trajectory = JointTrajectory(times, poses, 15.0)
    commands = [trajectory.sample(index / hz) for index in range(10 * hz + 1)]
    assert len(commands) == 10 * hz + 1
    assert trajectory.duration_s == pytest.approx(10.0)
    assert commands[0]["gripper"] == pytest.approx(0.0)
    assert commands[-1]["gripper"] == pytest.approx(100.0)
    assert commands[hz // 2]["gripper"] == pytest.approx(10 * (hz // 2) / hz)


def test_arbitrary_rate_and_hold_interpolation() -> None:
    trajectory = JointTrajectory((0.0, 1 / 15, 2 / 15), ({"gripper": 0.0}, {"gripper": 10.0}, {"gripper": 20.0}), 15.0)
    assert trajectory.sample(1 / 50)["gripper"] == pytest.approx(3.0)
    assert trajectory.sample(1 / 50, "hold")["gripper"] == 0.0
    assert RateSetting("multiplier", 4).resolve(30, trajectory.source_hz) == 60


def test_playback_load_uses_complete_episode(tmp_path: Path, monkeypatch) -> None:
    calls: list[bool] = []

    def load(_root: Path, _episode: int, *, full: bool = False) -> dict:
        calls.append(full)
        times = [index / 15 for index in range(1001)]
        return {"t": times, "series": {"act.gripper": times}, "action_fps": 15}

    monkeypatch.setattr(trajectory_module, "local_episode_payload", load)
    trajectory = JointTrajectory.load("video", tmp_path, 0)
    assert calls == [True]
    assert len(trajectory.times) == 1001
    assert trajectory.sample(1000 / 15)["gripper"] == pytest.approx(1000 / 15)


def test_playback_session_rejects_stale_control(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.mode = "idle"
    trajectory = JointTrajectory((0.0, 1.0), ({"gripper": 0.0}, {"gripper": 20.0}), 1.0)
    started = _dispatch(loop, "playback_start", {
        "kind": "video", "source_id": "demo", "episode": 0,
        "source": "command", "trajectory": trajectory,
    })["playback"]
    assert started["aligning"]
    duplicate = _dispatch(loop, "playback_start", {
        "kind": "video", "source_id": "demo", "episode": 0,
        "source": "command", "trajectory": trajectory,
    })["playback"]
    assert duplicate["id"] == started["id"]
    paused = _dispatch(loop, "playback_control", {
        "id": started["id"], "version": started["version"], "operation": "pause",
    })["playback"]
    assert paused["playing"] is False
    with pytest.raises(ValueError, match="state changed"):
        loop._handle(Command("playback_control", {
            "id": started["id"], "version": started["version"], "operation": "resume",
        }))
    assert loop.playback["playing"] is False


def test_cadence_counts_completed_sends() -> None:
    cadence = CadenceStats()
    for step in range(60):
        cadence.sent_at(step / 60)
    snapshot = cadence.snapshot(60, now=59 / 60)
    assert snapshot["sent"] == 60
    assert snapshot["actual_hz"] == pytest.approx(60.0)
    assert snapshot["interval_p95_ms"] == pytest.approx(16.67)


def test_live_rate_changes_do_not_change_playback_clock(tmp_path: Path, monkeypatch) -> None:
    loop = _loop(tmp_path)
    loop.mode = "idle"
    trajectory = JointTrajectory((0.0, 1.0), ({"gripper": 0.0}, {"gripper": 20.0}), 15.0)
    _dispatch(loop, "playback_start", {
        "kind": "video", "source_id": "demo", "episode": 0,
        "source": "command", "trajectory": trajectory, "speed": 2.0,
    })
    assert loop.playback is not None
    loop.playback.update(aligning=False, playing=True, clock_t=100.0)
    monkeypatch.setattr(loop_module.time, "perf_counter", lambda: 100.25)
    before = loop._playback_elapsed(100.25)
    assert before == pytest.approx(0.5)
    _dispatch(loop, "control_rates", {"mode": "playback", "setting": {"kind": "multiplier", "value": 4}})
    assert loop._control_hz() == 60
    _dispatch(loop, "control_rates", {"mode": "playback", "setting": {"kind": "hz", "value": 50}})
    assert loop._control_hz() == 50
    assert loop._playback_elapsed(100.25) == before


def test_slow_playback_changes_effective_source_rate_only(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.mode = "idle"
    trajectory = JointTrajectory((0.0, 1.0), ({"gripper": 0.0}, {"gripper": 20.0}), 15.0)
    started = _dispatch(loop, "playback_start", {
        "kind": "video", "source_id": "demo", "episode": 0,
        "source": "command", "trajectory": trajectory, "speed": 0.5,
    })["playback"]
    _dispatch(loop, "control_rates", {"mode": "playback", "setting": {"kind": "multiplier", "value": 4}})
    assert started["source_hz"] == 15.0
    assert started["effective_source_hz"] == 7.5
    assert loop._control_hz() == 60.0

    changed = _dispatch(loop, "playback_control", {
        "id": started["id"], "version": started["version"],
        "operation": "speed", "speed": 2.0,
    })["playback"]
    assert changed["effective_source_hz"] == 30.0
    assert loop._control_hz() == 60.0


def test_dataset_fps_cannot_be_used_as_record_control_multiplier(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    with pytest.raises(ValueError, match="no stable action source"):
        loop._task_setting("record", {"control_rate": {"kind": "multiplier", "value": 4}}, 15)
