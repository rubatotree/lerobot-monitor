"""Process-wide runtime: cameras + control loop + latest snapshot."""

from __future__ import annotations

import threading
from typing import Any

from pathlib import Path

from .cameras import CameraHub
from .config import MonitorConfig
from .dataset_hub import DatasetRegistry
from .leader import LeaderArm
from .library import VideoLibrary
from .loop import ControlLoop
from .model_hub import ModelRegistry
from .policy import load_policy
from .policy_residency import PolicyResidencyManager
from .robot import FollowerArm
from .robot_models import RobotModelRegistry
from .runtime import format_runtime, probe_runtime
from .session import list_sessions
from .snapshots import SnapshotLibrary
from .store import JsonStore
from .types import JOINT_LIMITS, JOINT_ORDER, PRESETS


class RuntimeHub:
    def __init__(self, config: MonitorConfig) -> None:
        self.config = config
        self.store = JsonStore(config.store_path)
        self.cameras = CameraHub(config.cameras, self.store)
        self.videos = VideoLibrary(config.videos_root())
        self.datasets = self.videos
        self.snapshots = SnapshotLibrary(config.snapshots_root())
        self.model_registry = ModelRegistry(
            self.store,
            [Path(p) for p in config.library.models_roots],
        )
        self.robot_model_registry = RobotModelRegistry(
            self.store,
            Path(config.robot_models.root),
            max_file_mb=config.robot_models.max_file_mb,
            max_bundle_mb=config.robot_models.max_bundle_mb,
        )
        active_robot_model = self.robot_model_registry.active()
        if active_robot_model:
            self.config.virtual_follower.model_id = active_robot_model
        dataset_roots = list(config.library.dataset_roots)
        if config.library.datasets_root:
            dataset_roots.append(Path(config.library.datasets_root))
        self.dataset_registry = DatasetRegistry(self.store, dataset_roots)
        self.follower = FollowerArm(config.robot)
        self.leader = LeaderArm(config.leader)
        self._snapshot: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.runtime = probe_runtime()
        self.policy_residency = PolicyResidencyManager(
            load_policy,
            robot_type=config.robot.type,
            rename_map=config.rollout.rename_map,
        )
        self.loop = ControlLoop(
            config,
            self.cameras,
            self.follower,
            self.leader,
            on_snapshot=self._store_snapshot,
            store=self.store,
            policy_residency=self.policy_residency,
        )

    def _store_snapshot(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._snapshot = snapshot

    def start(self) -> None:
        self.cameras.start()
        self.loop.start()
        self._restore_active_hardware_preset()

    def _trim_video_library(self) -> None:
        for row in self.videos.trim_all_to_first_episode():
            dataset_id = str(row.get("id") or "")
            first = row.get("trimmed_from_episode")
            if dataset_id and first is not None:
                self.store.remap_episode_overrides("video", dataset_id, {str(int(first)): 0})
                self.store.delete_episode_view("video", dataset_id)

    def _restore_active_hardware_preset(self) -> None:
        ui = self.store.ui()
        name = str(ui.get("active_hardware_preset") or "").strip()
        if not name:
            self.loop.log("info", "hardware preset auto-restore skipped: no active preset")
            return
        preset = self.store.presets("hardware").get(name)
        if not isinstance(preset, dict):
            self.loop.log(
                "error",
                f'hardware preset auto-restore skipped: "{name}" no longer exists',
            )
            return
        self.loop.submit_nowait("hardware_apply", {"name": name, "preset": preset})

    def stop(self) -> None:
        try:
            self.loop.stop()
        except BaseException as loop_error:
            try:
                self.cameras.stop()
            except BaseException as camera_error:
                loop_error.add_note(f"camera shutdown also failed: {camera_error}")
            raise
        else:
            self.cameras.stop()
        finally:
            self.policy_residency.close()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._snapshot:
                data = dict(self._snapshot)
            else:
                data = self.loop.snapshot()
        data["runtime"] = dict(self.runtime)
        data["runtime_label"] = format_runtime(self.runtime)
        data["dataset_transfers"] = self.dataset_transfers()
        data["model_residency"] = self.policy_residency.all_statuses()
        data["model_gpu_process"] = self.policy_residency.process_gpu_memory()
        return data

    def dataset_transfers(self) -> list[dict[str, Any]]:
        """In-flight and recently finished Hub dataset transfers."""
        return self.dataset_registry.transfers.states()

    def static_meta(self) -> dict[str, Any]:
        return {
            "joints": list(JOINT_ORDER),
            "limits": {name: {"min": lo, "max": hi} for name, (lo, hi) in JOINT_LIMITS.items()},
            "presets": PRESETS,
            "saved_presets": self.store.presets(),
            "ui": self.store.ui(),
            "active_hardware_preset": self.store.ui().get("active_hardware_preset"),
            "cameras": self.cameras.snapshots(),
            "recording_root": str(self.config.recording.root),
            "control_fps": self.config.control.fps,
            "robot": {
                "type": self.config.robot.type,
                "port": self.config.robot.port,
                "id": self.config.robot.id,
                "use_degrees": self.config.robot.use_degrees,
            },
            "virtual_follower": {
                "enabled": self.config.virtual_follower.enabled,
                "auto_connect": self.config.virtual_follower.auto_connect,
                "model_id": self.robot_model_registry.active(),
                "port": "virtual://preview",
            },
            "leader": {
                "type": self.config.leader.type,
                "port": self.config.leader.port,
                "id": self.config.leader.id,
                "use_degrees": self.config.leader.use_degrees,
            },
            "recording": {
                "fps": self.config.recording.fps,
                "action_fps": self.config.recording.action_fps,
                "video_fps": self.config.recording.video_fps,
                "default_episode_time_s": self.config.recording.default_episode_time_s,
                "default_reset_time_s": self.config.recording.default_reset_time_s,
                "default_num_episodes": self.config.recording.default_num_episodes,
                "video_format": self.config.recording.video_format,
                "streaming_encoding": self.config.recording.streaming_encoding,
                "encoder_threads": self.config.recording.encoder_threads,
                "root": str(self.config.videos_root()),
            },
            "runtime": dict(self.runtime),
            "runtime_label": format_runtime(self.runtime),
        }

    def sessions(self) -> list[dict[str, Any]]:
        items = self.videos.list()
        extra_root = Path(self.config.recording.root)
        if extra_root.resolve() != self.videos.root.resolve():
            items.extend(list_sessions(extra_root))
        leftover = Path("data/datasets")
        if leftover.resolve() != self.videos.root.resolve() and leftover.is_dir():
            items.extend(list_sessions(leftover))
        for item in items:
            root = Path(item.get("path") or "")
            log_path = root / "run.log"
            if log_path.is_file():
                try:
                    lines = log_path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    lines = []
                item["log_tail"] = lines[-8:]
                item["session_id"] = item.get("session_id") or item.get("id")
        return items

    def hf_datasets(self) -> list[dict[str, Any]]:
        return self.dataset_registry.list()

    def resolve_dataset(self, repo_id: str) -> dict[str, Any]:
        return self.dataset_registry.get(repo_id)

    def models(self) -> list[dict[str, Any]]:
        return [
            {**row, "residency": self.policy_residency.status(str(row.get("path") or ""))}
            for row in self.model_registry.list()
        ]
