import io
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
def test_task_start_requires_connected_leader_without_auto_connect(
    tmp_path: Path,
    kind: str,
) -> None:
    loop = _loop(tmp_path)
    loop.leader.connected = False

    with pytest.raises(RuntimeError, match="requires a connected leader arm"):
        loop._handle(Command(kind, {}))

    loop.leader.connect.assert_not_called()
    assert loop.writer is None
    assert loop.mode != "teleop"
    assert loop.mode != "record"


@pytest.mark.parametrize(
    "kind,payload,message",
    [
        ("jog", {"joints": {"gripper": 1.0}}, "requires a connected follower arm"),
        ("resume", {}, "requires a connected follower arm"),
        ("read_pose", {}, "requires a connected follower arm"),
        ("rollout_start", {"policy_path": "fake/policy"}, "requires a connected follower arm"),
        ("read_leader", {}, "requires a connected leader arm"),
    ],
)
def test_control_commands_do_not_auto_connect_devices(
    tmp_path: Path,
    kind: str,
    payload: dict[str, object],
    message: str,
) -> None:
    loop = _loop(tmp_path)
    loop.follower.connected = False
    loop.leader.connected = False

    with pytest.raises(RuntimeError, match=message):
        loop._handle(Command(kind, dict(payload)))

    loop.follower.connect.assert_not_called()
    loop.leader.connect.assert_not_called()


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


