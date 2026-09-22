import logging
import queue
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from lerobot_monitor import loop as loop_module
from lerobot_monitor.config import CamerasConfig, LibraryConfig, MonitorConfig, RecordingConfig, RobotConfig
from lerobot_monitor.loop import Command, ControlLoop
from lerobot_monitor.types import JOINT_ORDER


def _loop(tmp_path: Path) -> ControlLoop:
    config = MonitorConfig(
        robot=RobotConfig(auto_connect=False),
        cameras=CamerasConfig(probe=False),
        recording=RecordingConfig(root=tmp_path / "videos"),
        library=LibraryConfig(videos_root=tmp_path / "videos"),
    )
    cameras = MagicMock()
    cameras.snapshots.return_value = []
    follower = MagicMock()
    follower.connected = True
    follower.snapshot.return_value = {"connected": True}
    leader = MagicMock()
    leader.connected = False
    leader.snapshot.return_value = {"connected": False}
    return ControlLoop(config, cameras, follower, leader)


class FakeInferenceEngine:
    """Small stand-in for LeRobot's sync/RTC engine at the monitor boundary."""

    def __init__(self, action: dict[str, float], leftovers: list[dict[str, float]] | None = None) -> None:
        self.action = action
        self.failed = False
        self.failure_traceback = None
        self.notify_observation = MagicMock()
        self.get_action = MagicMock(return_value=action)
        self.stop = MagicMock()
        self.action_queue = None
        if leftovers is not None:
            self.action_queue = SimpleNamespace(
                get_processed_left_over=MagicMock(return_value=leftovers)
            )


def _dispatch(loop: ControlLoop, kind: str, payload: dict[str, object] | None = None) -> dict:
    """Run one command synchronously and return the handler reply."""
    reply: queue.Queue[dict] = queue.Queue(maxsize=1)
    loop._handle(Command(kind, payload or {}, reply))
    return reply.get(timeout=1)


