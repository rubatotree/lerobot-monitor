"""Exclusive control thread: the only code that talks to the Feetech buses."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .cameras import CameraHub
from .config import MonitorConfig
from .leader import LeaderArm
from .policy import LoadedPolicy, load_policy, predict_pose
from .robot import FollowerArm
from .session import SessionWriter
from .types import PRESETS, lerp_pose, merge_partial

logger = logging.getLogger(__name__)


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


class ControlLoop:
    def __init__(
        self,
        config: MonitorConfig,
        cameras: CameraHub,
        follower: FollowerArm,
        leader: LeaderArm,
        on_snapshot: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config
        self.cameras = cameras
        self.follower = follower
        self.leader = leader
        self.on_snapshot = on_snapshot

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

        self.writer: SessionWriter | None = None
        self.record_kind: str | None = None
        self.episode_index = 0
        self.episode_t0 = 0.0
        self.episode_time_s = config.recording.default_episode_time_s
        self.task_t0 = 0.0
        self.task_deadline: float | None = None
        self.loaded_policy: LoadedPolicy | None = None
        self.rollout_task = ""

        self._commands: queue.Queue[Command] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._record_interval = 1.0 / max(1, config.recording.fps)
        self._last_record_t = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="control-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._close_writer()
        self.leader.disconnect()
        self.follower.disconnect()

    def submit(self, kind: str, payload: dict[str, Any] | None = None, timeout: float = 8.0) -> dict[str, Any]:
        reply: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self._commands.put(Command(kind=kind, payload=payload or {}, reply=reply))
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            return {"ok": False, "error": f"command '{kind}' timed out"}

    def log(self, level: str, message: str) -> None:
        entry = {"level": level, "message": message, "t": time.strftime("%H:%M:%S")}
        self.logs.append(entry)
        self.logs = self.logs[-80:]
        logger.log(logging.ERROR if level == "error" else logging.INFO, message)

    def snapshot(self) -> dict[str, Any]:
        elapsed = time.perf_counter() - self.task_t0 if self.task_t0 else 0.0
        rec_elapsed = time.perf_counter() - self.episode_t0 if self.episode_t0 else 0.0
        return {
            "ts": time.time(),
            "mode": self.mode,
            "fps": round(self.loop_fps, 1),
            "robot": self.follower.snapshot(),
            "leader": self.leader.snapshot(),
            "cameras": self.cameras.snapshots(),
            "joints": self.joints,
            "goal": self.latched,
            "action": self.action,
            "hold": self.hold_when_idle,
            "task": {
                "kind": self.mode,
                "elapsed_s": round(elapsed, 2),
                "episode_index": self.episode_index,
                "episode_elapsed_s": round(rec_elapsed, 2) if self.writer else None,
                "episode_time_s": self.episode_time_s if self.writer else None,
                "recording": self.writer is not None,
                "session_id": None if self.writer is None else self.writer.session_id,
                "policy_path": None if self.loaded_policy is None else self.loaded_policy.path,
                "task": self.rollout_task,
                "message": self.last_error,
            },
            "logs": self.logs[-12:],
        }

    def _reply(self, cmd: Command, **kwargs: Any) -> None:
        if cmd.reply is not None:
            cmd.reply.put(kwargs)

    def _close_writer(self) -> Path | None:
        if self.writer is None:
            return None
        path = self.writer.close()
        self.log("info", f"session saved: {path}")
        self.writer = None
        self.record_kind = None
        return path

    def _maybe_record(self, kind: str) -> None:
        if self.writer is None:
            return
        now = time.perf_counter()
        if now - self._last_record_t < self._record_interval:
            return
        self._last_record_t = now
        self.writer.add_frame(
            self.joints,
            self.action or self.latched,
            self.cameras.latest_bgr_map(),
            episode_index=self.episode_index,
            kind=kind,
        )

    def _run(self) -> None:
        if self.config.robot.auto_connect:
            self._connect_follower()
        period = 1.0 / max(1.0, self.config.control.fps)
        fps_n = 0
        fps_t = time.perf_counter()
        while not self._stop.is_set():
            t0 = time.perf_counter()
            self._drain_commands()
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

    def _connect_follower(self) -> None:
        try:
            self.follower.connect()
            pose = self.follower.get_pose()
            self.joints = dict(pose)
            self.latched = dict(pose)
            self.mode = "idle"
            self.last_error = None
            self.log("info", f"follower connected on {self.config.robot.port}")
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self.follower.error = str(exc)
            self.mode = "offline"
            self.log("error", f"follower connect failed: {exc}")

    def _drain_commands(self) -> None:
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                self._handle(cmd)
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                self.log("error", f"{cmd.kind}: {exc}")
                self._reply(cmd, ok=False, error=str(exc))

    def _handle(self, cmd: Command) -> None:
        kind = cmd.kind
        p = cmd.payload
        if kind == "connect_robot":
            self._connect_follower()
            self._reply(cmd, ok=self.follower.connected, error=self.follower.error)
        elif kind == "disconnect_robot":
            self._close_writer()
            self.mode = "offline"
            self.follower.disconnect()
            self._reply(cmd, ok=True)
        elif kind == "connect_leader":
            self.leader.connect()
            self.log("info", f"leader connected on {self.config.leader.port}")
            self._reply(cmd, ok=True)
        elif kind == "disconnect_leader":
            if self.mode in {"teleop", "record"}:
                self.mode = "idle"
            self.leader.disconnect()
            self._reply(cmd, ok=True)
        elif kind == "jog":
            if not self.follower.connected:
                raise RuntimeError("follower not connected")
            if self.mode in {"teleop", "record", "rollout"}:
                raise RuntimeError(f"cannot jog while {self.mode} is running")
            current = self.joints or self.follower.get_pose()
            target = merge_partial(current, p.get("joints") or {})
            self._slew_start = dict(current)
            self._slew_goal = target
            self._slew_t0 = time.perf_counter()
            self._slew_duration = float(p.get("duration_s") or self.config.control.jog_duration_s)
            self.mode = "jogging"
            self.log("info", f"jog {list((p.get('joints') or {}).keys())} in {self._slew_duration:.1f}s")
            self._reply(cmd, ok=True, target=target)
        elif kind == "preset":
            name = str(p.get("name", "home"))
            if name not in PRESETS:
                raise ValueError(f"unknown preset '{name}'")
            cmd.payload = {"joints": dict(PRESETS[name]), "duration_s": p.get("duration_s")}
            cmd.kind = "jog"
            self._handle(cmd)
        elif kind == "hold":
            self.hold_when_idle = bool(p.get("enabled", True))
            self._reply(cmd, ok=True, hold=self.hold_when_idle)
        elif kind == "estop":
            self._close_writer()
            self.mode = "estop"
            self.hold_when_idle = False
            self._slew_goal = None
            if self.follower.connected:
                self.follower.disable_torque()
            self.log("error", "E-STOP — torque disabled")
            self._reply(cmd, ok=True)
        elif kind == "resume":
            if not self.follower.connected:
                self._connect_follower()
            else:
                self.follower.enable_torque()
                pose = self.follower.get_pose()
                self.joints = dict(pose)
                self.latched = dict(pose)
            self.hold_when_idle = self.config.control.hold_when_idle
            self.mode = "idle" if self.follower.connected else "offline"
            self.log("info", "torque re-enabled, idle")
            self._reply(cmd, ok=True)
        elif kind == "teleop_start":
            if not self.follower.connected:
                raise RuntimeError("follower not connected")
            if not self.leader.connected:
                self.leader.connect()
            self.mode = "teleop"
            self.task_t0 = time.perf_counter()
            self.log("info", "teleop started")
            self._reply(cmd, ok=True)
        elif kind == "teleop_stop":
            if self.mode == "teleop":
                self.mode = "idle"
                if self.joints:
                    self.latched = dict(self.joints)
            self._reply(cmd, ok=True)
        elif kind == "record_start":
            if not self.follower.connected:
                raise RuntimeError("follower not connected")
            if not self.leader.connected:
                self.leader.connect()
            self._close_writer()
            self.episode_index = 0
            self.episode_time_s = float(p.get("episode_time_s") or self.config.recording.default_episode_time_s)
            self.writer = SessionWriter(
                self.config.recording.root,
                kind="record",
                fps=int(p.get("fps") or self.config.recording.fps),
                extra_meta={
                    "task": p.get("task") or "",
                    "repo_id": p.get("repo_id") or "",
                },
            )
            self.record_kind = "record"
            self.mode = "record"
            self.task_t0 = time.perf_counter()
            self.episode_t0 = self.task_t0
            self._last_record_t = 0.0
            self.log("info", f"recording {self.writer.session_id}")
            self._reply(cmd, ok=True, session_id=self.writer.session_id)
        elif kind == "record_stop":
            path = self._close_writer()
            self.mode = "idle" if self.follower.connected else "offline"
            self._reply(cmd, ok=True, path=None if path is None else str(path))
        elif kind == "record_next":
            if self.writer is None:
                raise RuntimeError("not recording")
            self.episode_index += 1
            self.episode_t0 = time.perf_counter()
            self.log("info", f"episode {self.episode_index}")
            self._reply(cmd, ok=True, episode_index=self.episode_index)
        elif kind == "rollout_start":
            if not self.follower.connected:
                raise RuntimeError("follower not connected")
            path = str(p.get("policy_path") or "")
            if not path:
                raise ValueError("policy_path is required")
            self.log("info", f"loading policy {path}")
            self.loaded_policy = load_policy(
                path,
                device=str(p.get("device") or self.config.rollout.device),
                task=str(p.get("task") or ""),
                robot_type=self.config.robot.type,
                rename_map=self.config.rollout.rename_map,
            )
            self.loaded_policy.reset()
            self.rollout_task = self.loaded_policy.task
            duration = float(p.get("duration_s") or self.config.rollout.default_duration_s)
            self.task_t0 = time.perf_counter()
            self.task_deadline = None if duration <= 0 else self.task_t0 + duration
            if p.get("record"):
                self._close_writer()
                self.episode_index = 0
                self.writer = SessionWriter(
                    self.config.recording.root,
                    kind="rollout",
                    fps=int(p.get("fps") or self.config.recording.fps),
                    extra_meta={
                        "task": self.rollout_task,
                        "policy_path": path,
                        "device": self.loaded_policy.device,
                    },
                )
                self.record_kind = "rollout"
                self.episode_t0 = self.task_t0
                self._last_record_t = 0.0
            self.mode = "rollout"
            self.log("info", f"rollout started ({self.loaded_policy.policy.__class__.__name__})")
            self._reply(
                cmd,
                ok=True,
                session_id=None if self.writer is None else self.writer.session_id,
            )
        elif kind == "rollout_stop":
            path = self._close_writer()
            self.loaded_policy = None
            self.mode = "idle" if self.follower.connected else "offline"
            if self.joints:
                self.latched = dict(self.joints)
            self._reply(cmd, ok=True, path=None if path is None else str(path))
        else:
            raise ValueError(f"unknown command '{kind}'")

    def _tick(self) -> None:
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
            elif self.mode == "idle" and self.hold_when_idle and self.latched:
                self.follower.send_pose(self.latched)
                self.action = dict(self.latched)
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
            self.mode = "idle"

    def _tick_teleop(self) -> None:
        pose = self.leader.get_action_pose()
        self.follower.send_pose(pose)
        self.action = dict(pose)
        self.latched = dict(pose)

    def _tick_record(self) -> None:
        self._tick_teleop()
        self._maybe_record("record")
        if time.perf_counter() - self.episode_t0 >= self.episode_time_s:
            self.episode_index += 1
            self.episode_t0 = time.perf_counter()
            self.log("info", f"auto-advance episode {self.episode_index}")

    def _tick_rollout(self) -> None:
        if self.loaded_policy is None:
            self.mode = "idle"
            return
        if self.task_deadline is not None and time.perf_counter() >= self.task_deadline:
            self.log("info", "rollout duration reached")
            self._close_writer()
            self.loaded_policy = None
            self.mode = "idle"
            self.latched = dict(self.joints)
            return
        images = self.cameras.latest_rgb_map()
        pose = predict_pose(self.loaded_policy, self.joints, images)
        self.follower.send_pose(pose)
        self.action = dict(pose)
        self.latched = dict(pose)
        self._maybe_record("rollout")
