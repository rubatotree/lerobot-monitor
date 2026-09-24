"""Exclusive control thread: the only code that talks to the Feetech buses."""

from __future__ import annotations

import contextlib
import logging
import math
import queue
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TextIO

from .cameras import CameraHub
from .config import MonitorConfig
from .hardware import apply_hardware_preset
from .leader import LeaderArm
from .library import DatasetRecorder, VideoLibrary
from .policy import (
    ActionChunk,
    LoadedPolicy,
    create_monitor_inference_engine,
    inference_leftover_poses,
    inference_config_from_extra,
    load_policy,
    pose_from_action_tensor,
    predict_action_chunk,
)
from .recording_worker import RecordingWorker
from .record_dataset import RecordDatasetSession, RecordSample, recover_record_publish
from .robot import FollowerArm
from .store import JsonStore
from .thread_priority import set_current_thread_priority
from .types import JOINT_ORDER, RELAX_POSE, lerp_pose, merge_partial
from .virtual_follower import VIRTUAL_PORT

logger = logging.getLogger(__name__)
_LOG_SKIP_PREFIXES = ("uvicorn.access", "lerobot_monitor")
# Third-party transfer libraries log every request and progress tick at INFO
# ("HTTP Request: HEAD https://huggingface.co/..."). Only warnings and errors
# from these loggers belong in the monitor log.
_LOG_QUIET_PREFIXES = (
    "httpcore",
    "httpx",
    "urllib3",
    "filelock",
    "fsspec",
    "huggingface_hub",
    "hf_xet",
    "datasets",
)
# Policy construction mutates process-global stdout, environment, and HF/torch
# caches. Serializing only output capture still lets two loaders corrupt those
# globals, including when separate ControlLoop instances exist in one process.
_POLICY_LOAD_LOCK = threading.Lock()
# Inference can retain policy state and mutate process-global torch/HF state just
# like loading. The order is always inference -> load to avoid a lock inversion.
_POLICY_INFER_LOCK = threading.Lock()
_UI_LOG_LOCK = threading.Lock()
_UI_LOG_HANDLERS: set[logging.Handler] = set()
_UI_LOG_PREVIOUS_ROOT_LEVEL: int | None = None
_UI_LOG_CHANGED_ROOT_LEVEL = False


def _step_pose_toward(
    current: dict[str, float],
    target: dict[str, float],
    max_delta: float,
) -> dict[str, float]:
    """Move each joint toward its target by at most ``max_delta`` units."""
    step: dict[str, float] = {}
    for name in JOINT_ORDER:
        if name not in target:
            continue
        current_value = float(current.get(name, target[name]))
        delta = float(target[name]) - current_value
        if abs(delta) <= max_delta:
            step[name] = float(target[name])
        else:
            step[name] = current_value + math.copysign(max_delta, delta)
    return step


class _UiLogHandler(logging.Handler):
    """Forward third-party loggers (policy load, HF download) into the UI log."""

    def __init__(self, loop: ControlLoop) -> None:
        super().__init__(level=logging.INFO)
        self.loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        name = record.name
        if any(name.startswith(prefix) for prefix in _LOG_SKIP_PREFIXES):
            return
        if record.levelno < logging.WARNING and any(
            name.startswith(prefix) for prefix in _LOG_QUIET_PREFIXES
        ):
            return
        try:
            # Formatter.format() appends exc_info/stack_info; getMessage() loses
            # the traceback that is most useful for diagnosing Windows failures.
            message = self.format(record).strip()
        except Exception:
            self.handleError(record)
            return
        if not message:
            return
        level = "error" if record.levelno >= logging.ERROR else "info"
        self.loop.log(level, message, echo=False)


class _StdioToLog:
    def __init__(self, loop: ControlLoop, stream: TextIO) -> None:
        self.loop = loop
        self.stream = stream
        self._buf = ""

    def write(self, text: str) -> int:
        self.stream.write(text)
        # Progress bars redraw with a bare carriage return; buffering the text
        # before the last CR would glue every redraw into one huge log line.
        self._buf += text.rsplit("\r", 1)[-1]
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if line:
                self.loop.log("info", line, echo=False)
        return len(text)

    def flush(self) -> None:
        self.stream.flush()
        leftover = self._buf.strip()
        self._buf = ""
        if leftover:
            self.loop.log("info", leftover, echo=False)


def _precise_sleep(seconds: float) -> None:
    try:
        from lerobot.utils.robot_utils import precise_sleep

        precise_sleep(seconds)
    except Exception:
        time.sleep(max(0.0, seconds))


@dataclass
class Command:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    reply: queue.Queue[dict[str, Any]] | None = None


@dataclass
class _PolicyLoadJob:
    """One rollout load attempt; only the current generation may consume it."""

    generation: int
    payload: dict[str, Any]
    thread: threading.Thread | None = None
    result: LoadedPolicy | None = None
    error: str | None = None