def test_recorder_resume_is_explicit_and_root_is_honored(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    first = loop._open_recorder("record", {"name": "first"})
    first.close()

    fresh = loop._open_recorder("record", {"name": "fresh", "dataset_id": first.dataset_id, "resume": False})
    assert fresh.dataset_id != first.dataset_id
    fresh.close()

    with pytest.raises(ValueError, match="resume requires"):
        loop._open_recorder("record", {"resume": True})

    resumed = loop._open_recorder("record", {"video_id": first.dataset_id, "resume": True})
    assert resumed.root == first.root
    assert resumed.action_fps == first.action_fps
    assert resumed.video_fps == first.video_fps
    resumed.close()

    with pytest.raises(ValueError, match="format mismatch"):
        loop._open_recorder(
            "record",
            {"video_id": first.dataset_id, "resume": True, "format": "avi"},
        )

    custom_root = tmp_path / "custom-library"
    custom = loop._open_recorder("record", {"name": "custom", "root": str(custom_root)})
    assert custom.root.parent == custom_root.resolve()
    custom.close()


def test_stopped_policy_worker_cannot_replace_new_rollout(tmp_path: Path, monkeypatch) -> None:
    loop = _loop(tmp_path)
    old_release = threading.Event()

    def loaded(path: str):
        policy = MagicMock()
        policy.__class__.__name__ = f"Policy_{path}"
        return SimpleNamespace(path=path, task="", policy=policy, reset=MagicMock())

    def worker(job, path: str, _device: str, _task: str, _extra: dict[str, str]) -> None:
        if path == "old":
            old_release.wait(timeout=5)
        job.result = loaded(path)

    monkeypatch.setattr(loop, "_policy_worker", worker)
    monkeypatch.setattr(loop, "_start_inference_engine", lambda _loaded: True)
    loop._handle(Command("rollout_start", {"policy_path": "old", "record": False}))
    old_job = loop._policy_job
    assert old_job is not None

    loop.request_stop()
    loop._cancel.clear()
    loop._handle(Command("rollout_start", {"policy_path": "new", "record": False}))
    new_job = loop._policy_job
    assert new_job is not None and new_job is not old_job
    new_job.thread.join(timeout=5)
    loop._complete_rollout_load()
    assert loop.mode == "rollout"
    assert loop.loaded_policy.path == "new"

    old_release.set()
    old_job.thread.join(timeout=5)
    loop._complete_rollout_load()
    assert loop.loaded_policy.path == "new"


def test_policy_loads_are_serialized_across_control_loops(tmp_path: Path, monkeypatch) -> None:
    first = _loop(tmp_path / "first")
    second = _loop(tmp_path / "second")
    guard = threading.Lock()
    active = 0
    max_active = 0

    def fake_load(path: str, **_kwargs):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return SimpleNamespace(path=path, task="", policy=MagicMock(), reset=MagicMock())

    monkeypatch.setattr(loop_module, "load_policy", fake_load)
    jobs = [
        loop_module._PolicyLoadJob(generation=1, payload={}),
        loop_module._PolicyLoadJob(generation=1, payload={}),
    ]
    threads = [
        threading.Thread(target=first._policy_worker, args=(jobs[0], "first", "cpu", "", {})),
        threading.Thread(target=second._policy_worker, args=(jobs[1], "second", "cpu", "", {})),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert max_active == 1
    assert all(job.result is not None for job in jobs)


@pytest.mark.parametrize("kind", ["teleop_start", "record_start"])
def test_cancel_after_leader_connect_does_not_start_task(tmp_path: Path, kind: str) -> None:
    loop = _loop(tmp_path)
    loop.leader.connect.side_effect = loop._cancel.set

    loop._handle(Command(kind, {}))

    assert loop.writer is None
    assert loop.mode != "teleop"
    assert loop.mode != "record"


def test_jog_is_rejected_while_task_start_is_pending(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.pending = "record_start"

    with pytest.raises(RuntimeError, match="pending"):
        loop._handle(Command("jog", {"joints": {"gripper": 1.0}}))

    loop.follower.send_pose.assert_not_called()


def test_debug_lease_allows_offline_loop_without_follower(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.follower.connected = False
    loop.mode = "offline"

    granted = _dispatch(loop, "debug_lease_acquire")
    assert granted["ok"] is True
    assert granted["token"]
    assert loop.display_mode() == "debug"
    assert loop.bus_owner() == "debug"
    assert _dispatch(loop, "debug_lease_acquire")["error"] == "model debug is already active"


def test_debug_lease_rejects_pending_task_and_active_control_mode(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.mode = "idle"
    loop.pending = "rollout_start"
    assert _dispatch(loop, "debug_lease_acquire") == {
        "ok": False,
        "error": "model debug requires no pending task or recording",
    }
    loop.pending = None

    loop.mode = "rollout"
    assert _dispatch(loop, "debug_lease_acquire") == {
        "ok": False,
        "error": "model debug requires an idle control loop",
    }


def test_debug_lease_release_requires_matching_token(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.mode = "idle"
    loop.hold_when_idle = False
    token = str(_dispatch(loop, "debug_lease_acquire")["token"])

    stale = _dispatch(loop, "debug_lease_release", {"token": "not-the-token"})
    assert stale == {"ok": True, "released": False}
    assert loop._debug_lease_token == token

    assert _dispatch(loop, "debug_lease_release", {"token": token}) == {"ok": True, "released": True}
    assert loop._debug_lease_token is None
    assert loop.display_mode() == "idle"


def test_estop_clears_debug_lease(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.mode = "idle"
    _dispatch(loop, "debug_lease_acquire")

    loop.request_estop()

    assert loop._debug_lease_token is None
    assert loop.display_mode() == "estop"


def test_debug_lease_survives_serial_loss(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.mode = "idle"
    token = str(_dispatch(loop, "debug_lease_acquire")["token"])
    loop.follower.connected = False

    loop._tick()

    assert loop._debug_lease_token == token
    assert loop.display_mode() == "debug"


def test_offline_debug_lease_blocks_follower_connect(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.follower.connected = False
    loop.mode = "offline"
    _dispatch(loop, "debug_lease_acquire")

    with pytest.raises(RuntimeError, match="model debug is active"):
        loop._handle(Command("connect_robot", {"port": "COM1"}))

    loop.follower.connect.assert_not_called()


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("teleop_start", {}),
        ("record_start", {}),
        ("rollout_start", {"policy_path": "fake/policy"}),
        ("jog", {"joints": {"gripper": 1.0}}),
        ("resume", {}),
    ],
)
def test_debug_lease_blocks_control_commands(tmp_path: Path, kind: str, payload: dict) -> None:
    loop = _loop(tmp_path)
    loop.mode = "idle"
    _dispatch(loop, "debug_lease_acquire")

    with pytest.raises(RuntimeError, match="model debug is active"):
        loop._handle(Command(kind, dict(payload)))

    assert loop.mode == "idle"
    loop.follower.send_pose.assert_not_called()


def test_debug_lease_does_not_hold_pose_while_idle(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.mode = "idle"
    loop.hold_when_idle = True
    loop.latched = {"gripper": 5.0}
    _dispatch(loop, "debug_lease_acquire")

    loop._tick()

    loop.follower.send_pose.assert_not_called()


def test_estop_does_not_wait_for_policy_lock(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop._policy_job_lock.acquire()
    try:
        started = time.perf_counter()
        result = loop.request_estop()
        elapsed = time.perf_counter() - started
    finally:
        loop._policy_job_lock.release()

    assert result == {"ok": True}
    assert elapsed < 0.25
    assert loop._estop.is_set()
    assert loop._cancel.is_set()
    loop.follower.disable_torque.assert_called()
    loop.follower.disconnect.assert_called()


def test_live_jog_cannot_cancel_in_flight_relax(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.mode = "jogging"
    loop._pending_release = "disconnect"
    goal = {"gripper": 12.0}
    loop._slew_goal = goal

    with pytest.raises(RuntimeError, match="live jog"):
        loop._handle(Command("jog", {"joints": {"gripper": 1.0}, "live": True}))

    assert loop.mode == "jogging"
    assert loop._pending_release == "disconnect"
    assert loop._slew_goal is goal
    loop.follower.send_pose.assert_not_called()


def test_disconnect_relaxes_when_follower_is_controllable(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.mode = "idle"
    loop.hold_when_idle = True
    loop.joints = {name: 0.0 for name in JOINT_ORDER}
    reply: queue.Queue[dict] = queue.Queue(maxsize=1)

    loop._handle(Command("disconnect_robot", {}, reply))

    assert loop.mode == "jogging"
    assert loop._pending_release == "disconnect"
    loop.follower.disconnect.assert_not_called()

    loop._slew_t0 -= loop._slew_duration + 1.0
    loop._tick_jog()

    loop.follower.disconnect.assert_called_once_with()
    assert loop.mode == "offline"
    assert loop._pending_release is None
    assert reply.get(timeout=1) == {"ok": True}


def test_disconnect_skips_relax_when_follower_is_not_controllable(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.mode = "offline"

    result = _dispatch(loop, "disconnect_robot")

    assert result == {"ok": True}
    loop.follower.disconnect.assert_called_once_with()
    loop.follower.send_pose.assert_not_called()
    assert loop.mode == "offline"
    assert loop._pending_release is None


def test_disconnect_releases_directly_if_relax_send_fails(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.mode = "idle"
    loop.hold_when_idle = True
    loop.joints = {name: 0.0 for name in JOINT_ORDER}
    reply: queue.Queue[dict] = queue.Queue(maxsize=1)
    loop._handle(Command("disconnect_robot", {}, reply))
    assert loop.mode == "jogging"

    loop.follower.send_pose.side_effect = RuntimeError("bus write failed")
    loop._tick()

    loop.follower.disconnect.assert_called_once_with()
    assert loop.mode == "offline"
    assert loop._pending_release is None
    assert reply.get(timeout=1) == {"ok": True}


@pytest.mark.parametrize("kind,payload", [("record_start", {}), ("teleop_start", {"auto_record": True})])
def test_stop_during_recorder_open_discards_unpublished_writer(
    tmp_path: Path,
    monkeypatch,
    kind: str,
    payload: dict[str, object],
) -> None:
    loop = _loop(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    recorder = SimpleNamespace(
        dataset_id="new-session",
        session_id="new-session",
        episode_index=0,
        kind="record",
        close=MagicMock(),
    )

    def open_recorder(_kind: str, _payload: dict[str, object]):
        entered.set()
        release.wait(timeout=2)
        return recorder

    monkeypatch.setattr(loop, "_open_recorder", open_recorder)
    command = Command(kind, payload)
    worker = threading.Thread(target=loop._handle, args=(command,))
    worker.start()
    assert entered.wait(timeout=1)
    loop.request_stop()
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    recorder.close.assert_called_once()
    assert loop.writer is None
    assert loop.mode not in {"record", "teleop"}


def test_legacy_fps_sets_both_recording_rates(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    recorder = loop._open_recorder("record", {"name": "legacy", "fps": 7})
    try:
        assert recorder.action_fps == 7
        assert recorder.video_fps == 7
    finally:
        recorder.close()


def test_recording_deadlines_do_not_drift_or_backfill_actions(tmp_path: Path, monkeypatch) -> None:
    loop = _loop(tmp_path)
    writer = SimpleNamespace(
        action_fps=10,
        video_fps=20,
        action_frames=0,
        video_frames=0,
        video=False,
        dataset_id="timing",
        session_id="timing",
        episode_index=0,
        add_action=MagicMock(),
        add_video=MagicMock(),
    )
    loop._publish_recorder(writer, "record")
    loop._next_action_t = 0.0
    loop._next_video_t = 0.0
    samples = iter((0.0, 0.35))
    monkeypatch.setattr(loop_module.time, "perf_counter", lambda: next(samples))

    loop._maybe_record("record")
    loop._maybe_record("record")

    assert writer.add_action.call_count == 2
    assert writer.add_video.call_count == 2
    assert loop._next_action_t == pytest.approx(0.4)
    assert loop._next_video_t == pytest.approx(0.4)


def test_record_reset_finishes_episode_and_does_not_record_reset_frames(tmp_path: Path, monkeypatch) -> None:
    loop = _loop(tmp_path)
    loop.mode = "record"
    loop.writer = MagicMock()
    loop.episode_index = 0
    loop.episode_t0 = 0.0
    loop.episode_time_s = 1.0
    loop.reset_time_s = 0.5
    loop.num_episodes = 2
    monkeypatch.setattr(loop, "_tick_teleop", MagicMock())
    maybe_record = MagicMock()
    monkeypatch.setattr(loop, "_maybe_record", maybe_record)
    samples = iter((1.1, 1.3, 1.7))
    monkeypatch.setattr(loop_module.time, "perf_counter", lambda: next(samples))

    loop._tick_record()
    loop._tick_record()
    loop._tick_record()

    loop.writer.finish_episode.assert_called_once_with(0)
    assert maybe_record.call_count == 1
    assert loop.episode_index == 1
    assert loop._resetting is False


def test_resumed_record_num_episodes_is_relative_to_start_index(tmp_path: Path, monkeypatch) -> None:
    loop = _loop(tmp_path)
    writer = MagicMock()
    loop.writer = writer
    loop.mode = "record"
    loop.recording_start_index = 5
    loop.episode_index = 5
    loop.episode_t0 = 0.0
    loop.episode_time_s = 1.0
    loop.reset_time_s = 0.0
    loop.num_episodes = 2
    monkeypatch.setattr(loop, "_tick_teleop", MagicMock())
    monkeypatch.setattr(loop, "_maybe_record", MagicMock())
    close_writer = MagicMock()
    monkeypatch.setattr(loop, "_close_writer", close_writer)
    samples = iter((1.1, 2.2, 2.2))
    monkeypatch.setattr(loop_module.time, "perf_counter", lambda: next(samples))

    loop._tick_record()
    assert loop.episode_index == 6
    close_writer.assert_not_called()
    loop._tick_record()

    assert writer.finish_episode.call_args_list == [call(5), call(6)]
    close_writer.assert_called_once()


def test_rollout_policy_deadline_runs_at_policy_rate_without_drift(tmp_path: Path, monkeypatch) -> None:
    loop = _loop(tmp_path)
    loaded = SimpleNamespace(path="policy", task="", policy=MagicMock(), reset=MagicMock())
    engine = FakeInferenceEngine({"gripper": 1.0})
    monkeypatch.setattr(loop, "_start_inference_engine", lambda _loaded: (
        setattr(loop, "_inference_engine", engine) or True
    ))
    monkeypatch.setattr(loop, "_build_rollout_obs_frame", lambda observation: observation)
    monkeypatch.setattr(loop_module.time, "perf_counter", lambda: 0.0)
    assert loop._begin_rollout(loaded, {"record": False, "policy_fps": 15}, "policy")

    clock = iter((0.0, 1 / 30, 2 / 30, 3 / 30, 4 / 30))
    monkeypatch.setattr(loop_module.time, "perf_counter", lambda: next(clock))
    for _ in range(5):
        loop._tick_rollout()

    assert engine.get_action.call_count == 3
    assert loop.follower.send_pose.call_count == 3
    assert loop._next_policy_t == pytest.approx(0.2)


def test_rollout_prediction_reads_lerobot_rtc_queue(tmp_path: Path, monkeypatch) -> None:
    loop = _loop(tmp_path)
    loop.loaded_policy = SimpleNamespace(path="policy", task="", policy=MagicMock(), reset=MagicMock())
    loop.mode = "rollout"
    loop.task_t0 = 10.0
    loop.policy_fps = 20.0
    loop._next_policy_t = 0.0
    loop._next_prediction_t = 0.0
    loop._inference_engine = FakeInferenceEngine(
        {"gripper": 0.0},
        leftovers=[{"gripper": 1.0}, {"gripper": 2.0}],
    )
    monkeypatch.setattr(loop, "_build_rollout_obs_frame", lambda observation: observation)
    clock = iter((12.0, 12.25, 13.0, 13.25))
    monkeypatch.setattr(loop_module.time, "perf_counter", lambda: next(clock))

    loop._tick_rollout()
    first = loop._rollout_prediction

    assert first is not None
    assert first["id"] == 1
    assert first["t_s"] == 2.25
    assert first["step_s"] == 0.05
    assert first["strategy"] == "policy_queue"
    assert first["latency_ms"] == 0.0
    assert first["actions"] == [{"gripper": 1.0}, {"gripper": 2.0}]


def test_rollout_prediction_reads_sync_policy_queue(tmp_path: Path, monkeypatch) -> None:
    loop = _loop(tmp_path)
    policy = SimpleNamespace(_action_queue=deque([{"gripper": 1.0}, {"gripper": 2.0}]))
    loop.loaded_policy = SimpleNamespace(
        path="policy",
        task="",
        policy=policy,
        postprocessor=lambda action: action,
    )
    loop.mode = "rollout"
    loop.task_t0 = 10.0
    loop.policy_fps = 20.0
    loop._next_policy_t = 0.0
    loop._next_prediction_t = 0.0
    loop._inference_engine = FakeInferenceEngine({"gripper": 0.0})
    monkeypatch.setattr(loop, "_build_rollout_obs_frame", lambda observation: observation)
    clock = iter((12.0, 12.25))
    monkeypatch.setattr(loop_module.time, "perf_counter", lambda: next(clock))

    loop._tick_rollout()

    assert loop._rollout_prediction is not None
    assert loop._rollout_prediction["strategy"] == "policy_queue"
    assert loop._rollout_prediction["actions"] == [{"gripper": 1.0}, {"gripper": 2.0}]


def test_start_inference_engine_uses_hw_features_method(tmp_path: Path, monkeypatch) -> None:
    loop = _loop(tmp_path)
    loaded = SimpleNamespace(task="pick cube", policy=MagicMock())
    engine = MagicMock()
    engine.failed = False
    config = SimpleNamespace(type="rtc")
    captured: dict[str, object] = {}

    monkeypatch.setattr(loop_module, "inference_config_from_extra", lambda _extra: config)
    monkeypatch.setattr(loop, "_rollout_hw_features", lambda: {"observation.state": {"names": []}})

    def fake_create(_loaded, **kwargs):
        captured.update(kwargs)
        return engine

    monkeypatch.setattr(loop_module, "create_monitor_inference_engine", fake_create)

    assert loop._start_inference_engine(loaded) is True
    engine.reset.assert_called_once_with()
    engine.start.assert_called_once_with()
    engine.resume.assert_called_once_with()
    assert captured["hw_features"] == {"observation.state": {"names": []}}


def test_rollout_legacy_fps_is_policy_rate_and_is_capped_to_control(tmp_path: Path) -> None:
    loop = _loop(tmp_path)

    assert loop._policy_rates({"fps": 15}) == (15.0, 15.0)
    assert loop._policy_rates({"fps": 120}) == (120.0, 30.0)
    with pytest.raises(ValueError, match="policy_fps must be positive"):
        loop._policy_rates({"fps": 0})


def test_ui_log_handler_keeps_traceback_and_is_removed_on_stop(tmp_path: Path, monkeypatch) -> None:
    loop = _loop(tmp_path)
    monkeypatch.setattr(loop, "_run", lambda: None)

    loop.start()
    handler = loop._ui_log_handler
    assert handler is not None
    try:
        try:
            raise RuntimeError("serial transport failed")
        except RuntimeError:
            logging.getLogger("external.transport").exception("camera callback failed")
        rendered = "\n".join(entry["message"] for entry in loop.logs)
        assert "camera callback failed" in rendered
        assert "Traceback (most recent call last)" in rendered
        assert "RuntimeError: serial transport failed" in rendered
    finally:
        loop.stop()

    assert handler not in logging.getLogger().handlers
    assert loop._ui_log_handler is None


def test_ui_log_handlers_share_and_cautiously_restore_root_level(tmp_path: Path, monkeypatch) -> None:
    root = logging.getLogger()
    original_level = root.level
    first = _loop(tmp_path / "first")
    second = _loop(tmp_path / "second")
    monkeypatch.setattr(first, "_run", lambda: None)
    monkeypatch.setattr(second, "_run", lambda: None)
    root.setLevel(logging.ERROR)
    try:
        first.start()
        first_handler = first._ui_log_handler
        second.start()
        second_handler = second._ui_log_handler
        assert root.level == logging.INFO
        assert first_handler in root.handlers
        assert second_handler in root.handlers

        first.stop()
        assert first_handler not in root.handlers
        assert second_handler in root.handlers
        assert root.level == logging.INFO

        second.stop()
        assert second_handler not in root.handlers
        assert root.level == logging.ERROR

        first.start()
        root.setLevel(logging.DEBUG)
        first.stop()
        assert root.level == logging.DEBUG

        first.start()
        assert root.level == logging.DEBUG
        root.setLevel(logging.ERROR)
        second.start()
        assert root.level == logging.ERROR
        first.stop()
        second.stop()
        assert root.level == logging.ERROR
    finally:
        first.stop()
        second.stop()
        root.setLevel(original_level)
