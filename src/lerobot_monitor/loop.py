"""Exclusive control thread: the only code that talks to the Feetech buses."""

from __future__ import annotations

import contextlib
import logging
import math
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TextIO

from .cameras import CameraHub
from .config import MonitorConfig
from .leader import LeaderArm
from .library import DatasetRecorder, VideoLibrary
from .policy import LoadedPolicy, load_policy, predict_pose
from .robot import FollowerArm
from .store import JsonStore
from .types import JOINT_ORDER, RELAX_POSE, lerp_pose, merge_partial

logger = logging.getLogger(__name__)
_LOG_SKIP_PREFIXES = ("uvicorn.access", "lerobot_monitor")
# Policy construction mutates process-global stdout, environment, and HF/torch
# caches. Serializing only output capture still lets two loaders corrupt those
# globals, including when separate ControlLoop instances exist in one process.
_POLICY_LOAD_LOCK = threading.Lock()
_UI_LOG_LOCK = threading.Lock()
_UI_LOG_HANDLERS: set[logging.Handler] = set()
_UI_LOG_PREVIOUS_ROOT_LEVEL: int | None = None
_UI_LOG_CHANGED_ROOT_LEVEL = False


class _UiLogHandler(logging.Handler):
    """Forward third-party loggers (policy load, HF download) into the UI log."""

    def __init__(self, loop: ControlLoop) -> None:
        super().__init__(level=logging.INFO)
        self.loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        if any(record.name.startswith(prefix) for prefix in _LOG_SKIP_PREFIXES):
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
        self._buf += text
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
        self.action: dict[str, float] = {}
        self.loop_fps = 0.0
        self.last_error: str | None = None
        self.logs: list[dict[str, str]] = []

        self._slew_start: dict[str, float] | None = None
        self._slew_goal: dict[str, float] | None = None
        self._slew_t0 = 0.0
        self._slew_duration = config.control.jog_duration_s

        self.writer: DatasetRecorder | None = None
        self.record_kind: str | None = None
        self.episode_index = 0
        self.recording_start_index = 0
        self.episode_t0 = 0.0
        self.episode_time_s = config.recording.default_episode_time_s
        self.reset_time_s = config.recording.default_reset_time_s
        self.num_episodes = config.recording.default_num_episodes
        self._resetting = False
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
        self._policy_cache: dict[tuple[Any, ...], LoadedPolicy] = {}
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
        self._estop = threading.Event()
        self._ui_log_handler: logging.Handler | None = None

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
        try:
            thread = self._thread
            if thread is not None:
                thread.join(timeout=10.0)
                if not thread.is_alive():
                    self._thread = None
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

    def request_estop(self) -> dict[str, Any]:
        """Disable torque immediately; do not wait for the control-queue tick."""
        self._estop.set()
        self._cancel.set()
        with self._start_lock:
            self._start_generation += 1
        self._apply_estop()
        self._invalidate_policy_load(blocking=False)
        return {"ok": True}

    def request_stop(self) -> dict[str, Any]:
        """Abort a pending start immediately; queue a real stop if a task is running."""
        self._cancel.set()
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
        self.mode = "estop"
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
        if self.mode == "idle" and self.follower.connected:
            return "hold" if self.hold_when_idle else "idle"
        if self.mode == "jogging":
            return "jog"
        return self.mode

    def bus_owner(self) -> str:
        if self._estop.is_set() or self.mode == "estop":
            return "estop"
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
        entry = {"level": level, "message": message, "t": time.strftime("%H:%M:%S")}
        with self._log_lock:
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
            with _POLICY_LOAD_LOCK, self._capture_task_output():
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
        if self.on_snapshot is not None:
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
            "fps": round(self.loop_fps, 1),
            "robot": self.follower.snapshot(),
            "leader": self.leader.snapshot(),
            "cameras": self.cameras.snapshots(),
            "joints": self.joints,
            "goal": self.latched,
            "action": self.action,
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
            },
            "logs": self.logs[-200:],
        }

    def _reply(self, cmd: Command, **kwargs: Any) -> None:
        if cmd.reply is not None:
            cmd.reply.put(kwargs)

    def _close_writer(self) -> Path | None:
        if self.writer is None:
            return None
        writer = self.writer
        self.writer = None
        self.record_kind = None
        path = writer.close()
        self.log("info", f"dataset saved: {path}")
        return path

    def _publish_recorder(self, recorder: DatasetRecorder, kind: str) -> None:
        self.writer = recorder
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
            "encoder_threads": int(
                p["encoder_threads"]
                if p.get("encoder_threads") is not None
                else self.config.recording.encoder_threads
            ),
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
        action_due = now >= self._next_action_t
        video_due = now >= self._next_video_t
        if not action_due and not video_due:
            return
        if action_due:
            self.writer.add_action(
                self.joints,
                self.action or self.latched,
                episode_index=self.episode_index,
                kind=kind,
            )
            self._next_action_t = self._advance_deadline(self._next_action_t, self._action_interval, now)
        if video_due:
            images: dict[str, Any] = {}
            if self.writer.video:
                images = self.cameras.latest_main_bgr_map()
                if not images:
                    images = self.cameras.latest_bgr_map()
            self.writer.add_video(images, episode_index=self.episode_index)
            self._next_video_t = self._advance_deadline(self._next_video_t, self._video_interval, now)

    def _run(self) -> None:
        if self.config.robot.auto_connect:
            self._connect_follower()
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

    def _ensure_follower(self) -> None:
        if not self.follower.connected:
            self._connect_follower()
        if not self.follower.connected:
            raise RuntimeError(self.follower.error or "follower serial not available")
        self._touch_bus()

    def _release_follower(self, reason: str) -> None:
        if not self.follower.connected:
            return
        self.follower.disconnect()
        self.mode = "estop" if reason == "estop" or self._estop.is_set() else "offline"
        self._pending_release = None
        self.log("info", f"released follower serial ({reason})")
        if self._pending_release_reply is not None:
            self._pending_release_reply.put({"ok": True})
            self._pending_release_reply = None

    def _relax_pose(self) -> dict[str, float]:
        if self.store is not None:
            saved = self.store.presets("pose").get("relax")
            if isinstance(saved, dict) and saved:
                return {name: float(saved[name]) for name in JOINT_ORDER if name in saved}
        return dict(RELAX_POSE)

    def _begin_relax_then_release(self, reason: str) -> None:
        if not self.follower.connected or self._pending_release:
            return
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
                if self._estop.is_set() and cmd.kind not in {"resume", "estop", "task_stop"}:
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
        if kind == "connect_robot":
            if p.get("port"):
                self.follower.config.port = str(p["port"])
            if p.get("id"):
                self.follower.config.id = str(p["id"])
            if self.follower.connected:
                self.follower.disconnect()
            self._connect_follower()
            if self.follower.connected:
                self._remember_port("arm", self.follower.config.port)
            self._reply(cmd, ok=self.follower.connected, error=self.follower.error)
        elif kind == "disconnect_robot":
            self._close_writer()
            if self.follower.connected:
                self._pending_release_reply = cmd.reply
                cmd.reply = None
                self._begin_relax_then_release("disconnect")
            else:
                self.mode = "offline"
                self._reply(cmd, ok=True)
        elif kind == "connect_leader":
            if p.get("port"):
                self.leader.config.port = str(p["port"])
            if p.get("id"):
                self.leader.config.id = str(p["id"])
            if self.leader.connected:
                self.leader.disconnect()
            self.leader.connect()
            if self.leader.connected:
                self._remember_port("leader", self.leader.config.port)
            self.log("info", f"leader connected on {self.leader.config.port}")
            self._reply(cmd, ok=True)
        elif kind == "disconnect_leader":
            if self.mode in {"teleop", "record"}:
                self.mode = "idle"
            self.leader.disconnect()
            self._reply(cmd, ok=True)
        elif kind == "jog":
            if self.pending in {"teleop_start", "record_start", "rollout_start", "capture_start"}:
                raise RuntimeError(f"cannot jog while {self.pending} is pending")
            if self.mode == "jogging" and bool(p.get("live")):
                raise RuntimeError("cannot live jog while a jog or relax motion is running")
            self._ensure_follower()
            self._pending_release = None
            if self.mode in {"teleop", "record", "rollout"}:
                raise RuntimeError(f"cannot jog while {self.mode} is running")
            current = self.joints or self.follower.get_pose()
            target = merge_partial(current, p.get("joints") or {})
            live = bool(p.get("live"))
            duration = p.get("duration_s")
            if live or (duration is not None and float(duration) <= 0):
                self._slew_goal = None
                self.latched = dict(target)
                self.action = dict(target)
                self.mode = "idle"
                self.follower.send_pose(target)
                self._touch_bus()
                self._reply(cmd, ok=True, target=target, live=True)
                return
            self._slew_start = dict(current)
            self._slew_goal = target
            self._slew_t0 = time.perf_counter()
            self._slew_duration = float(duration if duration is not None else self.config.control.jog_duration_s)
            self.mode = "jogging"
            self._touch_bus()
            self.log("info", f"jog {list((p.get('joints') or {}).keys())} in {self._slew_duration:.1f}s")
            self._reply(cmd, ok=True, target=target)
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
            self._ensure_follower()
            pose = self.follower.get_pose()
            self.joints = dict(pose)
            self._touch_bus()
            self._reply(cmd, ok=True, joints=pose)
        elif kind == "read_leader":
            if not self.leader.connected:
                self.leader.connect()
            pose = self.leader.get_action_pose()
            self._reply(cmd, ok=True, joints=pose)
        elif kind == "hold":
            self.hold_when_idle = bool(p.get("enabled", True))
            self._reply(cmd, ok=True, hold=self.hold_when_idle)
        elif kind == "estop":
            self.request_estop()
            self._reply(cmd, ok=True)
        elif kind == "task_stop":
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
                path = self._close_writer()
                self.mode = "idle" if self.follower.connected else "offline"
                self._touch_bus()
                self.log("info", "record stopped")
                self._reply(cmd, ok=True, stopped=stopped, path=None if path is None else str(path))
            elif stopped == "rollout":
                path = self._close_writer()
                self.loaded_policy = None
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
        elif kind == "resume":
            self._estop.clear()
            self._ensure_follower()
            self.follower.enable_torque()
            pose = self.follower.get_pose()
            self.joints = dict(pose)
            self.latched = dict(pose)
            self.hold_when_idle = self.config.control.hold_when_idle
            self.mode = "idle"
            self.log("info", "torque re-enabled, idle")
            self._reply(cmd, ok=True)
        elif kind == "teleop_start":
            start_token = self._start_token(p)
            if self.mode in {"teleop", "record", "rollout"}:
                raise RuntimeError(f"stop {self.mode} before starting teleop")
            if self._aborted(cmd, "teleop", start_token):
                return
            self._ensure_follower()
            if self._aborted(cmd, "teleop", start_token):
                return
            if not self.leader.connected:
                self.leader.connect()
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
            start_token = self._start_token(p)
            if self.mode in {"teleop", "record", "rollout"}:
                raise RuntimeError(f"stop {self.mode} before starting record")
            if self._aborted(cmd, "record", start_token):
                return
            self._ensure_follower()
            if self._aborted(cmd, "record", start_token):
                return
            if not self.leader.connected:
                self.leader.connect()
            if self._aborted(cmd, "record", start_token):
                return
            self._close_writer()
            self.episode_time_s = float(p.get("episode_time_s") or self.config.recording.default_episode_time_s)
            self.reset_time_s = float(
                p["reset_time_s"] if p.get("reset_time_s") is not None else self.config.recording.default_reset_time_s
            )
            self.num_episodes = int(p.get("num_episodes") or self.config.recording.default_num_episodes)
            self._resetting = False
            recorder = self._open_recorder("record", p)
            task_t0 = time.perf_counter()
            with self._start_lock:
                cancelled = self._start_cancelled(start_token)
                if not cancelled:
                    self._publish_recorder(recorder, "record")
                    self.mode = "record"
                    self.task_t0 = task_t0
                    self.episode_t0 = task_t0
            if cancelled:
                recorder.close()
                self.pending = None
                self.log("info", "record cancelled")
                self._reply(cmd, ok=True, cancelled=True)
                return
            self._touch_bus()
            self.log("info", f"recording {self.writer.session_id} ep {self.episode_index}")
            self._reply(cmd, ok=True, session_id=self.writer.session_id, dataset_id=self.selected_dataset_id)
        elif kind == "record_stop":
            path = self._close_writer()
            self.mode = "idle" if self.follower.connected else "offline"
            self._touch_bus()
            self._reply(cmd, ok=True, path=None if path is None else str(path))
        elif kind == "record_next":
            if self.writer is None:
                raise RuntimeError("not recording")
            self.writer.finish_episode(self.episode_index)
            self._resetting = False
            self.episode_index += 1
            self.episode_t0 = time.perf_counter()
            self._reset_recording_deadlines(self.episode_t0)
            self.log("info", f"episode {self.episode_index}")
            self._reply(cmd, ok=True, episode_index=self.episode_index)
        elif kind == "rollout_start":
            if self.mode in {"teleop", "record", "rollout"}:
                raise RuntimeError(f"stop {self.mode} before starting rollout")
            if self._aborted(cmd, "rollout"):
                return
            self._ensure_follower()
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
            cached = self._policy_cache.get(self._policy_key(path, device, self.rollout_extra))
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
            self.mode = "idle" if self.follower.connected else "offline"
            if self.joints:
                self.latched = dict(self.joints)
            self._touch_bus()
            self._reply(cmd, ok=True, path=None if path is None else str(path))
        elif kind == "capture_start":
            start_token = self._start_token(p)
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
        if not self.follower.connected:
            if self.mode not in {"offline", "estop"}:
                self.mode = "offline"
            return
        try:
            self.joints = self.follower.get_pose()
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self.log("error", f"read failed: {exc}")
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
                if self.hold_when_idle and self.latched:
                    self.follower.send_pose(self.latched)
                    self.action = dict(self.latched)
                self._maybe_record(self.record_kind or "capture")
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self.log("error", f"{self.mode} tick failed: {exc}")
            if self.mode in {"rollout", "record", "teleop"}:
                self._close_writer()
                self.mode = "idle"

    def _tick_jog(self) -> None:
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

    def _tick_teleop(self) -> None:
        pose = self.leader.get_action_pose()
        self.follower.send_pose(pose)
        self.action = dict(pose)
        self.latched = dict(pose)
        if self.mode == "teleop":
            self._maybe_record(self.record_kind or "teleop")

    def _tick_record(self) -> None:
        self._tick_teleop()
        now = time.perf_counter()
        if getattr(self, "_resetting", False):
            if now - self.episode_t0 >= getattr(self, "reset_time_s", 0.0):
                self._resetting = False
                self.episode_index += 1
                self.episode_t0 = now
                self._reset_recording_deadlines(now)
                self.log("info", f"episode {self.episode_index}")
            return
        self._maybe_record("record")
        if now - self.episode_t0 >= self.episode_time_s:
            finished = self.episode_index - self.recording_start_index + 1
            if self.writer is not None:
                self.writer.finish_episode(self.episode_index)
            if finished >= self.num_episodes:
                self.log("info", f"reached {self.num_episodes} episodes")
                self._close_writer()
                self.mode = "idle"
                self._touch_bus()
            elif getattr(self, "reset_time_s", 0.0) > 0:
                self._resetting = True
                self.episode_t0 = now
                self.log("info", "reset")
            else:
                self.episode_index += 1
                self.episode_t0 = now
                self._reset_recording_deadlines(now)
                self.log("info", f"episode {self.episode_index}")

    def _tick_rollout(self) -> None:
        if self._cancel.is_set() or self.loaded_policy is None:
            self.mode = "idle"
            return
        now = time.perf_counter()
        if self.task_deadline is not None and now >= self.task_deadline:
            self.log("info", "rollout duration reached")
            self._close_writer()
            self.loaded_policy = None
            self.mode = "idle"
            self.latched = dict(self.joints)
            return
        if now < self._next_policy_t:
            self._maybe_record("rollout")
            return
        self._next_policy_t = self._advance_deadline(self._next_policy_t, self._policy_interval, now)
        images = self.cameras.latest_rgb_map()
        pose = predict_pose(self.loaded_policy, self.joints, images)
        if self._cancel.is_set() or self._estop.is_set() or self.mode != "rollout":
            return
        self.follower.send_pose(pose)
        self.action = dict(pose)
        self.latched = dict(pose)
        self._maybe_record("rollout")