class ControlLoop:
    def __init__(
        self,
        config: MonitorConfig,
        cameras: CameraHub,
        follower: FollowerArm,
        leader: LeaderArm,
        on_snapshot: Callable[[dict[str, Any]], None] | None = None,
        store: JsonStore | None = None,
    ) -> None:
        self.config = config
        self.cameras = cameras
        self.follower = follower
        self.leader = leader
        self.on_snapshot = on_snapshot
        self.store = store

        self.mode = "offline"
        self.hold_when_idle = config.control.hold_when_idle
        self.latched: dict[str, float] = {}
        self.joints: dict[str, float] = {}
        self.leader_joints: dict[str, float] = {}
        self.action: dict[str, float] = {}
        self.loop_fps = 0.0
        self.last_error: str | None = None
        self.logs: list[dict[str, str | int]] = []
        self._log_sequence = 0

        self._slew_start: dict[str, float] | None = None
        self._slew_goal: dict[str, float] | None = None
        self._slew_t0 = 0.0
        self._slew_duration = config.control.jog_duration_s
        self._live_target: dict[str, float] | None = None
        self._live_max_speed = 0.0
        self._live_last_t = 0.0
        self._live_control = False

        self.writer: DatasetRecorder | RecordingWorker | None = None
        self.record_kind: str | None = None
        self.episode_index = 0
        self.recording_start_index = 0
        self.episode_t0 = 0.0
        self.episode_time_s = config.recording.default_episode_time_s
        self.reset_time_s = config.recording.default_reset_time_s
        self.num_episodes = config.recording.default_num_episodes
        self._resetting = False
        self.record_session: RecordDatasetSession | None = None
        self.record_phase: str | None = None
        self.record_paused = False
        self.record_auto_next = False
        self.record_attempt: str | None = None
        self.record_pending_attempt: str | None = None
        self.record_completed = 0
        self.record_base_index = 0
        self.record_elapsed = 0.0
        self.record_phase_t0 = 0.0
        self.record_version = 0
        self.record_speed = 30.0
        self.record_instant_fallback = False
        self.record_aligning = False
        self.record_fault: str | None = None
        self.record_motion_t = 0.0
        self.record_operations: set[str] = set()
        self.record_preparation: dict[str, Any] | None = None
        self._record_prepare_last_push = 0.0
        ui = store.ui() if store is not None else {}
        self.auto_record = bool(ui.get("auto_record"))
        self.selected_dataset_id: str | None = None
        self.pending: str | None = None
        self._cancel = threading.Event()
        self._log_lock = threading.Lock()
        self.rollout_extra: dict[str, str] = {}
        self.task_t0 = 0.0
        self.task_deadline: float | None = None
        self.loaded_policy: LoadedPolicy | None = None
        self.rollout_task = ""
        self.policy_fps = float(config.rollout.default_fps)
        self.effective_policy_fps = min(self.policy_fps, float(config.control.fps))
        self._policy_interval = 1.0 / max(1.0, self.effective_policy_fps)
        self._next_policy_t = 0.0
        # Latest rollout action-chunk preview for the charts (telemetry only).
        self._rollout_prediction: dict[str, Any] | None = None
        self._prediction_sequence = 0
        self._inference_engine: Any | None = None
        self._rollout_hw_feature_spec: dict = {}
        self._next_prediction_t = 0.0
        self._prediction_interval = 0.5
        self._policy_cache: dict[tuple[Any, ...], LoadedPolicy] = {}
        self._debug_lease_token: str | None = None
        self._policy_generation = 0
        self._policy_job: _PolicyLoadJob | None = None
        self._policy_job_lock = threading.Lock()
        self._start_generation = 0
        self._start_lock = threading.Lock()
        self.recording_mutation_lock = threading.Lock()
        self.library = VideoLibrary(config.videos_root())

        self._commands: queue.Queue[Command] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._action_interval = 1.0 / max(1, config.recording.action_fps)
        self._video_interval = 1.0 / max(1, config.recording.video_fps)
        self._next_action_t = 0.0
        self._next_video_t = 0.0
        self._last_bus_use = 0.0
        self._pending_release: str | None = None
        self._pending_release_reply: queue.Queue[dict[str, Any]] | None = None
        self._hardware_apply_active = threading.Event()
        self._hardware_apply_force = threading.Event()
        self._estop = threading.Event()
        self._ui_log_handler: logging.Handler | None = None
        self._virtual_power_enabled = bool(config.virtual_follower.enabled)
        self._virtual_model_id = str(config.virtual_follower.model_id or "so101")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        if self._ui_log_handler is None:
            handler = _UiLogHandler(self)
            handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
            self._attach_ui_log_handler(handler)
            self._ui_log_handler = handler
        self._thread = threading.Thread(target=self._run, name="control-loop", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._thread = None
            self._detach_ui_log_handler()
            raise

    def stop(self) -> None:
        self._stop.set()
        self._clear_debug_lease()
        try:
            thread = self._thread
            if thread is not None:
                thread.join(timeout=10.0)
                if not thread.is_alive():
                    self._thread = None
            self._end_rollout()
            try:
                self._close_writer()
            finally:
                try:
                    if self.follower.connected:
                        self.follower.disconnect()
                finally:
                    self.leader.disconnect()
        finally:
            self._detach_ui_log_handler()

    def _attach_ui_log_handler(self, handler: logging.Handler) -> None:
        global _UI_LOG_CHANGED_ROOT_LEVEL, _UI_LOG_PREVIOUS_ROOT_LEVEL

        root = logging.getLogger()
        with _UI_LOG_LOCK:
            if not _UI_LOG_HANDLERS:
                _UI_LOG_PREVIOUS_ROOT_LEVEL = root.level
                _UI_LOG_CHANGED_ROOT_LEVEL = root.level > logging.INFO
                if _UI_LOG_CHANGED_ROOT_LEVEL:
                    root.setLevel(logging.INFO)
            root.addHandler(handler)
            _UI_LOG_HANDLERS.add(handler)

    def _detach_ui_log_handler(self) -> None:
        global _UI_LOG_CHANGED_ROOT_LEVEL, _UI_LOG_PREVIOUS_ROOT_LEVEL

        handler = self._ui_log_handler
        if handler is None:
            return
        root = logging.getLogger()
        with _UI_LOG_LOCK:
            # Remove this exact instance only; other ControlLoops may be active.
            root.removeHandler(handler)
            _UI_LOG_HANDLERS.discard(handler)
            self._ui_log_handler = None
            if not _UI_LOG_HANDLERS:
                if (
                    _UI_LOG_CHANGED_ROOT_LEVEL
                    and _UI_LOG_PREVIOUS_ROOT_LEVEL is not None
                    and root.level == logging.INFO
                ):
                    root.setLevel(_UI_LOG_PREVIOUS_ROOT_LEVEL)
                _UI_LOG_PREVIOUS_ROOT_LEVEL = None
                _UI_LOG_CHANGED_ROOT_LEVEL = False

    def submit(self, kind: str, payload: dict[str, Any] | None = None, timeout: float = 8.0) -> dict[str, Any]:
        reply: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self._commands.put(Command(kind=kind, payload=payload or {}, reply=reply))
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            return {"ok": False, "error": f"command '{kind}' timed out"}

    def submit_nowait(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        self._commands.put(Command(kind=kind, payload=payload or {}, reply=None))

    def acquire_debug_lease(self, timeout: float = 8.0) -> dict[str, Any]:
        """Reserve control ownership for one read-only inference request."""
        return self.submit("debug_lease_acquire", timeout=timeout)

    def release_debug_lease(self, token: str, timeout: float = 8.0) -> dict[str, Any]:
        """Release a debug lease if it still belongs to ``token``."""
        return self.submit("debug_lease_release", {"token": str(token)}, timeout=timeout)

    def _clear_debug_lease(self) -> None:
        self._debug_lease_token = None

    def _clear_live_control(self) -> None:
        self._live_target = None
        self._live_max_speed = 0.0
        self._live_last_t = 0.0
        self._live_control = False

    def _read_leader_pose(self) -> dict[str, float]:
        if not self.leader.connected:
            raise RuntimeError("leader not connected")
        try:
            pose = self.leader.get_action_pose()
        except Exception as exc:  # noqa: BLE001
            self.leader.error = str(exc)
            self.leader_joints = {}
            with contextlib.suppress(Exception):
                self.leader.disconnect()
            raise
        self.leader_joints = dict(pose)
        return pose

    def _require_no_debug_lease(self, action: str) -> None:
        if self._debug_lease_token is not None:
            raise RuntimeError(f"cannot {action} while model debug is active")

    def request_estop(self) -> dict[str, Any]:
        """Disable torque immediately; do not wait for the control-queue tick."""
        self._estop.set()
        self._cancel.set()
        self._clear_debug_lease()
        with self._start_lock:
            self._start_generation += 1
        self._apply_estop()
        self._invalidate_policy_load(blocking=False)
        return {"ok": True}

    def request_stop(self) -> dict[str, Any]:
        """Abort a pending start immediately; queue a real stop if a task is running."""
        self._cancel.set()
        self._clear_debug_lease()
        with self._start_lock:
            self._start_generation += 1
        self._cancel_policy_load()
        pending = self.pending
        self.pending = None
        self.log("info", "stop requested")
        try:
            self._commands.put_nowait(Command(kind="task_stop", payload={}))
        except Exception:
            pass
        if self.on_snapshot is not None:
            self.on_snapshot(self.snapshot())
        return {"ok": True, "pending": pending, "mode": self.mode}

    def request_force_stop(self, timeout: float = 15.0) -> dict[str, Any]:
        """Detach the active task immediately instead of waiting for graceful shutdown."""
        self._cancel.set()
        with self._start_lock:
            self._start_generation += 1
        self._cancel_policy_load()
        self.pending = None
        return self.submit("force_stop", timeout=timeout)

    def force_current_hardware_apply(self) -> bool:
        """Ask an in-flight hardware preset to skip relax waits immediately."""
        if not self._hardware_apply_active.is_set():
            return False
        self._hardware_apply_force.set()
        return True

    def request_force_disconnect(
        self,
        role: str = "all",
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """Release the selected bus immediately, without a relax pose."""
        self._hardware_apply_force.set()
        return self.submit("force_disconnect", {"role": role}, timeout=timeout)

    def _start_token(self, payload: dict[str, Any]) -> int:
        token = payload.get("_start_generation")
        return self._start_generation if token is None else int(token)

    def _start_cancelled(self, token: int) -> bool:
        return self._cancel.is_set() or self._estop.is_set() or token != self._start_generation

    def _aborted(self, cmd: Command, label: str, token: int | None = None) -> bool:
        if not self._cancel.is_set() and not self._estop.is_set() and (
            token is None or token == self._start_generation
        ):
            return False
        self.pending = None
        self.log("info", f"{label} cancelled")
        self._reply(cmd, ok=True, cancelled=True)
        return True

    def _apply_estop(self) -> None:
        first = self.mode != "estop"
        if self.mode == "record" and self.record_session is not None:
            self._stop_record(preserve_current=True)
        self.mode = "estop"
        self._clear_debug_lease()
        self._clear_live_control()
        self.hold_when_idle = False
        self._slew_goal = None
        self._pending_release = None
        try:
            if self.follower.connected:
                self.follower.disable_torque()
                self._release_follower("estop")
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"E-STOP bus: {exc}")
            try:
                self.follower.disable_torque()
            except Exception:
                pass
        finally:
            self._end_rollout()
        try:
            self._close_writer()
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"E-STOP session: {exc}")
        if first:
            self.log("error", "E-STOP — torque disabled, serial released")

    def display_mode(self) -> str:
        if self._estop.is_set() or self.mode == "estop":
            return "estop"
        if self.pending:
            return "loading"
        if self._debug_lease_token is not None:
            return "debug"
        if self.mode == "idle" and self.follower.connected:
            return "hold" if self.hold_when_idle else "idle"
        if self.mode == "jogging":
            return "jog"
        return self.mode

    def bus_owner(self) -> str:
        if self._estop.is_set() or self.mode == "estop":
            return "estop"
        if self._debug_lease_token is not None:
            return "debug"
        if not self.follower.connected:
            return "free"
        return {
            "teleop": "teleop",
            "record": "record",
            "rollout": "rollout",
            "jogging": "jog",
            "idle": "hold" if self.hold_when_idle else "monitor",
            "offline": "free",
        }.get(self.mode, self.mode)

    def log(self, level: str, message: str, *, echo: bool = True) -> None:
        with self._log_lock:
            self._log_sequence += 1
            entry = {"seq": self._log_sequence, "level": level, "message": message, "t": time.strftime("%H:%M:%S")}
            self.logs.append(entry)
            self.logs = self.logs[-400:]
            writer = self.writer
        if writer is not None and not getattr(writer, "closed", False):
            try:
                log_path = Path(writer.root) / "run.log"
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(f"{entry['t']} {level} {message}\n")
            except OSError:
                pass
        if echo:
            logger.log(logging.ERROR if level == "error" else logging.INFO, message)
        if self.pending and self.on_snapshot is not None:
            self.on_snapshot(self.snapshot())

    def _remember_port(self, role: str, port: str) -> None:
        if self.store is None or not port:
            return
        ui = self.store.ui()
        hardware = dict(ui.get("hardware") or {})
        hardware[f"{role}_port"] = str(port)
        ui["hardware"] = hardware
        try:
            self.store.save_ui(ui)
        except OSError:
            pass

    @contextlib.contextmanager
    def _capture_task_output(self):
        stdout = _StdioToLog(self, sys.stdout)
        stderr = _StdioToLog(self, sys.stderr)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            yield

    def _policy_key(self, path: str, device: str, extra: dict[str, str]) -> tuple[Any, ...]:
        return (path, device, tuple(sorted(extra.items())))

    def _get_or_load_policy(self, path: str, device: str, task: str, extra: dict[str, str]) -> LoadedPolicy:
        with _POLICY_LOAD_LOCK:
            key = self._policy_key(path, device, extra)
            cached = self._policy_cache.get(key)
            if cached is not None:
                cached.task = task or cached.task
                return cached
            loaded = load_policy(
                path,
                device=device,
                task=task,
                robot_type=self.config.robot.type,
                rename_map=self.config.rollout.rename_map,
                extra=extra,
            )
            self._policy_cache[key] = loaded
            return loaded

    def _cached_policy(self, path: str, device: str, extra: dict[str, str]) -> LoadedPolicy | None:
        with _POLICY_LOAD_LOCK:
            return self._policy_cache.get(self._policy_key(path, device, extra))

    def infer_action_chunk(
        self,
        *,
        path: str,
        task: str,
        device: str,
        extra: dict[str, str],
        joints: dict[str, float],
        images_rgb: dict[str, Any],
        chunk_size: int,
    ) -> ActionChunk:
        """Load or reuse a policy and infer without touching the follower bus."""
        with _POLICY_INFER_LOCK, self._capture_task_output():
            loaded = self._get_or_load_policy(path, device, task, extra)
            return predict_action_chunk(loaded, joints, images_rgb, chunk_size)

    def _invalidate_policy_load(self, *, blocking: bool = True) -> bool:
        """Permanently detach the current loader without waiting for its thread."""
        acquired = self._policy_job_lock.acquire(blocking=blocking)
        if not acquired:
            return False
        try:
            self._policy_generation += 1
            self._policy_job = None
        finally:
            self._policy_job_lock.release()
        return True

    def _cancel_policy_load(self) -> None:
        """Linearize cancellation against rollout activation and first action."""
        self._cancel.set()
        with self._policy_job_lock:
            self._policy_generation += 1
            self._policy_job = None

    def _policy_worker(
        self,
        job: _PolicyLoadJob,
        path: str,
        device: str,
        task: str,
        extra: dict[str, str],
    ) -> None:
        try:
            with _POLICY_INFER_LOCK, self._capture_task_output():
                loaded = self._get_or_load_policy(path, device, task, extra)
            job.result = loaded
        except Exception as exc:  # noqa: BLE001
            job.error = str(exc)

    def _begin_rollout(self, loaded: LoadedPolicy, payload: dict[str, Any], path: str) -> bool:
        start_token = self._start_token(payload)
        if self._start_cancelled(start_token):
            self.pending = None
            self.log("info", "rollout cancelled")
            return False
        self._end_rollout()
        loaded.reset()
        loaded.task = str(payload.get("task") or loaded.task)
        self.loaded_policy = loaded
        self.rollout_task = loaded.task
        duration = float(payload.get("duration_s") or self.config.rollout.default_duration_s)
        self.task_t0 = time.perf_counter()
        self.task_deadline = None if duration <= 0 else self.task_t0 + duration
        self.policy_fps, self.effective_policy_fps = self._policy_rates(payload)
        self._policy_interval = 1.0 / self.effective_policy_fps
        self._next_policy_t = self.task_t0
        if payload.get("auto_record") is not None:
            self.auto_record = bool(payload.get("auto_record"))
        want_record = bool(payload.get("record")) if payload.get("record") is not None else self.auto_record
        recorder: DatasetRecorder | None = None
        if want_record:
            rec_payload = dict(payload)
            # Rollout `fps` is the legacy policy/inference frequency, never a
            # recording-rate alias. Recording uses only explicit dual rates or
            # RecordingConfig defaults.
            rec_payload.pop("fps", None)
            rec_payload.pop("policy_fps", None)
            rec_payload.setdefault("task", self.rollout_task)
            rec_payload.setdefault("name", Path(path).name or "rollout")
            recorder = self._open_recorder("rollout", rec_payload)
        with self._start_lock:
            cancelled = self._start_cancelled(start_token)
            if not cancelled:
                if recorder is not None:
                    self._publish_recorder(recorder, "rollout")
                    self.episode_t0 = self.task_t0
                self.pending = None
                self.mode = "rollout"
        if cancelled:
            if recorder is not None:
                recorder.close()
            self.pending = None
            self.log("info", "rollout cancelled")
            return False
        if not self._start_inference_engine(loaded):
            return False
        self._touch_bus()
        self.log("info", f"rollout started ({loaded.policy.__class__.__name__})")
        return True

    def _complete_rollout_load(self) -> None:
        with self._policy_job_lock:
            job = self._policy_job
            if job is None or job.thread is None or job.thread.is_alive():
                return
            if job.generation != self._policy_generation:
                self._policy_job = None
                return
            self._policy_job = None
        if self._cancel.is_set() or self._estop.is_set():
            self.pending = None
            self.log("info", "rollout cancelled")
            return
        if job.error:
            self.pending = None
            self.last_error = job.error
            self.log("error", f"policy load failed: {job.error}")
            return
        if job.result is None:
            self.pending = None
            return
        path = str(job.payload.get("policy_path") or "")
        self._begin_rollout(job.result, job.payload, path)

    def note_pending(self, kind: str, message: str) -> int:
        """Log and mark a task requested before the control thread picks it up."""
        with self._start_lock:
            self._start_generation += 1
            token = self._start_generation
            self._cancel.clear()
            self.pending = kind
        self.log("info", message)
        if self.on_snapshot is not None:
            self.on_snapshot(self.snapshot())
        return token

    def clear_pending(self, token: int | None = None) -> None:
        with self._start_lock:
            if token is not None and token != self._start_generation:
                return
            self.pending = None
            self.record_preparation = None
        if self.on_snapshot is not None:
            self.on_snapshot(self.snapshot())

    def update_record_preparation(self, dataset_id: str, step: str, done: int = 0, total: int = 0) -> None:
        self.record_preparation = {
            "dataset_id": dataset_id,
            "step": step,
            "done": done,
            "total": total,
        }
        now = time.perf_counter()
        if self.on_snapshot is not None and (now - self._record_prepare_last_push >= 0.25 or done == total):
            self._record_prepare_last_push = now
            self.on_snapshot(self.snapshot())

    def snapshot(self) -> dict[str, Any]:
        elapsed = time.perf_counter() - self.task_t0 if self.task_t0 else 0.0
        rec_elapsed = time.perf_counter() - self.episode_t0 if self.episode_t0 else 0.0
        owner = self.bus_owner()
        shown = self.display_mode()
        return {
            "ts": time.time(),
            "mode": self.mode,
            "display_mode": shown,
            "owner": owner,
            "live_control": self._live_control,
            "motion_locked": self.mode == "jogging" and not self._live_control,
            "fps": round(self.loop_fps, 1),
            "robot": self.follower.snapshot(),
            "leader": self.leader.snapshot(),
            "cameras": self.cameras.snapshots(),
            "joints": self.joints,
            "leader_joints": self.leader_joints,
            "goal": self.latched,
            "action": self.action,
            # Dashed overlay for the charts; only meaningful while a rollout runs.
            "prediction": self._rollout_prediction if self.mode == "rollout" else None,
            "hold": self.hold_when_idle,
            "task": {
                "kind": shown,
                "elapsed_s": round(elapsed, 2),
                "episode_index": self.episode_index,
                "episode_elapsed_s": round(rec_elapsed, 2) if self.writer else None,
                "episode_time_s": self.episode_time_s if self.writer else None,
                "reset_time_s": self.reset_time_s if self.writer and self.mode == "record" else None,
                "resetting": bool(getattr(self, "_resetting", False)),
                "recording": self.writer is not None,
                "auto_record": self.auto_record,
                "pending": self.pending,
                "session_id": None if self.writer is None else self.writer.session_id,
                "dataset_id": self.selected_dataset_id,
                "video_id": None if self.writer is None else self.writer.session_id,
                "num_episodes": self.num_episodes if self.writer else None,
                "policy_path": None if self.loaded_policy is None else self.loaded_policy.path,
                "policy_fps": self.policy_fps if self.loaded_policy is not None else None,
                "effective_policy_fps": self.effective_policy_fps if self.loaded_policy is not None else None,
                "task": self.rollout_task,
                "message": self.last_error,
                "record": self._record_snapshot(),
            },
            "logs": self.logs[-200:],
        }

    def _record_snapshot(self) -> dict[str, Any] | None:
        if self.pending == "record_start" and self.mode != "record" and self.record_preparation:
            return {"phase": "preparing", **self.record_preparation}
        session = self.record_session
        if session is None:
            return None
        progress = session.progress()
        now = time.perf_counter()
        elapsed = self.record_elapsed
        if self.mode == "record" and not self.record_paused:
            elapsed += max(0.0, now - self.record_phase_t0)
        phase = self.record_phase
        if phase == "finalizing" and progress["status"] in {"completed", "error"}:
            phase = progress["status"]
            self.record_phase = phase
        show_last = self.record_pending_attempt is not None or phase in {"finalizing", "completed", "error"}
        episode_number = max(1, self.record_completed) if show_last else self.record_completed + 1
        return {
            "session_id": session.session_id,
            "dataset_id": session.dataset_id,
            "phase": phase,
            "paused": self.record_paused,
            "version": self.record_version,
            "episode_index": self.record_base_index + episode_number - 1,
            "episode_number": episode_number,
            "completed": self.record_completed,
            "target": self.num_episodes,
            "elapsed_s": round(elapsed, 2),
            "duration_s": self.episode_time_s if phase == "recording" else self.reset_time_s,
            "auto_next": self.record_auto_next,
            "instant_fallback": self.record_instant_fallback,
            "pending": progress["queued"] - progress["saved"],
            "saved": progress["saved"],
            "save_status": progress["status"],
            "error": self.record_fault or progress["error"],
            "staging_path": progress.get("staging_path"),
            "needs_rerecord": self.record_fault is not None,
            "can_retry": progress.get("can_retry", False),
            "can_back": bool(self.record_attempt or self.record_pending_attempt),
        }

    def _record_control(self, payload: dict[str, Any]) -> bool:
        session = self.record_session
        if session is None:
            raise RuntimeError("not recording")
        operation = str(payload.get("operation_id") or "")
        if not operation or not payload.get("session_id") or payload.get("version") is None:
            raise ValueError("record control requires session_id, operation_id and version")
        if payload["session_id"] != session.session_id:
            raise ValueError("record session has changed")
        if operation in self.record_operations:
            return False
        if self.mode != "record":
            raise RuntimeError("not recording")
        if int(payload["version"]) != self.record_version:
            raise ValueError("record state changed; refresh before retrying")
        self.record_operations.add(operation)
        if len(self.record_operations) > 128:
            self.record_operations = {operation}
        self.record_version += 1
        return True

    def _set_record_phase(self, phase: str, *, paused: bool = False) -> None:
        self.record_phase = phase
        self.record_fault = None
        self.record_version += 1
        self.record_paused = paused
        self.record_elapsed = 0.0
        self.record_phase_t0 = time.perf_counter()
        self._reset_recording_deadlines(self.record_phase_t0)

    def _set_record_resume_speed(self, payload: dict[str, Any]) -> None:
        if payload.get("resume_speed") is None:
            return
        requested = float(payload["resume_speed"])
        if not math.isfinite(requested) or requested < 0 or requested > 720:
            raise ValueError("resume speed must be between 0 and 720 degrees per second")
        self.record_instant_fallback = requested == 0.0
        self.record_speed = 30.0 if self.record_instant_fallback else requested

    def _start_record_episode(self) -> None:
        if self.record_session is None:
            raise RuntimeError("record session is missing")
        if self.record_pending_attempt is not None:
            self.record_session.accept(self.record_pending_attempt)
            self.record_pending_attempt = None
        self.record_attempt = self.record_session.new_attempt()
        self.episode_index = self.record_base_index + self.record_completed
        self._set_record_phase("recording")
        self.record_aligning = True
        self.record_motion_t = time.perf_counter()

    def _finish_record_episode(self) -> None:
        if self.record_session is None or self.record_attempt is None:
            raise RuntimeError("no active episode")
        if self.record_fault:
            raise RuntimeError("this episode needs re-recording")
        if not self.record_session.has_samples(self.record_attempt):
            raise RuntimeError("wait for the first recorded frame before finishing this episode")
        self.record_session.seal(self.record_attempt)
        self.record_pending_attempt = self.record_attempt
        self.record_attempt = None
        self.record_completed += 1
        if self.record_completed >= self.num_episodes:
            self.record_session.accept(self.record_pending_attempt)
            self.record_pending_attempt = None
            self._stop_record()
        else:
            self._set_record_phase("resetting")

    def _stop_record(self, *, preserve_current: bool = False) -> None:
        if self.record_session is None:
            return
        if self.record_attempt is not None:
            self.record_session.seal(self.record_attempt, discard=not preserve_current)
            self.record_attempt = None
        if self.record_pending_attempt is not None:
            self.record_session.accept(self.record_pending_attempt)
            self.record_pending_attempt = None
        self.record_session.stop(retain_unpublished=preserve_current)
        self.record_phase = "finalizing"
        self.record_version += 1
        self.record_paused = False
        self.mode = "idle" if self.follower.connected else "offline"
        if self.joints:
            self.latched = dict(self.joints)
        self._touch_bus()

    def _pause_record_for_fault(self, message: str, now: float) -> None:
        if not self.record_paused:
            self.record_elapsed += max(0.0, now - self.record_phase_t0)
        self.record_paused = True
        self.record_fault = message
        self.last_error = message
        self.latched = dict(self.joints)
        self.record_version += 1

    def _reply(self, cmd: Command, **kwargs: Any) -> None:
        if cmd.reply is not None:
            cmd.reply.put(kwargs)

    def _close_writer(self) -> Path | None:
        if self.mode == "record" and self.record_session is not None:
            self._stop_record()
            return self.record_session.root
        if self.writer is None:
            return None
        writer = self.writer
        self.writer = None
        self.record_kind = None
        path = writer.close()
        self.log("info", f"dataset saved: {path}")
        return path

    def _publish_recorder(self, recorder: DatasetRecorder, kind: str) -> None:
        self.writer = RecordingWorker(recorder, self.cameras) if isinstance(recorder, DatasetRecorder) else recorder
        self.record_kind = kind
        self.selected_dataset_id = recorder.dataset_id
        self.episode_index = recorder.episode_index
        self.recording_start_index = recorder.episode_index
        self._action_interval = 1.0 / recorder.action_fps
        self._video_interval = 1.0 / recorder.video_fps
        self._reset_recording_deadlines()

    def _reset_recording_deadlines(self, now: float | None = None) -> None:
        start = time.perf_counter() if now is None else float(now)
        self._next_action_t = start
        self._next_video_t = start

    @staticmethod
    def _advance_deadline(deadline: float, interval: float, now: float) -> float:
        # The epsilon prevents a decimal deadline such as 0.35 from remaining
        # due because its binary quotient is represented as 6.999999999.
        missed = max(1, math.floor((now - deadline) / interval + 1e-9) + 1)
        return deadline + missed * interval

    def _recording_rates(self, payload: dict[str, Any]) -> tuple[int, int]:
        legacy = payload.get("fps")
        action_fps = int(
            payload.get("action_fps")
            if payload.get("action_fps") is not None
            else legacy if legacy is not None else self.config.recording.action_fps
        )
        video_fps = int(
            payload.get("video_fps")
            if payload.get("video_fps") is not None
            else legacy if legacy is not None else self.config.recording.video_fps
        )
        if action_fps <= 0 or video_fps <= 0:
            raise ValueError("action_fps and video_fps must be positive")
        control_limit = max(1, int(self.config.control.fps))
        if action_fps > control_limit:
            raise ValueError(f"action_fps={action_fps} exceeds control loop capacity ({control_limit} fps)")
        if video_fps > control_limit:
            raise ValueError(f"video_fps={video_fps} exceeds camera sampling capacity ({control_limit} fps)")
        return action_fps, video_fps

    def _policy_rates(self, payload: dict[str, Any]) -> tuple[float, float]:
        legacy = payload.get("fps")
        requested = float(
            payload.get("policy_fps")
            if payload.get("policy_fps") is not None
            else legacy if legacy is not None else self.config.rollout.default_fps
        )
        if not math.isfinite(requested) or requested <= 0:
            raise ValueError("policy_fps must be positive")
        control_limit = max(1.0, float(self.config.control.fps))
        return requested, min(requested, control_limit)

    def _open_recorder(self, kind: str, payload: dict[str, Any] | None = None) -> DatasetRecorder:
        self.recording_mutation_lock.acquire()
        try:
            recorder = self._open_recorder_unlocked(kind, payload)
            recorder._on_close = self.recording_mutation_lock.release
            return recorder
        except BaseException:
            self.recording_mutation_lock.release()
            raise

    def _open_recorder_unlocked(self, kind: str, payload: dict[str, Any] | None = None) -> DatasetRecorder:
        p = dict(payload or {})
        resume = bool(p.get("resume", False))
        dataset_id = str(p.get("video_id") or p.get("dataset_id") or "").strip()
        library = self.library
        requested_root = str(p.get("root") or "").strip()
        if requested_root:
            output_root = Path(requested_root).expanduser().resolve()
            if output_root.exists() and not output_root.is_dir():
                raise ValueError("recording root must be a directory")
            library = VideoLibrary(output_root)
        root: Path | None = None
        existing: dict[str, Any] = {}
        if resume:
            if not dataset_id:
                raise ValueError("resume requires video_id or dataset_id")
            try:
                existing = library.get(dataset_id)
            except ValueError as exc:
                raise ValueError(f"invalid resume target '{dataset_id}'") from exc
            except FileNotFoundError:
                raise FileNotFoundError(f"resume target '{dataset_id}' does not exist")
            root = Path(existing["path"])
            if p.get("action_fps") is None and p.get("fps") is None:
                p["action_fps"] = existing.get("action_fps") or existing.get("fps")
            if p.get("video_fps") is None and p.get("fps") is None:
                p["video_fps"] = existing.get("video_fps") or existing.get("fps")
            stored_format = str(existing.get("format") or self.config.recording.video_format)
            if p.get("format") is not None and str(p["format"]) != stored_format:
                raise ValueError(
                    f"resume format mismatch: dataset uses {stored_format}; requested {p['format']}"
                )
            p["format"] = stored_format

        action_fps, video_fps = self._recording_rates(p)
        video_format = str(p.get("format") or self.config.recording.video_format)
        merge = bool(p["merge"] if p.get("merge") is not None else self.config.recording.merge)
        encoder_threads = int(
            p["encoder_threads"]
            if p.get("encoder_threads") is not None
            else self.config.recording.encoder_threads
        )
        if not 1 <= encoder_threads <= 32:
            raise ValueError("encoder_threads must be between 1 and 32")
        extra = {
            "task": p.get("task") or existing.get("task") or "",
            "repo_id": p.get("repo_id") or existing.get("repo_id") or "",
            "kind": kind,
            "format": video_format,
            "streaming_encoding": bool(
                p["streaming_encoding"]
                if p.get("streaming_encoding") is not None
                else self.config.recording.streaming_encoding
            ),
            "deferred_encoding": bool(
                p["deferred_encoding"]
                if p.get("deferred_encoding") is not None
                else self.config.recording.deferred_encoding
            ),
            "encoder_threads": encoder_threads,
            "video": bool(
                p["video"]
                if p.get("video") is not None
                else existing.get("video", self.config.recording.video)
            ),
            "action_fps": action_fps,
            "video_fps": video_fps,
        }
        if root is not None:
            recorder = DatasetRecorder(
                root,
                action_fps=action_fps,
                video_fps=video_fps,
                kind=kind,
                extra_meta=extra,
                resume=True,
                video_format=video_format,
                merge=merge,
            )
        else:
            created = library.create(
                str(p.get("name") or p.get("repo_id") or p.get("task") or kind),
                fps=action_fps,
                action_fps=action_fps,
                video_fps=video_fps,
                task=str(p.get("task") or ""),
                repo_id=str(p.get("repo_id") or ""),
                extra=extra,
            )
            recorder = DatasetRecorder(
                Path(created["path"]),
                action_fps=action_fps,
                video_fps=video_fps,
                kind=kind,
                extra_meta=extra,
                resume=False,
                video_format=video_format,
                merge=merge,
            )
        return recorder

    def _maybe_record(self, kind: str) -> None:
        if self.writer is None:
            return
        now = time.perf_counter()
        elapsed = max(0.0, now - self.episode_t0)
        action_due = now >= self._next_action_t
        video_due = now >= self._next_video_t
        if not action_due and not video_due:
            return
        if action_due:
            if isinstance(self.writer, RecordingWorker):
                self.writer.add_action(
                    self.joints,
                    self.action or self.latched,
                    episode_index=self.episode_index,
                    kind=kind,
                    elapsed_s=elapsed,
                )
            else:
                self.writer.add_action(
                    self.joints,
                    self.action or self.latched,
                    episode_index=self.episode_index,
                    kind=kind,
                )
            self._next_action_t = self._advance_deadline(self._next_action_t, self._action_interval, now)
        if video_due:
            if isinstance(self.writer, RecordingWorker):
                self.writer.request_video(episode_index=self.episode_index, elapsed_s=elapsed)
            else:
                images: dict[str, Any] = {}
                if self.writer.video:
                    images = self.cameras.latest_main_bgr_map()
                    if not images:
                        images = self.cameras.latest_bgr_map()
                self.writer.add_video(images, episode_index=self.episode_index)
            self._next_video_t = self._advance_deadline(self._next_video_t, self._video_interval, now)

    def _run(self) -> None:
        set_current_thread_priority(1)
        if self.config.robot.auto_connect:
            self._connect_follower()
        if (
            not self.follower.connected
            and self._virtual_power_enabled
            and self.config.virtual_follower.auto_connect
        ):
            self._connect_virtual_follower()
        period = 1.0 / max(1.0, self.config.control.fps)
        fps_n = 0
        fps_t = time.perf_counter()
        while not self._stop.is_set():
            t0 = time.perf_counter()
            if self._estop.is_set() and self.mode != "estop":
                self._apply_estop()
            self._drain_commands()
            self._complete_rollout_load()
            self._tick()
            fps_n += 1
            now = time.perf_counter()
            if now - fps_t >= 1.0:
                self.loop_fps = fps_n / (now - fps_t)
                fps_n = 0
                fps_t = now
            if self.on_snapshot is not None:
                self.on_snapshot(self.snapshot())
            _precise_sleep(max(0.0, period - (time.perf_counter() - t0)))
        if self.follower.connected and self.mode != "estop":
            self._park_relax_blocking()
        self.leader.disconnect()
        self.follower.disconnect()

    def _touch_bus(self) -> None:
        self._last_bus_use = time.perf_counter()

    def _connect_follower(self) -> None:
        try:
            self.follower.connect()
            self._clear_debug_lease()
            pose = self.follower.get_pose()
            self.joints = dict(pose)
            if not self.latched:
                self.latched = dict(pose)
            self.mode = "idle"
            self.last_error = None
            self._touch_bus()
            self.log("info", f"acquired follower serial {self.config.robot.port}")
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self.follower.error = str(exc)
            self.mode = "offline"
            self.log("error", f"follower connect failed: {exc}")

    def _connect_virtual_follower(self) -> None:
        if not self._virtual_power_enabled:
            return
        if self.follower.connected and not self._follower_is_virtual():
            return
        try:
            self.follower.connect_virtual(
                model_id=self._virtual_model_id,
                robot_type=self.config.robot.type,
            )
            self._clear_debug_lease()
            pose = self.follower.get_pose()
            self.joints = dict(pose)
            if not self.latched:
                self.latched = dict(pose)
            self.mode = "idle"
            self.last_error = None
            self._touch_bus()
            self.log("info", f"virtual follower online ({self._virtual_model_id})")
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self.follower.error = str(exc)
            self.mode = "offline"
            self.log("error", f"virtual follower connect failed: {exc}")

    def _follower_is_virtual(self) -> bool:
        return self.follower.is_virtual is True

    def _require_follower_connected(self, action: str) -> None:
        if not self.follower.connected:
            error = f"{action} requires a connected follower arm; use Connect arm first"
            self.last_error = error
            raise RuntimeError(error)
        self._touch_bus()

    def _require_leader_connected(self, action: str) -> None:
        if not self.leader.connected:
            error = f"{action} requires a connected leader arm; use Connect leader first"
            self.last_error = error
            raise RuntimeError(error)

    def _release_follower(self, reason: str) -> None:
        self._clear_live_control()
        self._clear_debug_lease()
        if self.follower.connected:
            self.follower.disconnect()
        self._end_rollout()
        self._slew_goal = None
        self._slew_start = None
        self.mode = "estop" if reason == "estop" or self._estop.is_set() else "offline"
        self._pending_release = None
        self.log("info", f"released follower serial ({reason})")
        if (
            reason not in {"estop", "force_disconnect"}
            and self._virtual_power_enabled
            and self.config.virtual_follower.auto_connect
            and not self._estop.is_set()
        ):
            self._connect_virtual_follower()
        if self._pending_release_reply is not None:
            self._pending_release_reply.put({"ok": True})
            self._pending_release_reply = None

    def _relax_pose(self) -> dict[str, float]:
        if self.store is not None:
            saved = self.store.presets("pose").get("relax")
            if isinstance(saved, dict) and saved:
                return {name: float(saved[name]) for name in JOINT_ORDER if name in saved}
        return dict(RELAX_POSE)

    def _can_control_follower(self) -> bool:
        """Whether the loop currently owns a connected, actionable follower bus."""
        if (
            not self.follower.connected
            or self._estop.is_set()
            or self.mode == "estop"
            or self.pending is not None
        ):
            return False
        return self.bus_owner() in {"hold", "monitor", "teleop", "record", "rollout", "jog"}

    def _begin_relax_then_release(self, reason: str) -> None:
        if not self.follower.connected or self._pending_release:
            return
        if not self._can_control_follower():
            self._release_follower(reason)
            return
        self._clear_live_control()
        try:
            current = self.joints or self.follower.get_pose()
        except Exception:
            self._release_follower(reason)
            return
        target = merge_partial(current, self._relax_pose())
        self._slew_start = dict(current)
        self._slew_goal = target
        self._slew_t0 = time.perf_counter()
        self._slew_duration = max(self.config.control.jog_duration_s, 2.0)
        self._pending_release = reason
        self.mode = "jogging"
        self.log("info", f"relax pose before release ({reason})")

    def _park_relax_blocking(self) -> None:
        try:
            current = self.joints or self.follower.get_pose()
            goal = merge_partial(current, self._relax_pose())
            duration = max(self.config.control.jog_duration_s, 2.0)
            t0 = time.perf_counter()
            while True:
                if self._hardware_apply_force.is_set():
                    self.log("info", "relax wait interrupted by force request")
                    break
                alpha = (time.perf_counter() - t0) / max(duration, 1e-3)
                pose = lerp_pose(current, goal, alpha)
                self.follower.send_pose(pose)
                if alpha >= 1.0:
                    break
                _precise_sleep(1.0 / 30.0)
            self.log("info", "parked at relax")
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"relax park failed: {exc}")

    def _drain_commands(self) -> None:
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                if self._estop.is_set() and cmd.kind not in {
                    "resume",
                    "estop",
                    "task_stop",
                    "debug_lease_release",
                }:
                    self._reply(cmd, ok=False, error="estop")
                    continue
                self._handle(cmd)
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                self.log("error", f"{cmd.kind}: {exc}")
                self._reply(cmd, ok=False, error=str(exc))

    def _handle(self, cmd: Command) -> None:
        kind = cmd.kind
        p = cmd.payload
        if kind == "debug_lease_acquire":
            if self._debug_lease_token is not None:
                self._reply(cmd, ok=False, error="model debug is already active")
            elif self.pending is not None or self.writer is not None:
                self._reply(cmd, ok=False, error="model debug requires no pending task or recording")
            elif self.mode not in {"idle", "offline"}:
                self._reply(cmd, ok=False, error="model debug requires an idle control loop")
            else:
                self._debug_lease_token = uuid.uuid4().hex
                self._reply(cmd, ok=True, token=self._debug_lease_token)
        elif kind == "debug_lease_release":
            token = str(p.get("token") or "")
            released = bool(token and self._debug_lease_token == token)
            if released:
                self._clear_debug_lease()
            self._reply(cmd, ok=True, released=released)
        elif kind == "connect_robot":
            self._require_no_debug_lease("connect follower")
            requested_port = str(p.get("port") or "")
            if requested_port == VIRTUAL_PORT:
                self._virtual_power_enabled = True
                self._connect_virtual_follower()
                self._reply(cmd, ok=self.follower.connected, error=self.follower.error)
                return
            if p.get("port"):
                self.follower.config.port = requested_port
            if p.get("id"):
                self.follower.config.id = str(p["id"])
            if self.follower.connected:
                self.follower.disconnect()
            self._connect_follower()
            if self.follower.connected:
                self._remember_port("arm", self.follower.config.port)
            self._reply(cmd, ok=self.follower.connected, error=self.follower.error)
        elif kind == "disconnect_robot":
            self._clear_debug_lease()
            self._close_writer()
            if self._follower_is_virtual():
                self._virtual_power_enabled = False
                self._release_follower("virtual_disconnect")
                self._reply(cmd, ok=True)
                return
            if self.follower.connected:
                if self._pending_release:
                    self._reply(cmd, ok=True, pending=True)
                    return
                self._pending_release_reply = cmd.reply
                cmd.reply = None
                self._begin_relax_then_release("disconnect")
            else:
                self.mode = "offline"
                self._reply(cmd, ok=True)
        elif kind == "virtual_connect":
            self._require_no_debug_lease("connect virtual follower")
            if p.get("model_id"):
                self._virtual_model_id = str(p["model_id"])
            self._virtual_power_enabled = True
            if self.follower.connected and not self._follower_is_virtual():
                self.follower.set_virtual_model(self._virtual_model_id, self.config.robot.type)
            else:
                self._connect_virtual_follower()
            self._reply(cmd, ok=self.follower.connected, error=self.follower.error)
        elif kind == "virtual_disconnect":
            self._clear_debug_lease()
            self._close_writer()
            self._virtual_power_enabled = False
            if self._follower_is_virtual():
                self._release_follower("virtual_disconnect")
            else:
                self.mode = "idle" if self.follower.connected else "offline"
            self._reply(cmd, ok=True)
        elif kind == "set_virtual_model":
            model_id = str(p.get("model_id") or "").strip()
            if not model_id:
                raise ValueError("model_id is empty")
            self._virtual_model_id = model_id
            self.follower.set_virtual_model(model_id, str(p.get("robot_type") or "") or None)
            self._reply(cmd, ok=True, model_id=model_id)
        elif kind == "connect_leader":
            if p.get("port"):
                self.leader.config.port = str(p["port"])
            if p.get("id"):
                self.leader.config.id = str(p["id"])
            if self.leader.connected:
                self.leader.disconnect()
            self.leader_joints = {}
            self.leader.connect()
            if self.leader.connected:
                self._remember_port("leader", self.leader.config.port)
            self.log("info", f"leader connected on {self.leader.config.port}")
            self._reply(cmd, ok=True)
        elif kind == "disconnect_leader":
            if self.mode in {"teleop", "record"}:
                self.mode = "idle"
            self.leader_joints = {}
            self.leader.disconnect()
            self._reply(cmd, ok=True)
        elif kind == "hardware_apply":
            force = bool(p.get("force"))
            self._hardware_apply_active.set()
            if force:
                self._hardware_apply_force.set()
            else:
                self._hardware_apply_force.clear()
            try:
                result = apply_hardware_preset(
                    self,
                    str(p.get("name") or "unnamed"),
                    p.get("preset") if isinstance(p.get("preset"), dict) else {},
                    force=force,
                )
            finally:
                self._hardware_apply_active.clear()
                self._hardware_apply_force.clear()
            self._reply(cmd, **result)
        elif kind == "force_disconnect":
            role = str(p.get("role") or "all")
            if role not in {"arm", "leader", "all"}:
                raise ValueError(f"unknown force disconnect role '{role}'")
            self._cancel.set()
            with self._start_lock:
                self._start_generation += 1
            self.pending = None
            self._close_writer()
            self._end_rollout()
            if role in {"arm", "all"}:
                self._release_follower("force disconnect")
            if role in {"leader", "all"}:
                self.leader_joints = {}
                self.leader.disconnect()
            if not self.follower.connected and self.mode != "estop":
                self.mode = "offline"
            self._hardware_apply_force.clear()
            self._reply(cmd, ok=True, role=role)
        elif kind == "jog":
            self._require_no_debug_lease("jog")
            if self.pending in {"teleop_start", "record_start", "rollout_start", "capture_start"}:
                raise RuntimeError(f"cannot jog while {self.pending} is pending")
            source = str(p.get("source") or "manual")
            if source not in {"manual", "leader"}:
                raise ValueError(f"unknown jog source '{source}'")
            raw_max_speed = p.get("max_speed")
            max_speed: float | None = None
            if raw_max_speed is not None:
                max_speed = float(raw_max_speed)
                if not math.isfinite(max_speed) or max_speed <= 0:
                    raise ValueError("max_speed must be a positive finite number")
            live = bool(p.get("live"))
            if self.mode == "jogging" and live and not self._live_control:
                self._reply(cmd, ok=True, ignored=True, reason="motion_in_progress")
                return
            self._require_follower_connected("adjust joints")
            self._pending_release = None
            if self.mode in {"teleop", "record", "rollout"}:
                raise RuntimeError(f"cannot jog while {self.mode} is running")
            if source == "leader":
                self._require_leader_connected("relay leader pose")
                requested = self._read_leader_pose()
            else:
                requested = p.get("joints") or {}
            current = self.joints or self.follower.get_pose()
            target = merge_partial(current, requested)
            duration = p.get("duration_s")
            if live or (duration is not None and float(duration) <= 0):
                if max_speed is not None:
                    self._slew_start = None
                    self._slew_goal = None
                    self._live_target = target
                    self._live_max_speed = max_speed
                    self._live_last_t = time.perf_counter()
                    self._live_control = True
                    self.mode = "jogging"
                    self._touch_bus()
                    self._reply(
                        cmd,
                        ok=True,
                        target=target,
                        live=True,
                        source=source,
                        smoothed=True,
                    )
                    return
                self._clear_live_control()
                self._slew_goal = None
                self.latched = dict(target)
                self.action = dict(target)
                self.mode = "idle"
                self.follower.send_pose(target)
                self._touch_bus()
                self._reply(cmd, ok=True, target=target, live=True, source=source)
                return
            self._clear_live_control()
            self._slew_start = dict(current)
            self._slew_goal = target
            self._slew_t0 = time.perf_counter()
            self._slew_duration = float(duration if duration is not None else self.config.control.jog_duration_s)
            self.mode = "jogging"
            self._touch_bus()
            self.log("info", f"jog {list(requested.keys())} in {self._slew_duration:.1f}s")
            self._reply(cmd, ok=True, target=target, source=source)
        elif kind == "preset":
            name = str(p.get("name") or "relax")
            pose: dict[str, float] | None = None
            if self.store is not None:
                saved = self.store.presets("pose").get(name)
                if isinstance(saved, dict):
                    pose = {k: float(v) for k, v in saved.items()}
            if pose is None:
                raise ValueError(f"unknown pose preset '{name}'")
            cmd.payload = {"joints": pose, "duration_s": p.get("duration_s")}
            cmd.kind = "jog"
            self._handle(cmd)
        elif kind == "read_pose":
            self._require_follower_connected("read follower pose")
            pose = self.follower.get_pose()
            self.joints = dict(pose)
            self._touch_bus()
            self._reply(cmd, ok=True, joints=pose)
        elif kind == "read_leader":
            self._require_leader_connected("read leader pose")
            pose = self._read_leader_pose()
            self._reply(cmd, ok=True, joints=pose)
        elif kind == "hold":
            self.hold_when_idle = bool(p.get("enabled", True))
            self._reply(cmd, ok=True, hold=self.hold_when_idle)
        elif kind == "estop":
            self.request_estop()
            self._reply(cmd, ok=True)
        elif kind == "task_stop":
            self._clear_live_control()
            self._clear_debug_lease()
            self._invalidate_policy_load()
            self._cancel.clear()
            stopped = self.mode
            if stopped == "teleop":
                path = None
                if self.record_kind == "teleop":
                    path = self._close_writer()
                self.mode = "idle"
                if self.joints:
                    self.latched = dict(self.joints)
                self._touch_bus()
                self.log("info", "teleop stopped")
                self._reply(cmd, ok=True, stopped=stopped, path=None if path is None else str(path))
            elif stopped == "record":
                path = self.record_session.root if self.record_session is not None else self._close_writer()
                self._stop_record()
                self._touch_bus()
                self.log("info", "record stopped")
                self._reply(cmd, ok=True, stopped=stopped, path=None if path is None else str(path))
            elif stopped == "rollout":
                path = self._close_writer()
                self.loaded_policy = None
                self._end_rollout()
                self.mode = "idle" if self.follower.connected else "offline"
                if self.joints:
                    self.latched = dict(self.joints)
                self._touch_bus()
                self.log("info", "rollout stopped")
                self._reply(cmd, ok=True, stopped=stopped, path=None if path is None else str(path))
            elif stopped == "jogging":
                self._slew_goal = None
                if self.joints:
                    self.latched = dict(self.joints)
                self.mode = "idle"
                self._touch_bus()
                self._reply(cmd, ok=True, stopped=stopped)
            else:
                path = self._close_writer()
                self._reply(cmd, ok=True, stopped=stopped, path=None if path is None else str(path))
        elif kind == "force_stop":
            stopped = self.mode
            self._force_abort_active_task()
            self.log("info", f"force stop completed ({stopped})")
            self._reply(cmd, ok=True, stopped=stopped)
        elif kind == "resume":
            self._require_no_debug_lease("resume")
            self._clear_live_control()
            self._estop.clear()
            if not self.follower.connected and self._virtual_power_enabled:
                self._connect_virtual_follower()
            self._require_follower_connected("resume torque")
            self.follower.enable_torque()
            pose = self.follower.get_pose()
            self.joints = dict(pose)
            self.latched = dict(pose)
            self.hold_when_idle = self.config.control.hold_when_idle
            self.mode = "idle"
            self.log("info", "torque re-enabled, idle")
            self._reply(cmd, ok=True)
        elif kind == "teleop_start":
            self._require_no_debug_lease("start teleop")
            start_token = self._start_token(p)
            if self.mode in {"teleop", "record", "rollout"}:
                raise RuntimeError(f"stop {self.mode} before starting teleop")
            self._clear_live_control()
            if self._aborted(cmd, "teleop", start_token):
                return
            self._require_follower_connected("start teleop")
            if self._aborted(cmd, "teleop", start_token):
                return
            self._require_leader_connected("start teleop")
            if self._aborted(cmd, "teleop", start_token):
                return
            if p.get("auto_record") is not None:
                self.auto_record = bool(p.get("auto_record"))
            task_t0 = time.perf_counter()
            recorder: DatasetRecorder | None = None
            if self.auto_record and self.writer is None:
                recorder = self._open_recorder("teleop", p)
            with self._start_lock:
                cancelled = self._start_cancelled(start_token)
                if not cancelled:
                    self.mode = "teleop"
                    self.task_t0 = task_t0
                    if recorder is not None:
                        self._publish_recorder(recorder, "teleop")
                        self.episode_t0 = task_t0
            if cancelled:
                if recorder is not None:
                    recorder.close()
                self.pending = None
                self.log("info", "teleop cancelled")
                self._reply(cmd, ok=True, cancelled=True)
                return
            self._touch_bus()
            self.log("info", "teleop started")
            self._reply(cmd, ok=True, session_id=None if self.writer is None else self.writer.session_id)
        elif kind == "teleop_stop":
            if self.mode == "teleop":
                self.mode = "idle"
                if self.joints:
                    self.latched = dict(self.joints)
                if self.record_kind == "teleop":
                    self._close_writer()
                self._touch_bus()
            self._reply(cmd, ok=True)
        elif kind == "record_start":
            self._require_no_debug_lease("start recording")
            start_token = self._start_token(p)
            if self.mode in {"teleop", "record", "rollout"}:
                raise RuntimeError(f"stop {self.mode} before starting record")
            self._clear_live_control()
            if self._aborted(cmd, "record", start_token):
                return
            self._require_follower_connected("start recording")
            if self._aborted(cmd, "record", start_token):
                return
            self._require_leader_connected("start recording")
            if self._aborted(cmd, "record", start_token):
                return
            if self.record_session is not None and self.record_session.progress()["status"] not in {"completed", "error"}:
                raise RuntimeError("previous dataset is still saving")
            self._close_writer()
            self.episode_time_s = float(p.get("episode_time_s") or self.config.recording.default_episode_time_s)
            self.reset_time_s = float(
                p["reset_time_s"] if p.get("reset_time_s") is not None else self.config.recording.default_reset_time_s
            )
            self.num_episodes = int(p.get("num_episodes") or self.config.recording.default_num_episodes)
            if self.episode_time_s <= 0 or self.reset_time_s < 0 or self.num_episodes <= 0:
                raise ValueError("episode and reset durations or count are invalid")
            dataset_path = Path(str(p.get("dataset_path") or "")).expanduser().resolve()
            dataset_id = str(p.get("dataset_id") or "").strip()
            if not dataset_id or not (dataset_path / "meta" / "info.json").is_file():
                raise ValueError("choose a writable Dataset before recording")
            camera_keys = dict(p.get("camera_keys") or {})
            dataset_fps = int(p.get("dataset_fps") or 0)
            RecordDatasetSession.validate(dataset_path, camera_keys, dataset_fps)
            if not self.recording_mutation_lock.acquire(blocking=False):
                raise RuntimeError("dataset is busy")
            try:
                recover_record_publish(dataset_path)
                recorder = RecordDatasetSession(
                    dataset_path,
                    dataset_id,
                    str(p.get("dataset_repo_id") or dataset_id),
                    str(p.get("task") or "").strip(),
                    camera_keys,
                    dataset_fps,
                    int(p.get("encoder_threads") or self.config.recording.encoder_threads),
                    bool(p.get("deferred_encoding", False)),
                    self.recording_mutation_lock.release,
                )
            except BaseException:
                self.recording_mutation_lock.release()
                raise
            task_t0 = time.perf_counter()
            with self._start_lock:
                cancelled = self._start_cancelled(start_token)
                if not cancelled:
                    self.record_session = recorder
                    self.record_base_index = int(p.get("dataset_episodes") or 0)
                    self.record_completed = 0
                    self.record_pending_attempt = None
                    self.record_attempt = None
                    self.record_auto_next = bool(p.get("auto_next", False))
                    requested_speed = float(p.get("resume_speed") or 0.0)
                    self.record_instant_fallback = requested_speed == 0.0
                    self.record_speed = 30.0 if self.record_instant_fallback else requested_speed
                    self.record_version = 0
                    self.record_operations.clear()
                    self.mode = "record"
                    self.task_t0 = task_t0
                    self._set_record_phase("resetting")
            if cancelled:
                recorder.stop()
                self.pending = None
                self.log("info", "record cancelled")
                self._reply(cmd, ok=True, cancelled=True)
                return
            self._touch_bus()
            self.log("info", f"record ready: {dataset_id}")
            self._reply(cmd, ok=True, session_id=recorder.session_id, dataset_id=dataset_id)
        elif kind == "record_stop":
            if self._record_control(p):
                self._stop_record()
            self._reply(cmd, ok=True, record=self._record_snapshot())
        elif kind == "record_next":
            if self._record_control(p):
                if self.record_phase == "resetting":
                    self._set_record_resume_speed(p)
                    self._start_record_episode()
                elif self.record_phase == "recording":
                    self._finish_record_episode()
                else:
                    raise RuntimeError("record cannot advance in this phase")
            self._reply(cmd, ok=True, record=self._record_snapshot())
        elif kind == "record_pause":
            if self._record_control(p):
                if self.record_phase not in {"recording", "resetting"}:
                    raise RuntimeError("record cannot pause in this phase")
                if self.record_fault:
                    raise RuntimeError("this episode needs re-recording")
                now = time.perf_counter()
                if not self.record_paused:
                    self.record_elapsed += max(0.0, now - self.record_phase_t0)
                    self.record_paused = True
                    if self.record_phase == "recording":
                        self.latched = dict(self.joints)
                else:
                    self._set_record_resume_speed(p)
                    self.record_phase_t0 = now
                    self.record_paused = False
                    self.record_aligning = True
                    self.record_motion_t = now
                    self._reset_recording_deadlines(now)
            self._reply(cmd, ok=True, record=self._record_snapshot())
        elif kind == "record_back":
            if self._record_control(p):
                if self.record_phase == "recording" and self.record_attempt and self.record_session:
                    self.record_session.seal(self.record_attempt, discard=True)
                    self.record_session.clear_capture_error()
                    self.record_attempt = None
                    self.last_error = None
                    self._set_record_phase("resetting")
                elif self.record_phase == "resetting" and self.record_pending_attempt and self.record_session:
                    self.record_session.discard(self.record_pending_attempt)
                    self.record_pending_attempt = None
                    self.record_completed -= 1
                    self._set_record_phase("resetting")
                else:
                    raise RuntimeError("no episode to re-record")
            self._reply(cmd, ok=True, record=self._record_snapshot())
        elif kind == "record_retry":
            session = self.record_session
            if session is None or session.session_id != p.get("session_id"):
                raise RuntimeError("record session has changed")
            if not p.get("operation_id"):
                raise ValueError("record retry requires operation_id")
            if p["operation_id"] in self.record_operations:
                self._reply(cmd, ok=True, record=self._record_snapshot())
                return
            if int(p.get("version", -1)) != self.record_version:
                raise ValueError("record state changed; refresh before retrying")
            session.retry()
            self.record_operations.add(str(p["operation_id"]))
            self.record_version += 1
            self._reply(cmd, ok=True, record=self._record_snapshot())
        elif kind == "rollout_start":
            self._require_no_debug_lease("start rollout")
            if self.mode in {"teleop", "record", "rollout"}:
                raise RuntimeError(f"stop {self.mode} before starting rollout")
            self._clear_live_control()
            if self._aborted(cmd, "rollout"):
                return
            self._require_follower_connected("start rollout")
            if self._aborted(cmd, "rollout"):
                return
            path = str(p.get("policy_path") or "")
            if not path:
                raise ValueError("policy_path is required")
            requested_policy_fps, effective_policy_fps = self._policy_rates(p)
            p["policy_fps"] = requested_policy_fps
            p["effective_policy_fps"] = effective_policy_fps
            extra = p.get("extra") or {}
            self.rollout_extra = {str(k): str(v) for k, v in extra.items() if str(k).strip()}
            device = str(p.get("device") or self.config.rollout.device)
            task = str(p.get("task") or "")
            cached = self._cached_policy(path, device, self.rollout_extra)
            if cached is not None:
                self.log("info", f"using cached policy {path}")
                with self._policy_job_lock:
                    self._policy_generation += 1
                    self._policy_job = None
                started = self._begin_rollout(cached, p, path)
                self._reply(
                    cmd,
                    ok=True,
                    cancelled=not started,
                    session_id=None if self.writer is None else self.writer.session_id,
                )
                return
            self.log("info", f"loading policy {path} (background, local cache first)")
            with self._policy_job_lock:
                self._policy_generation += 1
                job = _PolicyLoadJob(generation=self._policy_generation, payload=dict(p))
                thread = threading.Thread(
                    target=self._policy_worker,
                    args=(job, path, device, task, self.rollout_extra),
                    daemon=True,
                    name="policy-load",
                )
                job.thread = thread
                self._policy_job = job
            thread.start()
            self._reply(cmd, ok=True, accepted=True)
        elif kind == "rollout_stop":
            self._invalidate_policy_load()
            path = self._close_writer()
            self.loaded_policy = None
            self._end_rollout()
            self.mode = "idle" if self.follower.connected else "offline"
            if self.joints:
                self.latched = dict(self.joints)
            self._touch_bus()
            self._reply(cmd, ok=True, path=None if path is None else str(path))
        elif kind == "capture_start":
            self._require_no_debug_lease("start capture")
            start_token = self._start_token(p)
            self._clear_live_control()
            if self._aborted(cmd, "capture", start_token):
                return
            if self.writer is not None:
                self._reply(cmd, ok=True, session_id=self.writer.session_id, already=True)
                return
            recorder = self._open_recorder(str(p.get("kind") or self.mode or "capture"), p)
            with self._start_lock:
                cancelled = self._start_cancelled(start_token)
                if not cancelled:
                    self._publish_recorder(recorder, recorder.kind)
                    self.episode_t0 = time.perf_counter()
            if cancelled:
                recorder.close()
                self.pending = None
                self.log("info", "capture cancelled")
                self._reply(cmd, ok=True, cancelled=True)
                return
            self.log("info", f"capture {self.writer.session_id}")
            self._reply(cmd, ok=True, session_id=self.writer.session_id, dataset_id=self.selected_dataset_id)
        elif kind == "capture_stop":
            if self.mode == "record":
                raise RuntimeError("stop the record task to finish the dataset")
            path = self._close_writer()
            self._reply(cmd, ok=True, path=None if path is None else str(path))
        elif kind == "auto_record":
            self.auto_record = bool(p.get("enabled", True))
            self._reply(cmd, ok=True, auto_record=self.auto_record)
        else:
            raise ValueError(f"unknown command '{kind}'")

    def _tick(self) -> None:
        if self._estop.is_set():
            if self.mode != "estop":
                self._apply_estop()
            return
        if not self.leader.connected:
            self.leader_joints = {}
        elif self.mode not in {"teleop", "record"}:
            try:
                self.leader_joints = dict(self.leader.get_action_pose())
            except Exception as exc:  # noqa: BLE001
                self.leader.error = str(exc)
                self.leader_joints = {}
                self.log("error", f"leader read failed: {exc}")
                with contextlib.suppress(Exception):
                    self.leader.disconnect()
        if not self.follower.connected:
            if self.mode in {"jogging", "teleop", "record", "rollout"}:
                self._abort_active_task("follower disconnected")
            elif self.mode not in {"offline", "estop"}:
                self.mode = "offline"
            return
        try:
            self.joints = self.follower.get_pose()
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self.log("error", f"read failed: {exc}")
            if self._pending_release:
                self._release_follower(self._pending_release)
            elif self.mode in {"jogging", "teleop", "record", "rollout"}:
                self._abort_active_task(f"read failed: {exc}")
            return

        try:
            if self.mode == "jogging":
                self._tick_jog()
            elif self.mode == "teleop":
                self._tick_teleop()
            elif self.mode == "record":
                self._tick_record()
            elif self.mode == "rollout":
                self._tick_rollout()
            elif self.mode == "idle":
                if self._debug_lease_token is None and self.hold_when_idle and self.latched:
                    self.follower.send_pose(self.latched)
                    self.action = dict(self.latched)
                self._maybe_record(self.record_kind or "capture")
        except Exception as exc:  # noqa: BLE001
            self._clear_debug_lease()
            self.last_error = str(exc)
            self.log("error", f"{self.mode} tick failed: {exc}")
            if self.mode == "jogging" and self._pending_release:
                self._release_follower(self._pending_release)
            elif self.mode in {"rollout", "record", "teleop"}:
                if self.mode == "rollout":
                    self._end_rollout()
                self._close_writer()
                self.mode = "idle"

    def _abort_active_task(self, reason: str) -> None:
        """Return to a safe idle/offline state without waiting for another tick."""
        self._clear_live_control()
        self._cancel.set()
        self.pending = None
        self._invalidate_policy_load()
        self._end_rollout()
        self.loaded_policy = None
        self._close_writer()
        self._slew_goal = None
        self._pending_release = None
        self.mode = "idle" if self.follower.connected else "offline"
        self.last_error = reason
        self.log("error", f"active task aborted: {reason}")

    def _force_abort_active_task(self) -> None:
        """Detach slow shutdown work so force stop can return immediately."""
        self._clear_live_control()
        self.pending = None
        self._invalidate_policy_load()
        engine = self._inference_engine
        self._inference_engine = None
        self._rollout_hw_feature_spec = {}
        self._clear_rollout_prediction()
        if engine is not None:
            threading.Thread(
                target=self._stop_inference_engine_safely,
                args=(engine,),
                daemon=True,
                name="force-stop-inference",
            ).start()
        self.loaded_policy = None
        self._close_writer()
        self._slew_goal = None
        self._pending_release = None
        self.mode = "idle" if self.follower.connected else "offline"

    @staticmethod
    def _stop_inference_engine_safely(engine: Any) -> None:
        try:
            engine.stop()
        except Exception:
            pass

    def _tick_jog(self) -> None:
        if self._live_control:
            self._tick_live_jog()
            return
        assert self._slew_start is not None and self._slew_goal is not None
        alpha = (time.perf_counter() - self._slew_t0) / max(self._slew_duration, 1e-3)
        pose = lerp_pose(self._slew_start, self._slew_goal, alpha)
        self.follower.send_pose(pose)
        self.action = dict(pose)
        self.latched = dict(pose)
        if alpha >= 1.0:
            self.latched = dict(self._slew_goal)
            self._slew_goal = None
            if self._pending_release:
                reason = self._pending_release
                self._release_follower(reason)
            else:
                self.mode = "idle"
                self._touch_bus()

    def _tick_live_jog(self) -> None:
        target = self._live_target
        if target is None:
            self._clear_live_control()
            self.mode = "idle"
            return
        now = time.perf_counter()
        dt = max(now - self._live_last_t, 1.0 / max(1.0, self.config.control.fps))
        self._live_last_t = now
        current = self.action or self.joints
        if not current:
            current = self.follower.get_pose()
        pose = _step_pose_toward(current, target, self._live_max_speed * dt)
        self.follower.send_pose(pose)
        self.action = dict(pose)
        self.latched = dict(pose)
        if all(abs(float(pose[name]) - float(target[name])) <= 1e-6 for name in pose):
            self.latched = dict(target)
            self._clear_live_control()
            self.mode = "idle"
            self._touch_bus()

    def _tick_teleop(self) -> None:
        pose = self.leader.get_action_pose()
        self.leader_joints = dict(pose)
        self.follower.send_pose(pose)
        self.action = dict(pose)
        self.latched = dict(pose)
        if self.mode == "teleop":
            self._maybe_record(self.record_kind or "teleop")

    def _tick_record(self) -> None:
        session = self.record_session
        if session is None:
            return
        progress = session.progress()
        if progress["error"]:
            if self.record_phase == "recording" and self.record_fault is None:
                self._pause_record_for_fault(progress["error"], time.perf_counter())
            return
        now = time.perf_counter()
        if self.record_phase == "recording" and self.record_paused:
            if self.latched:
                self.follower.send_pose(self.latched)
            return
        pose = self.leader.get_action_pose()
        self.leader_joints = dict(pose)
        if self.record_aligning:
            delta_t = min(2.0 / max(1.0, self.config.control.fps), max(0.0, now - self.record_motion_t))
            limited = _step_pose_toward(self.joints, pose, self.record_speed * delta_t)
            self.record_aligning = any(abs(limited.get(j, 0.0) - pose.get(j, 0.0)) > 1e-5 for j in pose)
            pose = limited
        self.record_motion_t = now
        self.follower.send_pose(pose)
        self.action = dict(pose)
        self.latched = dict(pose)
        if self.record_phase == "resetting":
            if self.record_auto_next and not self.record_paused and self.record_elapsed + now - self.record_phase_t0 >= self.reset_time_s:
                self._start_record_episode()
            return
        if self.record_phase != "recording":
            return
        elapsed = self.record_elapsed + now - self.record_phase_t0
        if now >= self._next_action_t and self.record_attempt is not None:
            if session.camera_keys:
                connected = {
                    str(cam.get("label") or cam.get("name"))
                    for cam in self.cameras.snapshots()
                    if cam.get("enabled") and cam.get("show_main") and cam.get("connected")
                }
                if connected != set(session.camera_keys):
                    self._pause_record_for_fault("record camera disconnected; re-record this episode", now)
                    return
            images = self.cameras.latest_main_jpeg_map() if session.camera_keys else {}
            if session.camera_keys and set(images) != set(session.camera_keys):
                self._pause_record_for_fault("record camera unavailable; re-record this episode", now)
                return
            try:
                session.add_sample(RecordSample(self.record_attempt, dict(self.joints), dict(pose), images))
            except RuntimeError as exc:
                self._pause_record_for_fault(str(exc), now)
                return
            self._next_action_t = self._advance_deadline(self._next_action_t, 1.0 / session.fps, now)
        if elapsed >= self.episode_time_s:
            self._finish_record_episode()

    def _tick_rollout(self) -> None:
        if self._cancel.is_set() or self.loaded_policy is None:
            self._end_rollout()
            self.mode = "idle"
            return
        now = time.perf_counter()
        if self.task_deadline is not None and now >= self.task_deadline:
            self.log("info", "rollout duration reached")
            self._close_writer()
            self.loaded_policy = None
            self._end_rollout()
            self.mode = "idle"
            self.latched = dict(self.joints)
            return
        if now < self._next_policy_t:
            self._maybe_record("rollout")
            return
        self._next_policy_t = self._advance_deadline(self._next_policy_t, self._policy_interval, now)
        engine = self._inference_engine
        if engine is None:
            raise RuntimeError("rollout inference engine is not running")
        if engine.failed:
            raise RuntimeError(engine.failure_traceback or "rollout inference engine failed")

        observation = dict(self.joints or {})
        observation.update(self.cameras.latest_rgb_map())
        engine.notify_observation(observation)
        obs_frame = self._build_rollout_obs_frame(observation)
        action = engine.get_action(obs_frame)
        if action is None:
            self._maybe_record("rollout")
            return
        pose = pose_from_action_tensor(self.loaded_policy, action, self.joints)
        if self._cancel.is_set() or self._estop.is_set() or self.mode != "rollout":
            return
        self.follower.send_pose(pose)
        self.action = dict(pose)
        self.latched = dict(pose)
        self._maybe_record("rollout")
        if now >= self._next_prediction_t:
            queued = inference_leftover_poses(engine, self.loaded_policy, self.joints)
            if queued:
                self._record_rollout_prediction(queued)
            self._next_prediction_t = now + self._prediction_interval

    def _rollout_hw_features(self) -> dict:
        """Describe the raw observation keys consumed by LeRobot's engine."""
        from lerobot.utils.constants import OBS_STR
        from lerobot.utils.feature_utils import hw_to_dataset_features

        hardware: dict[str, Any] = {name: float for name in JOINT_ORDER}
        for camera in self.cameras.snapshots():
            if not (camera.get("enabled") and camera.get("feed_robot")):
                continue
            hardware[str(camera["name"])] = (
                int(camera.get("height") or 480),
                int(camera.get("width") or 640),
                3,
            )
        return hw_to_dataset_features(hardware, OBS_STR, use_video=False)

    def _build_rollout_obs_frame(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Build the dataset frame passed to LeRobot's inference engine."""
        from lerobot.utils.constants import OBS_STR
        from lerobot.utils.feature_utils import build_dataset_frame

        return build_dataset_frame(self._rollout_hw_feature_spec, observation, prefix=OBS_STR)

    def _start_inference_engine(self, loaded: LoadedPolicy) -> bool:
        """Build and start LeRobot's sync or RTC inference engine."""
        self._end_rollout()
        try:
            config = inference_config_from_extra(self.rollout_extra)
            rtc = getattr(config, "rtc", None)
            self.log(
                "info",
                f"rollout inference config: type={config.type}"
                + (f", rtc={rtc}" if rtc is not None else ""),
            )
            hw_features = self._rollout_hw_features()
            engine = create_monitor_inference_engine(
                loaded,
                inference_config=config,
                hw_features=hw_features,
                task=loaded.task,
                fps=self.effective_policy_fps,
            )
            engine.reset()
            engine.start()
            engine.resume()
        except Exception as exc:  # noqa: BLE001 - report engine setup failures to the UI
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.log("error", f"inference engine setup failed: {self.last_error}")
            self.log("error", traceback.format_exc())
            self._close_writer()
            self.loaded_policy = None
            self.mode = "idle" if self.follower.connected else "offline"
            return False
        self._inference_engine = engine
        self._rollout_hw_feature_spec = hw_features
        self._next_prediction_t = self.task_t0
        self.log("info", f"rollout inference engine started ({config.type})")
        return True

    def _end_rollout(self) -> None:
        """Stop background inference and clear chart state for the current rollout."""
        engine = self._inference_engine
        self._inference_engine = None
        self._rollout_hw_feature_spec = {}
        if engine is not None:
            engine.stop()
        self._clear_rollout_prediction()

    def _clear_rollout_prediction(self) -> None:
        self._rollout_prediction = None

    def _record_rollout_prediction(self, actions: list[dict[str, float]]) -> None:
        """Publish the future actions already held by LeRobot's RTC action queue."""
        step_s = 1.0 / max(1.0, float(self.policy_fps))
        self._prediction_sequence += 1
        self._rollout_prediction = {
            "id": self._prediction_sequence,
            "t_s": round(time.perf_counter() - self.task_t0, 3),
            "step_s": round(step_s, 6),
            "strategy": "policy_queue",
            "degraded": False,
            "latency_ms": 0.0,
            "actions": [
                {name: round(float(value), 6) for name, value in action.items()}
                for action in actions
            ],
        }