def test_live_jog_is_ignored_during_in_flight_relax(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.mode = "jogging"
    loop._pending_release = "disconnect"
    goal = {"gripper": 12.0}
    loop._slew_goal = goal
    reply: queue.Queue[dict] = queue.Queue(maxsize=1)

    loop._handle(Command("jog", {"joints": {"gripper": 1.0}, "live": True}, reply))

    assert reply.get(timeout=1) == {"ok": True, "ignored": True, "reason": "motion_in_progress"}
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


def test_force_disconnect_skips_relax_and_releases_both_buses(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop._park_relax_blocking = MagicMock()
    loop.leader.connected = True

    result = _dispatch(loop, "force_disconnect")

    assert result == {"ok": True, "role": "all"}
    loop._park_relax_blocking.assert_not_called()
    loop.follower.disconnect.assert_called_once_with()
    loop.leader.disconnect.assert_called_once_with()
    assert loop.mode == "offline"


def test_force_hardware_apply_only_signals_when_one_is_active(tmp_path: Path) -> None:
    loop = _loop(tmp_path)

    assert loop.force_current_hardware_apply() is False
    assert loop._hardware_apply_force.is_set() is False

    loop._hardware_apply_active.set()
    assert loop.force_current_hardware_apply() is True
    assert loop._hardware_apply_force.is_set() is True


def test_force_disconnect_leader_only_keeps_arm_connected(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.leader.connected = True

    result = _dispatch(loop, "force_disconnect", {"role": "leader"})

    assert result == {"ok": True, "role": "leader"}
    loop.follower.disconnect.assert_not_called()
    loop.leader.disconnect.assert_called_once_with()


def test_force_stop_detaches_engine_without_waiting(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.mode = "rollout"
    engine = MagicMock()
    loop._inference_engine = engine
    loop.loaded_policy = SimpleNamespace(path="policy")

    result = _dispatch(loop, "force_stop")

    assert result == {"ok": True, "stopped": "rollout"}
    assert loop._inference_engine is None
    assert loop.loaded_policy is None
    assert loop.mode == "idle"
    deadline = time.monotonic() + 1.0
    while not engine.stop.called and time.monotonic() < deadline:
        time.sleep(0.01)
    engine.stop.assert_called_once_with()


def test_rollout_aborts_when_follower_disconnects(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.mode = "rollout"
    loop.follower.connected = False
    engine = MagicMock()
    loop._inference_engine = engine
    loop.loaded_policy = SimpleNamespace(path="policy")

    loop._tick()

    assert loop._inference_engine is None
    assert loop.loaded_policy is None
    assert loop.mode == "offline"
    assert loop.last_error == "follower disconnected"
    engine.stop.assert_called_once_with()


def test_stop_during_recorder_open_discards_unpublished_writer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    kind = "teleop_start"
    payload = {"auto_record": True}
    loop = _loop(tmp_path)
    loop.leader.connected = True
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


def test_encoder_thread_setting_reaches_episode_writer(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    recorder = loop._open_recorder(
        "record", {"name": "threaded", "streaming_encoding": True, "encoder_threads": 6}
    )
    try:
        episode = recorder._ensure_episode(0)
        assert episode.streaming_encoding is True
        assert episode.encoder_threads == 6
    finally:
        recorder.close()
    with pytest.raises(ValueError, match="encoder_threads"):
        loop._open_recorder("record", {"name": "invalid", "encoder_threads": 0})


def test_recording_camera_work_does_not_block_control_thread(tmp_path: Path) -> None:
    from lerobot_monitor.recording_worker import RecordingWorker

    loop = _loop(tmp_path)
    camera_entered = threading.Event()
    release_camera = threading.Event()
    camera_threads: list[str] = []

    def slow_camera() -> dict[str, object]:
        camera_threads.append(threading.current_thread().name)
        camera_entered.set()
        assert release_camera.wait(timeout=5)
        return {}

    loop.cameras.latest_main_bgr_map.side_effect = slow_camera
    loop.cameras.latest_bgr_map.return_value = {}
    recorder = loop._open_recorder("record", {"name": "isolated", "streaming_encoding": False})
    loop._publish_recorder(recorder, "record")
    assert isinstance(loop.writer, RecordingWorker)
    loop.episode_t0 = time.perf_counter()
    loop._maybe_record("record")
    try:
        assert camera_entered.wait(timeout=2)
        loop._next_action_t = 0
        loop._next_video_t = 0
        started = time.perf_counter()
        loop._maybe_record("record")
        assert time.perf_counter() - started < 0.2
    finally:
        release_camera.set()
        loop._close_writer()
    assert camera_threads == ["recording-worker", "recording-worker"]


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
    session = MagicMock()
    session.camera_keys = {}
    session.fps = 5
    session.progress.return_value = {"error": None}
    loop.record_session = session
    loop.record_phase = "recording"
    loop.record_attempt = "attempt-0"
    loop._record_first_sample_t = 0.0
    loop.record_phase_t0 = 0.0
    loop.episode_time_s = 1.0
    loop.reset_time_s = 0.5
    loop.num_episodes = 2
    loop.leader.get_action_pose.return_value = {joint: 0.0 for joint in JOINT_ORDER}
    monkeypatch.setattr(loop_module.time, "perf_counter", lambda: 1.1)

    loop._tick_record()
    loop._tick_record()

    session.seal.assert_called_once_with("attempt-0")
    assert session.add_sample.call_count == 1
    assert loop.record_phase == "resetting"
    assert loop.record_completed == 1
    assert loop.record_pending_attempt == "attempt-0"


def test_resumed_record_num_episodes_is_relative_to_start_index(tmp_path: Path, monkeypatch) -> None:
    loop = _loop(tmp_path)
    session = MagicMock()
    session.new_attempt.return_value = "attempt-6"
    loop.record_session = session
    loop.mode = "record"
    loop.record_phase = "recording"
    loop.record_base_index = 5
    loop.record_attempt = "attempt-5"
    loop.episode_time_s = 1.0
    loop.reset_time_s = 0.0
    loop.num_episodes = 2

    loop._finish_record_episode()
    loop._start_record_episode()
    assert loop.episode_index == 6
    loop._finish_record_episode()

    assert session.seal.call_args_list == [call("attempt-5"), call("attempt-6")]
    assert session.accept.call_args_list == [call("attempt-5"), call("attempt-6")]
    session.stop.assert_called_once()
    assert loop.record_completed == 2


def test_record_controls_pause_rewind_and_stop_are_versioned(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    session = MagicMock()
    session.session_id = "session-1"
    session.dataset_id = "selected-dataset"
    session.progress.return_value = {"status": "ready", "saved": 0, "queued": 0, "error": None}
    session.new_attempt.side_effect = ["attempt-1", "attempt-2"]
    loop.record_session = session
    loop.mode = "record"
    loop.record_phase = "resetting"
    loop.num_episodes = 2
    loop.joints = {joint: 0.0 for joint in JOINT_ORDER}

    def control(action: str, operation: str, version: int) -> dict:
        return _dispatch(loop, f"record_{action}", {
            "session_id": "session-1", "operation_id": operation, "version": version,
        })

    started = control("next", "start", 0)
    assert started["record"]["phase"] == "recording"
    assert started["record"]["episode_number"] == 1
    assert control("next", "start", 0)["record"]["episode_number"] == 1
    paused = control("pause", "pause", started["record"]["version"])
    assert paused["record"]["paused"] is True
    session.add_sample.assert_not_called()
    rewound = control("back", "back", paused["record"]["version"])
    assert rewound["record"]["phase"] == "resetting"
    session.seal.assert_called_once_with("attempt-1", discard=True)
    restarted = control("next", "restart", rewound["record"]["version"])
    assert restarted["record"]["episode_number"] == 1
    stopped = control("stop", "stop", restarted["record"]["version"])
    assert stopped["record"]["phase"] == "finalizing"
    session.seal.assert_called_with("attempt-2", discard=True)
    session.stop.assert_called_once()
    assert loop.mode == "idle"
    assert control("stop", "stop", restarted["record"]["version"])["record"]["phase"] == "finalizing"


def test_estop_keeps_interrupted_record_attempt_out_of_dataset(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    session = MagicMock()
    loop.record_session = session
    loop.record_attempt = "interrupted"
    loop.mode = "record"
    loop._apply_estop()
    session.seal.assert_called_once_with("interrupted", discard=False)
    session.accept.assert_not_called()
    session.stop.assert_called_once_with(retain_unpublished=True)
    assert loop.mode == "estop"


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

    clock = [0.0]
    monkeypatch.setattr(loop_module.time, "perf_counter", lambda: clock[0])
    for step in range(5):
        clock[0] = step / 30
        loop._tick_rollout()

    assert engine.get_action.call_count == 3
    assert loop.follower.send_pose.call_count == 5
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
    clock = [12.0]
    monkeypatch.setattr(loop_module.time, "perf_counter", lambda: clock[0])

    loop._tick_rollout()
    first = loop._rollout_prediction

    assert first is not None
    assert first["id"] == 1
    assert first["t_s"] == 2.0
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
    clock = [12.0]
    monkeypatch.setattr(loop_module.time, "perf_counter", lambda: clock[0])

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


def test_rollout_legacy_fps_is_policy_rate_without_silent_cap(tmp_path: Path) -> None:
    loop = _loop(tmp_path)

    assert loop._policy_rates({"fps": 15}) == (15.0, 15.0)
    assert loop._policy_rates({"fps": 120}) == (120.0, 120.0)
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


def test_ui_log_handler_quiets_third_party_transfer_loggers(tmp_path: Path, monkeypatch) -> None:
    loop = _loop(tmp_path)
    monkeypatch.setattr(loop, "_run", lambda: None)

    loop.start()
    try:
        logging.getLogger("httpx").info("HTTP Request: HEAD https://huggingface.co/api/datasets/x 200 OK")
        logging.getLogger("huggingface_hub.file_download").info("Downloading bytes: 42%")
        logging.getLogger("httpx").warning("connection reset by peer")
        logging.getLogger("external.tool").info("real app message")
        rendered = "\n".join(str(entry["message"]) for entry in loop.logs)
        assert "HTTP Request: HEAD" not in rendered
        assert "Downloading bytes" not in rendered
        assert "connection reset by peer" in rendered
        assert "real app message" in rendered
    finally:
        loop.stop()


def test_stdio_log_capture_keeps_only_the_last_progress_redraw(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    stream = io.StringIO()
    capture = loop_module._StdioToLog(loop, stream)

    capture.write("Downloading 10%\r")
    capture.write("Downloading 60%\r")
    capture.write("Downloading 100%\n")
    rendered = "\n".join(str(entry["message"]) for entry in loop.logs)
    assert rendered.count("Downloading") == 1
    assert "Downloading 100%" in rendered
    # The real stream still receives every redraw untouched.
    assert stream.getvalue() == "Downloading 10%\rDownloading 60%\rDownloading 100%\n"


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


def test_live_jog_speed_cap_steps_toward_target(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.joints = {name: 0.0 for name in JOINT_ORDER}

    result = _dispatch(
        loop,
        "jog",
        {
            "joints": {"gripper": 50.0},
            "live": True,
            "source": "manual",
            "max_speed": 10.0,
        },
    )

    assert result["ok"] is True
    assert result["smoothed"] is True
    assert loop.mode == "jogging"
    assert loop._live_control is True

    loop._live_last_t -= 1.0
    loop._tick_live_jog()
    assert loop.follower.send_pose.call_args.args[0]["gripper"] == pytest.approx(10.0, abs=0.01)
    assert loop.mode == "jogging"

    loop._live_last_t -= 1.0
    loop._tick_live_jog()
    assert loop.follower.send_pose.call_args.args[0]["gripper"] == pytest.approx(20.0, abs=0.02)
    assert loop.mode == "jogging"

    loop._live_last_t -= 10.0
    loop._tick_live_jog()
    assert loop.follower.send_pose.call_args.args[0]["gripper"] == pytest.approx(50.0)
    assert loop.mode == "idle"
    assert loop._live_control is False


def test_leader_jog_relays_pose_and_publishes_telemetry(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.leader.connected = True
    pose = {name: 0.0 for name in JOINT_ORDER}
    pose["gripper"] = 12.0
    loop.leader.get_action_pose.return_value = pose
    loop.joints = {name: 0.0 for name in JOINT_ORDER}

    result = _dispatch(
        loop,
        "jog",
        {"joints": {}, "live": True, "source": "leader"},
    )

    assert result["ok"] is True
    assert result["source"] == "leader"
    loop.follower.send_pose.assert_called_with(pose)
    assert loop.snapshot()["leader_joints"] == pose


def test_leader_jog_requires_manual_leader_connection(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.leader.connected = False
    loop.joints = {name: 0.0 for name in JOINT_ORDER}

    with pytest.raises(RuntimeError, match="leader"):
        loop._handle(
            Command(
                "jog",
                {"joints": {}, "live": True, "source": "leader"},
            )
        )

    loop.follower.send_pose.assert_not_called()


def test_task_stop_clears_live_jog_target(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.joints = {name: 0.0 for name in JOINT_ORDER}
    _dispatch(
        loop,
        "jog",
        {
            "joints": {"gripper": 50.0},
            "live": True,
            "source": "manual",
            "max_speed": 10.0,
        },
    )
    assert loop._live_control is True

    result = _dispatch(loop, "task_stop")

    assert result["ok"] is True
    assert loop._live_control is False
    assert loop._live_target is None
    assert loop.mode == "idle"


def test_snapshot_reports_slew_motion_lock(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.mode = "jogging"
    loop._live_control = False
    assert loop.snapshot()["motion_locked"] is True

    loop._live_control = True
    assert loop.snapshot()["motion_locked"] is False
