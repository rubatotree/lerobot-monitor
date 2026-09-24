"""FastAPI application: REST, WebSocket telemetry, MJPEG cameras."""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import APIRouter, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .cameras import RemoteMjpegCamera, supported_resolutions
from .config import MonitorConfig
from .dataset_hub import (
    DatasetHubError,
    create_empty_dataset,
    download_hf_dataset,
    search_hf_datasets,
)
from .hub import RuntimeHub
from .library import hub_cache_repo_dir, lerobot_home, library_metadata
from .record_dataset import RecordDatasetSession, recover_record_publish
from .pathutil import ensure_lerobot_on_path
from .session import safe_cam_name
from .metrics import evaluate_action_chunk
from .model_hub import ModelHubError, search_hf_models
from .robot_models import RobotModelError, search_hf_robot_models
from .preview import (
    find_lerobot_video,
    lerobot_episode_count,
    lerobot_episode_payload,
    local_episode_payload,
)
from .snapshots import SnapshotTooLargeError, _decode_camera_payloads
from .store import PRESET_KINDS
from .types import JOINT_ORDER

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


class JogBody(BaseModel):
    joints: dict[str, float] = Field(default_factory=dict)
    duration_s: float | None = None
    live: bool = False
    source: str = "manual"
    max_speed: float | None = None


class PresetBody(BaseModel):
    name: str = "home"
    duration_s: float | None = None


class PresetRenameBody(BaseModel):
    name: str


class HardwareApplyBody(BaseModel):
    name: str
    force: bool = False


class ForceDisconnectBody(BaseModel):
    role: str = "all"


class HoldBody(BaseModel):
    enabled: bool = True


class RecordStartBody(BaseModel):
    task: str = ""
    repo_id: str = ""
    episode_time_s: float | None = None
    reset_time_s: float | None = None
    num_episodes: int | None = None
    fps: int | None = None
    action_fps: int | None = None
    video_fps: int | None = None
    resume: bool = False
    video_id: str | None = None
    dataset_id: str | None = None
    format: str | None = None
    root: str | None = None
    streaming_encoding: bool | None = None
    deferred_encoding: bool | None = None
    encoder_threads: int | None = None
    video: bool | None = None
    merge: bool | None = None
    auto_record: bool | None = None
    auto_next: bool = False
    resume_speed: float = 30.0


class RecordControlBody(BaseModel):
    session_id: str
    operation_id: str
    version: int
    resume_speed: float | None = None


class RolloutStartBody(BaseModel):
    policy_path: str
    task: str = ""
    duration_s: float | None = None
    device: str | None = None
    record: bool | None = None
    auto_record: bool | None = None
    fps: int | None = None
    policy_fps: int | None = None
    action_fps: int | None = None
    video_fps: int | None = None
    streaming_encoding: bool | None = None
    deferred_encoding: bool | None = None
    encoder_threads: int | None = None
    video: bool | None = None
    extra: dict[str, str] | None = None
    dataset_id: str | None = None
    format: str | None = None


class DatasetCreateBody(BaseModel):
    name: str = ""
    task: str = ""
    repo_id: str = ""
    fps: int | None = None
    action_fps: int | None = None
    video_fps: int | None = None


class ReorderBody(BaseModel):
    order: list[int] = Field(default_factory=list)


class EpisodeReorderBody(BaseModel):
    kind: str
    id: str
    order: list[int] = Field(default_factory=list)


class EpisodeEditBody(BaseModel):
    kind: str
    id: str
    episode: int
    name: str | None = None
    task: str | None = None
    note: str | None = None


class LibraryEditBody(BaseModel):
    kind: str
    id: str
    name: str | None = None
    note: str | None = None
    notes: list[str] | None = None
    description: str | None = None
    task: str | None = None
    repo_id: str | None = None
    private: bool | None = None
    remote: str | None = None
    revision: str | None = None
    path: str | None = None
    metadata: dict[str, Any] | None = None


class SnapshotCameraBody(BaseModel):
    key: str
    jpeg_base64: str


class SnapshotCreateBody(BaseModel):
    name: str = ""
    task: str = ""
    note: str = ""
    notes: list[str] | None = None
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    origin: str = "hardware"
    source: dict[str, Any] | None = None
    joints: dict[str, float] = Field(default_factory=dict)
    cameras: list[SnapshotCameraBody] = Field(default_factory=list)


class SnapshotUpdateBody(BaseModel):
    name: str | None = None
    task: str | None = None
    note: str | None = None
    notes: list[str] | None = None
    description: str | None = None
    metadata: dict[str, Any] | None = None
    origin: str | None = None
    source: dict[str, Any] | None = None
    joints: dict[str, float] | None = None


class DebugInferBody(BaseModel):
    policy_path: str
    task: str = ""
    device: str | None = None
    extra: dict[str, str] = Field(default_factory=dict)
    chunk_size: int = 16
    fps: float = 30.0
    camera_map: dict[str, str] = Field(default_factory=dict)
    source: dict[str, Any] | None = None
    joints: dict[str, float] = Field(default_factory=dict)
    cameras: list[SnapshotCameraBody] = Field(default_factory=list)
    reference: list[dict[str, float]] = Field(default_factory=list)


class ModelRegisterBody(BaseModel):
    remote: str
    name: str = ""
    revision: str = ""
    note: str = ""
    download: bool = True


class ModelSaveBody(BaseModel):
    name: str | None = None
    remote: str | None = None
    path: str | None = None
    revision: str | None = None


class RobotModelInstallBody(BaseModel):
    remote: str
    name: str = ""
    revision: str = ""
    download: bool = True


class RobotModelActiveBody(BaseModel):
    id: str


class VirtualFollowerBody(BaseModel):
    model_id: str = ""
    note: str | None = None


class DatasetDownloadBody(BaseModel):
    remote: str
    revision: str = ""
    name: str = ""


class DatasetEmptyBody(BaseModel):
    name: str
    repo_id: str = ""
    private: bool = False
    fps: int = 15
    robot_type: str = ""
    cameras: list[dict[str, Any]] = Field(default_factory=list)


class AutoRecordBody(BaseModel):
    enabled: bool = True


class CaptureStartBody(BaseModel):
    fps: int | None = None
    action_fps: int | None = None
    video_fps: int | None = None
    format: str | None = None
    video_id: str | None = None
    dataset_id: str | None = None
    resume: bool = False
    task: str = ""
    name: str = ""
    repo_id: str = ""
    root: str | None = None
    merge: bool = True
    video: bool | None = None
    streaming_encoding: bool | None = None
    deferred_encoding: bool | None = None
    encoder_threads: int | None = None
    auto_record: bool | None = None


class ConnectBody(BaseModel):
    port: str | None = None
    id: str | None = None


class CameraFocusBody(BaseModel):
    autofocus: bool | None = None
    focus: float | None = None


class CameraResolutionBody(BaseModel):
    width: int
    height: int


class CameraStreamBody(BaseModel):
    enable: bool = True
    port: int | None = None


class CameraLabelBody(BaseModel):
    label: str = ""


class CameraFlagsBody(BaseModel):
    enabled: bool | None = None
    show_main: bool | None = None
    feed_robot: bool | None = None


class UiStateBody(BaseModel):
    record: dict[str, Any] | None = None
    rollout: dict[str, Any] | None = None
    joints: dict[str, Any] | None = None
    hold: bool | None = None
    hardware: dict[str, Any] | None = None
    auto_record: bool | None = None
    selected_dataset: str | None = None
    selected_video: str | None = None
    active_hardware_preset: str | None = None


def _normalize_prefix(base_path: str) -> str:
    prefix = (base_path or "").strip()
    if not prefix or prefix == "/":
        return ""
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    return prefix.rstrip("/")


def _json_safe(value: Any) -> Any:
    """Replace JSON-forbidden floating-point sentinels at the API boundary."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _force_remove(function: Any, path: str, excinfo: Any) -> None:
    """Retry a failed unlink/rmdir after clearing a read-only attribute.

    Windows refuses to delete read-only files, which is how some Hub cache
    payloads arrive; retrying once makes library deletion reliable.
    """
    del excinfo
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass
    function(path)


def create_app(config: MonitorConfig, *, apply_prefix: bool = True) -> FastAPI:
    hub = RuntimeHub(config)
    prefix = _normalize_prefix(config.server.base_path) if apply_prefix else ""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            hub.start()
            yield
        finally:
            hub.stop()

    app = FastAPI(title="LeRobot Monitor", lifespan=lifespan)
    app.state.hub = hub
    app.state.base_path = prefix or _normalize_prefix(config.server.base_path)

    router = APIRouter()
    library_mutation_lock = asyncio.Lock()

    def _recording_rates(payload: dict[str, Any]) -> tuple[int, int]:
        legacy = payload.get("fps")
        action_fps = int(
            payload.get("action_fps")
            if payload.get("action_fps") is not None
            else legacy if legacy is not None else config.recording.action_fps
        )
        video_fps = int(
            payload.get("video_fps")
            if payload.get("video_fps") is not None
            else legacy if legacy is not None else config.recording.video_fps
        )
        if action_fps <= 0 or video_fps <= 0:
            raise HTTPException(400, "action_fps and video_fps must be positive")
        control_limit = max(1, int(config.control.fps))
        if action_fps > control_limit:
            raise HTTPException(400, f"action_fps={action_fps} exceeds control loop capacity ({control_limit} fps)")
        if video_fps > control_limit:
            raise HTTPException(400, f"video_fps={video_fps} exceeds camera sampling capacity ({control_limit} fps)")
        return action_fps, video_fps

    def _recording_payload(body: BaseModel, *, exclude_none: bool = False) -> dict[str, Any]:
        payload = body.model_dump(exclude_none=exclude_none)
        if payload.get("resume"):
            for field in ("fps", "action_fps", "video_fps"):
                value = payload.get(field)
                if value is not None and int(value) <= 0:
                    raise HTTPException(400, f"{field} must be positive")
            # Missing values must remain absent so the control loop can inherit
            # each rate independently from the existing dataset metadata.
            return payload
        action_fps, video_fps = _recording_rates(payload)
        payload["action_fps"] = action_fps
        payload["video_fps"] = video_fps
        return payload

    @router.get("/")
    async def index() -> FileResponse:
        page = STATIC_DIR / "index.html"
        if not page.is_file():
            raise HTTPException(404, "frontend not packaged")
        return FileResponse(page)

    @router.get("/api/status")
    async def status() -> dict[str, Any]:
        return hub.snapshot()

    @router.get("/api/meta")
    async def meta() -> dict[str, Any]:
        data = hub.static_meta()
        data["base_path"] = app.state.base_path
        return data

    @router.get("/api/sessions")
    async def sessions() -> list[dict[str, Any]]:
        return await asyncio.to_thread(hub.sessions)

    async def _submit(kind: str, payload: dict[str, Any] | None = None, timeout: float = 8.0) -> dict[str, Any]:
        result = await asyncio.to_thread(hub.loop.submit, kind, payload or {}, timeout)
        if not result.get("ok", False):
            raise HTTPException(400, result.get("error") or "command failed")
        return result

    async def _submit_logged(
        kind: str,
        message: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 8.0,
    ) -> dict[str, Any]:
        data = dict(payload or {})
        token = hub.loop.note_pending(kind, message)
        data["_start_generation"] = token
        try:
            return await _submit(kind, data, timeout=timeout)
        finally:
            hub.loop.clear_pending(token)

    def _enqueue(kind: str, message: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = dict(payload or {})
        token = hub.loop.note_pending(kind, message)
        data["_start_generation"] = token
        hub.loop.submit_nowait(kind, data)
        return {"ok": True, "accepted": True, "kind": kind}

    @router.get("/api/ports")
    async def list_ports() -> list[dict[str, Any]]:
        from .ports import list_serial_ports

        return list_serial_ports()

    @router.post("/api/robot/connect")
    async def robot_connect(body: ConnectBody = ConnectBody()) -> dict[str, Any]:
        payload = body.model_dump(exclude_none=True)
        if not payload.get("port"):
            payload.pop("port", None)
        if not payload.get("id"):
            payload.pop("id", None)
        return await _submit("connect_robot", payload, timeout=15.0)

    @router.post("/api/robot/disconnect")
    async def robot_disconnect() -> dict[str, Any]:
        return await _submit("disconnect_robot")

    @router.post("/api/virtual-follower/connect")
    async def virtual_follower_connect(body: VirtualFollowerBody = VirtualFollowerBody()) -> dict[str, Any]:
        payload = {"model_id": body.model_id} if body.model_id else {}
        return await _submit("virtual_connect", payload)

    @router.post("/api/virtual-follower/disconnect")
    async def virtual_follower_disconnect() -> dict[str, Any]:
        return await _submit("virtual_disconnect")

    @router.post("/api/leader/connect")
    async def leader_connect(body: ConnectBody = ConnectBody()) -> dict[str, Any]:
        payload = body.model_dump(exclude_none=True)
        if not payload.get("port"):
            payload.pop("port", None)
        if not payload.get("id"):
            payload.pop("id", None)
        return await _submit("connect_leader", payload, timeout=15.0)

    @router.post("/api/leader/disconnect")
    async def leader_disconnect() -> dict[str, Any]:
        return await _submit("disconnect_leader")

    @router.post("/api/joints/read")
    async def read_pose() -> dict[str, Any]:
        return await _submit("read_pose", timeout=15.0)

    @router.post("/api/joints")
    async def joints(body: JogBody) -> dict[str, Any]:
        unknown = [name for name in body.joints if name not in JOINT_ORDER]
        if unknown:
            raise HTTPException(400, f"unknown joints: {unknown}")
        if body.source not in {"manual", "leader"}:
            raise HTTPException(400, f"unknown joint source '{body.source}'")
        if body.max_speed is not None:
            if not math.isfinite(body.max_speed) or body.max_speed <= 0:
                raise HTTPException(400, "max_speed must be a positive finite number")
        return await _submit(
            "jog",
            {
                "joints": body.joints,
                "duration_s": body.duration_s,
                "live": body.live,
                "source": body.source,
                "max_speed": body.max_speed,
            },
        )

    @router.post("/api/joints/preset")
    async def preset(body: PresetBody) -> dict[str, Any]:
        return await _submit_logged("preset", f"relax/preset '{body.name}' requested", {"name": body.name, "duration_s": body.duration_s})

    @router.post("/api/hold")
    async def hold(body: HoldBody) -> dict[str, Any]:
        return await _submit("hold", {"enabled": body.enabled})

    @router.post("/api/estop")
    async def estop() -> dict[str, Any]:
        return hub.loop.request_estop()

    @router.post("/api/task/stop")
    async def task_stop() -> dict[str, Any]:
        return hub.loop.request_stop()

    @router.post("/api/task/force_stop")
    async def task_force_stop() -> dict[str, Any]:
        result = await asyncio.to_thread(hub.loop.request_force_stop)
        if not result.get("ok", False):
            raise HTTPException(400, result.get("error") or "force stop failed")
        return result

    @router.post("/api/scan")
    async def scan_devices() -> dict[str, Any]:
        from .ports import list_serial_ports

        hub.cameras.sync_remote_cameras()
        cameras = hub.cameras.rescan()
        return {"cameras": cameras, "ports": list_serial_ports()}

    @router.post("/api/joints/read_leader")
    async def read_leader_pose() -> dict[str, Any]:
        return await _submit("read_leader", timeout=15.0)

    @router.post("/api/resume")
    async def resume() -> dict[str, Any]:
        return await _submit("resume")

    @router.post("/api/teleop/start")
    async def teleop_start(body: CaptureStartBody = CaptureStartBody()) -> dict[str, Any]:
        return await _submit_logged(
            "teleop_start",
            "teleop requested",
            _recording_payload(body, exclude_none=True),
            timeout=15.0,
        )

    @router.post("/api/teleop/stop")
    async def teleop_stop() -> dict[str, Any]:
        return await _submit("teleop_stop")

    @router.post("/api/record/start")
    async def record_start(body: RecordStartBody) -> dict[str, Any]:
        if not body.dataset_id:
            raise HTTPException(400, "choose a Dataset before recording")
        if not body.task.strip():
            raise HTTPException(400, "enter a task before recording")
        if not math.isfinite(body.resume_speed) or not 0 <= body.resume_speed <= 720:
            raise HTTPException(400, "resume speed must be between 0 and 720 degrees per second")
        token = hub.loop.note_pending("record_start", "preparing selected dataset")
        hub.loop.update_record_preparation(body.dataset_id, "Checking Dataset")
        try:
            def prepare() -> dict[str, Any]:
                ensure_lerobot_on_path()
                from lerobot.configs.video import RGBEncoderConfig
                from lerobot.datasets.lerobot_dataset import LeRobotDataset  # noqa: F401 - validate availability

                RGBEncoderConfig(vcodec="h264", preset="ultrafast", crf=22)
                row = hub.resolve_dataset(body.dataset_id or "")
                hub.dataset_registry.assert_not_transferring(body.dataset_id or "")
                root = Path(str(row.get("path") or "")).expanduser().resolve()
                if not root.is_dir():
                    raise ValueError("selected Dataset has no local files; download it first")
                if hub.loop.recording_mutation_lock.locked():
                    raise ValueError("another dataset write is still active")
                if hub_cache_repo_dir(root, str(row.get("repo_id") or "")) is None:
                    recover_record_publish(root)
                info = json.loads((root / "meta" / "info.json").read_text(encoding="utf-8"))
                fps = int(info.get("fps") or 0)
                snapshots = hub.cameras.snapshots()
                selected_cameras = [cam for cam in snapshots if cam.get("enabled") and cam.get("show_main")]
                cameras = {
                    str(cam.get("label") or cam.get("name")): f"observation.images.{safe_cam_name(str(cam.get('label') or cam.get('name')))}"
                    for cam in selected_cameras
                }
                camera_shapes = {
                    str(cam.get("label") or cam.get("name")): [int(cam["height"]), int(cam["width"]), 3]
                    for cam in selected_cameras
                }
                if len(cameras) != len(selected_cameras) or len(set(cameras.values())) != len(cameras):
                    raise ValueError("Record cameras need unique labels and Dataset keys")
                if fps <= 0 or fps > hub.config.control.fps:
                    raise ValueError("Dataset FPS exceeds the robot control rate")
                RecordDatasetSession.validate(root, cameras, fps, camera_shapes)
                if hub_cache_repo_dir(root, str(row.get("repo_id") or "")) is not None:
                    total = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
                    copied = 0
                    hub.loop.update_record_preparation(body.dataset_id or "", "Copying Hub cache to writable Dataset", 0, total)
                    destination = lerobot_home() / str(row.get("repo_id") or row.get("id") or "record")
                    if destination.exists():
                        destination = destination.parent / f"{destination.name}-record-{uuid.uuid4().hex[:8]}"
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
                    def copy_with_progress(source: str, target: str) -> str:
                        nonlocal copied
                        result = shutil.copy2(source, target)
                        copied += Path(source).stat().st_size
                        hub.loop.update_record_preparation(
                            body.dataset_id or "", "Copying Hub cache to writable Dataset", copied, total,
                        )
                        return result

                    try:
                        shutil.copytree(root, temporary, copy_function=copy_with_progress)
                        temporary.rename(destination)
                    except BaseException:
                        shutil.rmtree(temporary, ignore_errors=True)
                        raise
                    row = hub.dataset_registry.save(body.dataset_id or "", {"path": str(destination)})
                    root = destination
                hub.loop.update_record_preparation(body.dataset_id or "", "Opening Dataset")
                return {
                    "dataset_id": str(row.get("id") or body.dataset_id),
                    "dataset_repo_id": str(row.get("repo_id") or row.get("id") or body.dataset_id),
                    "dataset_path": str(root),
                    "dataset_fps": fps,
                    "dataset_episodes": int(info.get("total_episodes") or 0),
                    "camera_keys": cameras,
                }

            prepared = await asyncio.to_thread(prepare)
            payload = body.model_dump(exclude_none=True)
            payload.update(prepared)
            payload["_start_generation"] = token
            return await _submit("record_start", payload, timeout=15.0)
        except (OSError, ValueError, DatasetHubError) as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            hub.loop.clear_pending(token)

    @router.post("/api/record/stop")
    async def record_stop(body: RecordControlBody) -> dict[str, Any]:
        return await _submit("record_stop", body.model_dump())

    @router.post("/api/record/next")
    async def record_next(body: RecordControlBody) -> dict[str, Any]:
        return await _submit("record_next", body.model_dump())

    @router.post("/api/record/pause")
    async def record_pause(body: RecordControlBody) -> dict[str, Any]:
        return await _submit("record_pause", body.model_dump())

    @router.post("/api/record/back")
    async def record_back(body: RecordControlBody) -> dict[str, Any]:
        return await _submit("record_back", body.model_dump())

    @router.post("/api/record/retry")
    async def record_retry(body: RecordControlBody) -> dict[str, Any]:
        return await _submit("record_retry", body.model_dump())

    @router.post("/api/rollout/start")
    async def rollout_start(body: RolloutStartBody) -> dict[str, Any]:
        path = body.policy_path or "(no policy)"
        payload = body.model_dump()
        if payload.get("policy_fps") is None:
            payload["policy_fps"] = (
                payload["fps"] if payload.get("fps") is not None else hub.config.rollout.default_fps
            )
        if int(payload["policy_fps"]) <= 0:
            raise HTTPException(400, "policy_fps must be positive")
        return _enqueue("rollout_start", f"rollout requested — loading {path}", payload)

    @router.post("/api/rollout/stop")
    async def rollout_stop() -> dict[str, Any]:
        return await _submit("rollout_stop")

    @router.post("/api/capture/start")
    async def capture_start(body: CaptureStartBody = CaptureStartBody()) -> dict[str, Any]:
        return await _submit_logged(
            "capture_start",
            "video capture requested",
            _recording_payload(body, exclude_none=True),
            timeout=15.0,
        )

    @router.post("/api/capture/stop")
    async def capture_stop() -> dict[str, Any]:
        return await _submit("capture_stop")

    @router.post("/api/auto_record")
    async def auto_record(body: AutoRecordBody) -> dict[str, Any]:
        return await _submit("auto_record", {"enabled": body.enabled})

    def _episode_item(
        kind: str,
        source_id: str,
        index: int,
        *,
        playable: bool,
        row: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "index": int(index),
            "name": "",
            "task": "",
            "note": "",
            "playable": bool(playable),
        }
        if row:
            item["has_video"] = bool(row.get("videos"))
            item["task"] = str(row.get("task") or "")
        saved = hub.store.episode_overrides(kind, source_id).get(str(int(index)))
        if saved:
            for key in ("name", "task", "note"):
                if saved.get(key) is not None:
                    item[key] = str(saved[key])
        return item

    def _merge_episode_overrides(kind: str, source_id: str, episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            _episode_item(kind, source_id, int(row.get("index", i)), playable=True, row=row)
            for i, row in enumerate(episodes)
        ]

    def _merge_library_override(kind: str, source_id: str, row: dict[str, Any]) -> dict[str, Any]:
        merged = dict(row)
        saved = hub.store.library_override(kind, source_id)
        for field in ("name", "description", "task", "repo_id", "path", "source", "remote", "revision"):
            if field in saved:
                merged[field] = saved[field]
        merged["description"] = str(saved.get("description") or merged.get("description") or "")
        merged["metadata"] = library_metadata(kind, merged)
        if isinstance(saved.get("metadata"), dict):
            merged["metadata"].update(saved["metadata"])
        if kind == "video" and merged["description"]:
            parts = merged["description"].split(" · ")
            if parts[0].endswith(" episodes"):
                parts[0] = f"{merged['metadata'].get('episodes', 1)} episodes"
                merged["description"] = " · ".join(parts)
        if kind == "dataset":
            managed_name = merged.get("name") if merged.get("managed") else ""
            merged["display_name"] = str(
                saved.get("name")
                or managed_name
                or merged.get("title")
                or merged.get("repo_id")
                or merged.get("id")
                or source_id
            )
        else:
            merged["display_name"] = str(
                saved.get("name") or merged.get("name") or merged.get("repo_id") or merged.get("id") or source_id
            )
        return merged

    def _merge_snapshot_metadata(row: dict[str, Any]) -> dict[str, Any]:
        merged = dict(row)
        merged.pop("_notes_initialized", None)
        merged["display_name"] = str(merged.get("name") or merged.get("id") or "")
        legacy_notes = [str(note) for note in (merged.pop("notes", []) or []) if str(note).strip()]
        merged["description"] = str(merged.get("description") or "\n".join(legacy_notes))
        merged["metadata"] = library_metadata("snapshot", merged)
        return merged

    def _resolve_library_row(kind: str, source_id: str) -> dict[str, Any]:
        if kind == "video":
            row = hub.videos.get(source_id)
            row = _merge_library_override("video", source_id, row)
            row["episodes"] = _merge_episode_overrides("video", source_id, row.get("episodes") or [])
            return row
        if kind == "dataset":
            return _merge_library_override("dataset", source_id, hub.resolve_dataset(source_id))
        if kind == "model":
            row = next(
                (
                    item
                    for item in hub.models()
                    if str(item.get("id") or "") == str(source_id)
                ),
                None,
            )
            if row is None:
                raise KeyError(source_id)
            return _merge_library_override("model", source_id, row)
        if kind == "snapshot":
            return _merge_snapshot_metadata(hub.snapshots.get(source_id))
        raise ValueError(f"unknown library kind '{kind}'")

    def _open_library_folder(kind: str, source_id: str) -> None:
        # Resolve the ID on the server; a browser-supplied path must never launch a local process.
        if kind == "video":
            row = hub.videos.get(source_id)
        elif kind == "snapshot":
            row = hub.snapshots.get(source_id)
        elif kind == "dataset":
            row = hub.resolve_dataset(source_id)
        elif kind == "model":
            row = next((item for item in hub.models() if str(item.get("id") or "") == source_id), None)
            if row is None:
                raise KeyError(source_id)
        else:
            raise ValueError(f"unknown library kind '{kind}'")
        raw_path = row.get("path")
        if not raw_path:
            raise FileNotFoundError(f"{kind} '{source_id}' has no local folder")
        folder = Path(raw_path).expanduser().resolve(strict=True)
        if not folder.is_dir():
            raise FileNotFoundError(f"{kind} '{source_id}' has no local folder")
        if sys.platform == "win32":
            command = ["explorer.exe", str(folder)]
        elif sys.platform == "darwin":
            command = ["open", str(folder)]
        else:
            command = ["xdg-open", str(folder)]
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _delete_resource_path(path: str | Path | None) -> None:
        if not path:
            return
        try:
            target = Path(path).expanduser().resolve()
        except OSError:
            return
        if not target.exists() or target == Path(target.anchor):
            return
        if target.is_dir():
            shutil.rmtree(target, onexc=_force_remove)
        else:
            _force_remove(os.unlink, str(target), None)

    async def _delete_library_path(path: str | Path | None) -> None:
        # Retry transient Windows access conflicts while file handles close.
        for delay in (0.1, 0.2, 0.4, 0.8, 1.6, 2.0, None):
            try:
                await asyncio.to_thread(_delete_resource_path, path)
                return
            except OSError as exc:
                if getattr(exc, "winerror", None) not in (5, 32, 33) or delay is None:
                    raise
                await asyncio.sleep(delay)

    @router.get("/api/videos")
    async def list_videos() -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(hub.videos.list)
        for index, row in enumerate(rows):
            row = _merge_library_override("video", row["id"], row)
            rows[index] = row
            row["episodes"] = _merge_episode_overrides("video", row["id"], row.get("episodes") or [])
        return rows

    @router.post("/api/videos")
    async def create_video(body: DatasetCreateBody) -> dict[str, Any]:
        payload = body.model_dump(exclude_none=True)
        action_fps, video_fps = _recording_rates(payload)
        async with library_mutation_lock:
            return await asyncio.to_thread(
                hub.videos.create,
                body.name,
                fps=action_fps,
                action_fps=action_fps,
                video_fps=video_fps,
                task=body.task,
                repo_id=body.repo_id,
            )

    @router.post("/api/videos/{video_id}/duplicate")
    async def duplicate_video(video_id: str) -> dict[str, Any]:
        try:
            row = await asyncio.to_thread(hub.videos.duplicate, video_id)
            return _merge_library_override("video", str(row["id"]), row)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/api/videos/{video_id}")
    async def get_video(video_id: str) -> dict[str, Any]:
        try:
            row = await asyncio.to_thread(hub.videos.get, video_id)
        except FileNotFoundError:
            raise HTTPException(404, f"unknown video '{video_id}'") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        row = _merge_library_override("video", video_id, row)
        row["episodes"] = _merge_episode_overrides("video", video_id, row.get("episodes") or [])
        return row

    @router.delete("/api/videos/{video_id}")
    async def delete_video(video_id: str) -> dict[str, Any]:
        if not hub.loop.recording_mutation_lock.acquire(blocking=False):
            raise HTTPException(409, "cannot edit datasets while recording is active")
        try:
            await asyncio.to_thread(hub.videos.delete, video_id)
            await asyncio.to_thread(hub.store.delete_episode_overrides, "video", video_id)
            await asyncio.to_thread(hub.store.delete_library_override, "video", video_id)
        except FileNotFoundError:
            raise HTTPException(404, f"unknown video '{video_id}'") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            hub.loop.recording_mutation_lock.release()
        return {"ok": True}

    @router.delete("/api/videos/{video_id}/episodes/{index}")
    async def delete_video_episode(video_id: str, index: int) -> dict[str, Any]:
        if not hub.loop.recording_mutation_lock.acquire(blocking=False):
            raise HTTPException(409, "cannot edit datasets while recording is active")
        try:
            result = await asyncio.to_thread(hub.videos.delete_episode, video_id, index)
            await asyncio.to_thread(
                hub.store.remap_episode_overrides,
                "video",
                video_id,
                result.get("episode_index_map") or {},
            )
            return result
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            hub.loop.recording_mutation_lock.release()

    @router.post("/api/videos/{video_id}/episodes/reorder")
    async def reorder_video_episodes(video_id: str, body: ReorderBody) -> dict[str, Any]:
        if not hub.loop.recording_mutation_lock.acquire(blocking=False):
            raise HTTPException(409, "cannot edit datasets while recording is active")
        try:
            result = await asyncio.to_thread(hub.videos.reorder, video_id, body.order)
            await asyncio.to_thread(
                hub.store.remap_episode_overrides,
                "video",
                video_id,
                result.get("episode_index_map") or {},
            )
            return result
        except FileNotFoundError:
            raise HTTPException(404, f"unknown video '{video_id}'") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            hub.loop.recording_mutation_lock.release()

    @router.get("/api/videos/{video_id}/episodes/{index}/video/{cam}")
    async def video_episode_file(video_id: str, index: int, cam: str) -> FileResponse:
        try:
            path = await asyncio.to_thread(hub.videos.episode_browser_video, video_id, index, cam)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return FileResponse(path, media_type="video/mp4" if path.suffix == ".mp4" else "video/x-msvideo")

    @router.get("/api/videos/{video_id}/episodes/{index}/preview")
    async def video_episode_preview(video_id: str, index: int) -> FileResponse:
        try:
            path = await asyncio.to_thread(hub.videos.episode_preview, video_id, index)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return FileResponse(path, media_type="image/jpeg")

    @router.get("/api/datasets")
    async def list_datasets() -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(hub.hf_datasets)
        return [
            _merge_library_override("dataset", str(row.get("repo_id") or row.get("id") or ""), row)
            for row in rows
        ]

    @router.get("/api/datasets/search")
    async def search_datasets(q: str, limit: int = 20) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(
                search_hf_datasets,
                q,
                limit=max(1, min(int(limit), 50)),
            )
        except DatasetHubError as exc:
            raise HTTPException(502, str(exc)) from exc

    @router.get("/api/datasets/transfers")
    async def list_dataset_transfers() -> list[dict[str, Any]]:
        return await asyncio.to_thread(hub.dataset_transfers)

    @router.post("/api/datasets/download")
    async def download_dataset(body: DatasetDownloadBody) -> dict[str, Any]:
        async with library_mutation_lock:
            if hub.loop.recording_mutation_lock.locked():
                raise HTTPException(409, "cannot download a Dataset while recording is active")
            try:
                return await asyncio.to_thread(
                    hub.dataset_registry.start_download,
                    remote=body.remote,
                    name=body.name,
                    revision=body.revision,
                )
            except DatasetHubError as exc:
                raise HTTPException(400, str(exc)) from exc

    @router.post("/api/datasets/empty")
    async def create_empty_dataset_route(body: DatasetEmptyBody) -> dict[str, Any]:
        cameras = list(body.cameras or [])
        if not cameras:
            cameras = [
                {
                    "key": str(camera.get("label") or camera.get("name") or ""),
                    "width": camera.get("width"),
                    "height": camera.get("height"),
                }
                for camera in hub.cameras.snapshots()
                if camera.get("enabled") and camera.get("show_main")
            ]
        async with library_mutation_lock:
            try:
                created = await asyncio.to_thread(
                    create_empty_dataset,
                    name=body.name,
                    repo_id=body.repo_id,
                    fps=body.fps,
                    robot_type=body.robot_type or config.robot.type,
                    cameras=cameras,
                )
            except DatasetHubError as exc:
                raise HTTPException(400, str(exc)) from exc
            row = await asyncio.to_thread(
                hub.dataset_registry.register,
                remote=str(created["path"]),
                name=str(created.get("title") or body.name),
                revision="",
                repo_id=body.repo_id.strip(),
                private=body.private,
            )
        return _merge_library_override("dataset", str(row.get("id") or row.get("repo_id")), row)

    # Dataset ids are Hub repo ids, so they contain a slash: the path
    # converter is required or every card action would 404.
    @router.post("/api/datasets/{dataset_id:path}/download")
    async def download_dataset_local(dataset_id: str) -> dict[str, Any]:
        async with library_mutation_lock:
            if hub.loop.recording_mutation_lock.locked():
                raise HTTPException(409, "cannot replace a Dataset while recording is active")
            try:
                return await asyncio.to_thread(hub.dataset_registry.start_download, dataset_id=dataset_id)
            except FileNotFoundError as exc:
                raise HTTPException(404, str(exc)) from exc
            except DatasetHubError as exc:
                raise HTTPException(400, str(exc)) from exc

    @router.post("/api/datasets/{dataset_id:path}/upload")
    async def upload_dataset(dataset_id: str) -> dict[str, Any]:
        async with library_mutation_lock:
            if hub.loop.recording_mutation_lock.locked():
                raise HTTPException(409, "cannot upload a Dataset while recording is active")
            try:
                return await asyncio.to_thread(hub.dataset_registry.start_upload, dataset_id)
            except FileNotFoundError as exc:
                raise HTTPException(404, str(exc)) from exc
            except DatasetHubError as exc:
                raise HTTPException(400, str(exc)) from exc

    @router.post("/api/library/open-folder")
    async def open_library_folder(kind: str, id: str) -> dict[str, bool]:
        try:
            await asyncio.to_thread(_open_library_folder, kind, id)
        except (FileNotFoundError, KeyError) as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, f"could not open file manager: {exc}") from exc
        return {"ok": True}

    @router.put("/api/library")
    async def edit_library(body: LibraryEditBody) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in (
            ("name", body.name),
            ("description", body.description),
            ("task", body.task),
            ("repo_id", body.repo_id),
            ("private", body.private),
            ("remote", body.remote),
            ("revision", body.revision),
            ("path", body.path),
            ("metadata", body.metadata),
        ):
            if value is not None:
                payload[key] = value
        if body.note is not None:
            payload["description"] = body.note.strip()
        if body.notes is not None:
            payload["description"] = "\n".join(str(note).strip() for note in body.notes if str(note).strip())
        response_id = body.id
        try:
            if body.kind == "snapshot":
                await asyncio.to_thread(hub.snapshots.update, body.id, payload)
            elif body.kind == "dataset":
                # Saving dataset details must never sync: an address edit is
                # stored as-is and only the download action touches the Hub.
                async with library_mutation_lock:
                    if hub.loop.recording_mutation_lock.locked():
                        raise HTTPException(409, "cannot edit a Dataset while recording is active")
                    source_payload = {
                        key: value
                        for key, value in payload.items()
                        if key in {"name", "repo_id", "remote", "revision", "path", "private"}
                    }
                    if source_payload:
                        saved = await asyncio.to_thread(hub.dataset_registry.save, body.id, source_payload)
                        response_id = str(saved.get("id") or body.id)
                        if response_id != body.id:
                            await asyncio.to_thread(
                                hub.store.move_library_override, "dataset", body.id, response_id
                            )
                    metadata_payload = {
                        key: value
                        for key, value in payload.items()
                        if key in {"description", "task", "metadata"}
                    }
                    if metadata_payload:
                        await asyncio.to_thread(
                            hub.store.save_library_override, "dataset", response_id, metadata_payload
                        )
            elif body.kind == "model":
                source_payload = {
                    key: value
                    for key, value in payload.items()
                    if key in {"name", "remote", "revision", "path"}
                }
                if source_payload:
                    await asyncio.to_thread(hub.model_registry.save, body.id, source_payload)
                metadata_payload = {
                    key: value
                    for key, value in payload.items()
                    if key in {"name", "description", "metadata"}
                }
                if metadata_payload:
                    await asyncio.to_thread(hub.store.save_library_override, "model", body.id, metadata_payload)
            elif body.kind == "video":
                await asyncio.to_thread(hub.store.save_library_override, body.kind, body.id, payload)
            else:
                raise ValueError(f"unknown library kind '{body.kind}'")
            return await asyncio.to_thread(_resolve_library_row, body.kind, response_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except DatasetHubError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/api/library")
    async def delete_library(kind: str, id: str) -> dict[str, Any]:
        try:
            if kind == "video":
                row = await asyncio.to_thread(hub.videos.get, id)
                async with library_mutation_lock:
                    await asyncio.to_thread(hub.videos.delete, id)
                await asyncio.to_thread(hub.store.delete_episode_overrides, "video", id)
                await asyncio.to_thread(hub.store.delete_library_override, "video", id)
                await asyncio.to_thread(hub.store.delete_episode_view, "video", id)
            elif kind == "snapshot":
                row = await asyncio.to_thread(hub.snapshots.get, id)
                await asyncio.to_thread(hub.snapshots.delete, id)
            elif kind == "model":
                row = next(
                    (
                        item
                        for item in await asyncio.to_thread(hub.models)
                        if str(item.get("id") or "") == str(id)
                    ),
                    None,
                )
                if row is None:
                    raise FileNotFoundError(id)
                target = hub_cache_repo_dir(row.get("path"), str(row.get("repo_id") or ""), kind="model") or row.get("path")
                try:
                    await _delete_library_path(target)
                except OSError as exc:
                    raise HTTPException(400, f"could not delete {target}: {exc}") from exc
                await asyncio.to_thread(hub.model_registry.delete, id)
                await asyncio.to_thread(hub.store.delete_library_override, "model", id)
            elif kind == "dataset":
                async with library_mutation_lock:
                    if hub.loop.recording_mutation_lock.locked():
                        raise HTTPException(409, "cannot delete a Dataset while recording is active")
                    row = await asyncio.to_thread(hub.resolve_dataset, id)
                    await asyncio.to_thread(hub.dataset_registry.assert_not_transferring, id)
                    # Delete bytes first. A failed filesystem operation must not
                    # orphan them by removing the Library record prematurely.
                    target = hub_cache_repo_dir(row.get("path"), str(row.get("repo_id") or "")) or row.get("path")
                    try:
                        await _delete_library_path(target)
                    except OSError as exc:
                        raise HTTPException(400, f"could not delete {target}: {exc}") from exc
                    removed = await asyncio.to_thread(hub.dataset_registry.delete, id, current=row)
                    for source_id in {id, str(removed.get("id") or id)}:
                        await asyncio.to_thread(hub.store.delete_library_override, "dataset", source_id)
                        await asyncio.to_thread(hub.store.delete_episode_overrides, "dataset", source_id)
                        await asyncio.to_thread(hub.store.delete_episode_view, "dataset", source_id)
            else:
                raise ValueError(f"unknown library kind '{kind}'")
            return {"ok": True, "kind": kind, "id": id}
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except DatasetHubError as exc:
            raise HTTPException(409, str(exc)) from exc

    def _episode_source(kind: str, source_id: str) -> tuple[dict[str, Any], int, bool]:
        """Resolve a library entry to (row, episode count, playable)."""
        if kind == "video":
            meta = _merge_library_override("video", source_id, hub.videos.get(source_id))
            return meta, len(meta.get("episodes") or []), True
        if kind != "dataset":
            raise ValueError(f"unknown episode source kind '{kind}'")
        row = _merge_library_override("dataset", source_id, hub.resolve_dataset(source_id))
        return row, lerobot_episode_count(Path(row["path"])), bool(row.get("playable"))

    @router.get("/api/episodes")
    async def list_episodes(kind: str, id: str) -> dict[str, Any]:
        try:
            row, count, playable = await asyncio.to_thread(_episode_source, kind, id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if kind == "video":
            episodes = [
                _episode_item("video", id, int(ep.get("index", i)), playable=True, row=ep)
                for i, ep in enumerate(row.get("episodes") or [])
            ]
            title = row.get("display_name") or row.get("name") or row.get("repo_id") or id
            subtitle = row.get("task") or row.get("repo_id") or "Local recording"
        else:
            has_video = bool(row.get("has_video"))
            episodes = [
                _episode_item(
                    "dataset",
                    id,
                    index,
                    playable=playable,
                    row={"videos": has_video, "task": row.get("task") or ""},
                )
                for index in range(count)
            ]
            title = row.get("display_name") or row.get("title") or row.get("repo_id") or row.get("name") or id
            fallback_bits = [str(row.get("source") or "dataset")]
            if row.get("fps"):
                fallback_bits.append(f"{row['fps']} fps")
            fallback_bits.append(f"{count} episodes")
            subtitle = row.get("subtitle") or row.get("task") or " · ".join(fallback_bits)
        view = hub.store.episode_view(kind, id)
        hidden = {int(index) for index in view.get("hidden") or []}
        episodes = [item for item in episodes if int(item["index"]) not in hidden]
        by_index = {int(item["index"]): item for item in episodes}
        requested = [int(index) for index in view.get("order") or []]
        ordered = [by_index.pop(index) for index in requested if index in by_index]
        ordered.extend(by_index[index] for index in sorted(by_index))
        episodes = ordered
        source = {
            "title": str(title),
            "subtitle": str(subtitle) if isinstance(subtitle, str) else "",
        }
        return {
            "kind": kind,
            "id": id,
            "title": title,
            "source": source,
            "playable": playable,
            "has_video": any(bool(item.get("has_video")) for item in episodes),
            "episodes": episodes,
        }

    @router.put("/api/episodes")
    async def edit_episode(body: EpisodeEditBody) -> dict[str, Any]:
        if not hub.loop.recording_mutation_lock.acquire(blocking=False):
            raise HTTPException(409, "cannot edit datasets while recording is active")
        try:
            try:
                row, count, playable = await asyncio.to_thread(_episode_source, body.kind, body.id)
            except FileNotFoundError as exc:
                raise HTTPException(404, str(exc)) from None
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            if body.episode < 0 or body.episode >= count:
                raise HTTPException(404, f"episode {body.episode} not found")
            payload = {
                key: value
                for key, value in (("name", body.name), ("task", body.task), ("note", body.note))
                if value is not None
            }
            try:
                await asyncio.to_thread(
                    hub.store.save_episode_override,
                    body.kind,
                    body.id,
                    body.episode,
                    payload,
                )
            except (KeyError, ValueError) as exc:
                raise HTTPException(400, str(exc)) from exc
            return _episode_item(
                body.kind,
                body.id,
                body.episode,
                playable=playable,
                row=row if body.kind == "video" else None,
            )
        finally:
            hub.loop.recording_mutation_lock.release()

    @router.delete("/api/episodes")
    async def delete_episode(kind: str, id: str, episode: int) -> dict[str, Any]:
        if kind == "video":
            return await delete_video_episode(id, episode)
        if kind != "dataset":
            raise HTTPException(400, f"unknown episode source kind '{kind}'")
        view = hub.store.episode_view(kind, id)
        hidden = {int(index) for index in view.get("hidden") or []}
        hidden.add(int(episode))
        await asyncio.to_thread(hub.store.save_episode_view, kind, id, {"hidden": sorted(hidden)})
        return {"ok": True, "hidden": sorted(hidden)}

    @router.post("/api/episodes/reorder")
    async def reorder_episodes(body: EpisodeReorderBody) -> dict[str, Any]:
        if body.kind == "video":
            return await reorder_video_episodes(body.id, ReorderBody(order=body.order))
        if body.kind != "dataset":
            raise HTTPException(400, f"unknown episode source kind '{body.kind}'")
        await asyncio.to_thread(hub.store.save_episode_view, body.kind, body.id, {"order": body.order})
        return {"ok": True, "order": body.order}

    @router.get("/api/preview")
    async def preview(kind: str, id: str, episode: int = 0) -> dict[str, Any]:
        def load_preview() -> dict[str, Any]:
            if kind == "video":
                meta = _merge_library_override("video", id, hub.videos.get(id))
                loaded = local_episode_payload(Path(meta["path"]), episode)
                loaded["episodes"] = len(meta.get("episodes") or [])
                loaded["title"] = meta.get("display_name") or meta.get("name") or id
                loaded["id"] = id
                loaded["kind"] = "video"
                loaded["action_fps"] = meta.get("action_fps") or meta.get("fps")
                loaded["video_fps"] = meta.get("video_fps") or meta.get("fps")
            else:
                row = _merge_library_override("dataset", id, hub.resolve_dataset(id))
                root = Path(row["path"])
                loaded = lerobot_episode_payload(root, episode)
                loaded["title"] = row.get("display_name") or row.get("repo_id") or id
                loaded["id"] = id
                loaded["path"] = str(root)
                loaded["kind"] = "dataset"
                loaded["action_fps"] = row.get("action_fps") or row.get("fps")
                loaded["video_fps"] = row.get("video_fps") or row.get("fps")
            saved = hub.store.episode_overrides(loaded["kind"], id).get(str(int(episode))) or {}
            loaded["episode_name"] = str(saved.get("name") or "")
            loaded["episode_note"] = str(saved.get("note") or "")
            if saved.get("task"):
                loaded["task"] = str(saved["task"])
            loaded["task"] = loaded.get("task") or ""
            return loaded

        try:
            return _json_safe(await asyncio.to_thread(load_preview))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/api/preview/file")
    async def preview_file(kind: str, id: str, episode: int, cam: str) -> FileResponse:
        def resolve_preview_file() -> Path:
            if kind == "video":
                return hub.videos.episode_video(id, episode, cam)
            row = hub.resolve_dataset(id)
            return find_lerobot_video(Path(row["path"]), episode, cam)

        try:
            path = await asyncio.to_thread(resolve_preview_file)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return FileResponse(path, media_type="video/mp4" if path.suffix == ".mp4" else "video/x-msvideo")

    @router.get("/api/models")
    async def list_models() -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(hub.models)
        return [
            _merge_library_override("model", str(row.get("id") or row.get("name") or ""), row)
            for row in rows
        ]

    @router.get("/api/models/search")
    async def search_models(q: str, limit: int = 20) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(search_hf_models, q, limit=max(1, min(int(limit), 50)))
        except ModelHubError as exc:
            raise HTTPException(502, str(exc)) from exc

    @router.post("/api/models")
    async def register_model(body: ModelRegisterBody) -> dict[str, Any]:
        try:
            row = await asyncio.to_thread(
                hub.model_registry.register,
                remote=body.remote,
                name=body.name,
                revision=body.revision,
                note=body.note,
                download=body.download,
            )
            return await asyncio.to_thread(
                _merge_library_override,
                "model",
                str(row.get("id") or body.remote),
                row,
            )
        except ModelHubError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.put("/api/models/{model_id}")
    async def save_model(model_id: str, body: ModelSaveBody) -> dict[str, Any]:
        payload = body.model_dump(exclude_unset=True)
        try:
            row = await asyncio.to_thread(
                hub.model_registry.save,
                model_id,
                payload,
            )
            override: dict[str, Any] = {}
            if "name" in payload and payload["name"] is not None:
                override["name"] = str(payload["name"])
            if "note" in payload and payload["note"] is not None:
                note = str(payload["note"]).strip()
                override["notes"] = [note] if note else []
            if override:
                await asyncio.to_thread(hub.store.save_library_override, "model", model_id, override)
            return await asyncio.to_thread(_merge_library_override, "model", model_id, row)
        except KeyError as exc:
            raise HTTPException(404, f"unknown model '{model_id}'") from exc
        except ModelHubError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/api/models/{model_id}/update")
    async def update_model(model_id: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(hub.model_registry.update, model_id)
        except KeyError as exc:
            raise HTTPException(404, f"unknown model '{model_id}'") from exc
        except ModelHubError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/api/models/{model_id}/download")
    async def download_model(model_id: str) -> dict[str, Any]:
        return await update_model(model_id)

    @router.post("/api/models/{model_id}/upload")
    async def upload_model(model_id: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(hub.model_registry.upload, model_id)
        except KeyError as exc:
            raise HTTPException(404, f"unknown model '{model_id}'") from exc
        except ModelHubError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/api/models/{model_id}")
    async def delete_model(model_id: str) -> dict[str, Any]:
        try:
            await asyncio.to_thread(hub.model_registry.delete, model_id)
            await asyncio.to_thread(hub.store.delete_library_override, "model", model_id)
        except KeyError as exc:
            raise HTTPException(404, f"unknown model '{model_id}'") from exc
        return {"ok": True}

    @router.get("/api/robot-models")
    async def list_robot_models() -> list[dict[str, Any]]:
        return await asyncio.to_thread(hub.robot_model_registry.list)

    @router.get("/api/robot-models/search")
    async def search_robot_models(q: str, limit: int = 20) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(
                search_hf_robot_models,
                q,
                limit=max(1, min(int(limit), 50)),
            )
        except RobotModelError as exc:
            raise HTTPException(502, str(exc)) from exc

    @router.post("/api/robot-models")
    async def install_robot_model(body: RobotModelInstallBody) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                hub.robot_model_registry.register,
                remote=body.remote,
                name=body.name,
                revision=body.revision,
                download=body.download,
            )
        except RobotModelError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/api/robot-models/active")
    async def activate_robot_model(body: RobotModelActiveBody) -> dict[str, Any]:
        try:
            row = await asyncio.to_thread(hub.robot_model_registry.activate, body.id)
        except KeyError as exc:
            raise HTTPException(404, f"unknown robot model '{body.id}'") from exc
        except RobotModelError as exc:
            raise HTTPException(400, str(exc)) from exc
        robot_types = [str(value) for value in row.get("robot_types") or []]
        await _submit(
            "set_virtual_model",
            {
                "model_id": row["id"],
                "robot_type": robot_types[0] if robot_types else "",
            },
        )
        return row

    @router.post("/api/robot-models/{model_id}/update")
    async def update_robot_model(model_id: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(hub.robot_model_registry.update, model_id)
        except KeyError as exc:
            raise HTTPException(404, f"unknown robot model '{model_id}'") from exc
        except RobotModelError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/api/robot-models/{model_id}")
    async def delete_robot_model(model_id: str) -> dict[str, Any]:
        try:
            await asyncio.to_thread(hub.robot_model_registry.delete, model_id)
        except KeyError as exc:
            raise HTTPException(404, f"unknown robot model '{model_id}'") from exc
        except RobotModelError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @router.get("/api/robot-models/{model_id}/manifest")
    async def robot_model_manifest(model_id: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(hub.robot_model_registry.get, model_id)
        except KeyError as exc:
            raise HTTPException(404, f"unknown robot model '{model_id}'") from exc

    @router.get("/api/robot-models/{model_id}/files/{relative_path:path}")
    async def robot_model_file(model_id: str, relative_path: str) -> FileResponse:
        try:
            path = await asyncio.to_thread(
                hub.robot_model_registry.resolve_file,
                model_id,
                relative_path,
            )
        except KeyError as exc:
            raise HTTPException(404, f"unknown robot model '{model_id}'") from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, "robot-model file not found") from exc
        except RobotModelError as exc:
            raise HTTPException(400, str(exc)) from exc
        return FileResponse(path)

    @router.post("/api/debug/infer")
    async def debug_infer(body: DebugInferBody) -> dict[str, Any]:
        if not body.policy_path.strip():
            raise HTTPException(400, "policy_path is required")
        if body.chunk_size <= 0 or body.chunk_size > 256:
            raise HTTPException(400, "chunk_size must be between 1 and 256")
        if not math.isfinite(body.fps) or body.fps <= 0:
            raise HTTPException(400, "fps must be positive")

        try:
            decoded = await asyncio.to_thread(
                _decode_camera_payloads,
                [camera.model_dump() for camera in body.cameras],
            )
        except SnapshotTooLargeError as exc:
            raise HTTPException(413, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        images_rgb: dict[str, np.ndarray] = {}
        for key, data in decoded:
            suffix = str(body.camera_map.get(key, key)).strip()
            if suffix.startswith("observation.images."):
                suffix = suffix[len("observation.images.") :]
            suffix = suffix or key
            if suffix in images_rgb:
                raise HTTPException(400, f"duplicate camera observation suffix '{suffix}'")
            image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise HTTPException(400, f"camera '{key}' is not a decodable image")
            images_rgb[suffix] = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

        lease = await asyncio.to_thread(hub.loop.acquire_debug_lease)
        if not lease.get("ok"):
            raise HTTPException(409, str(lease.get("error") or "model debug is unavailable"))
        token = str(lease.get("token") or "")
        started = time.perf_counter()
        try:
            chunk = await asyncio.to_thread(
                hub.loop.infer_action_chunk,
                path=body.policy_path,
                task=body.task,
                device=str(body.device or hub.config.rollout.device),
                extra={str(key): str(value) for key, value in body.extra.items()},
                joints=body.joints,
                images_rgb=images_rgb,
                chunk_size=body.chunk_size,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"policy inference failed: {exc}") from exc
        finally:
            if token:
                await asyncio.shield(asyncio.to_thread(hub.loop.release_debug_lease, token))
        latency_ms = (time.perf_counter() - started) * 1000.0
        actions = [
            {
                "t_s": round((index + 1) / body.fps, 6),
                "joints": joints,
            }
            for index, joints in enumerate(chunk.actions)
        ]
        evaluation = (
            evaluate_action_chunk([action["joints"] for action in actions], body.reference)
            if body.reference
            else None
        )
        return {
            "ok": True,
            "source": body.source,
            "strategy": chunk.strategy,
            "degraded": chunk.degraded,
            "fps": body.fps,
            "latency_ms": round(latency_ms, 3),
            "actions": actions,
            "evaluation": evaluation,
            "warnings": chunk.warnings,
        }

    @router.get("/api/snapshots")
    async def list_snapshots() -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(hub.snapshots.list)
        result: list[dict[str, Any]] = []
        for row in rows:
            if not row.get("_notes_initialized"):
                row = await asyncio.to_thread(hub.snapshots.ensure_default_note, str(row["id"]))
            result.append(_merge_snapshot_metadata(row))
        return result

    @router.post("/api/snapshots")
    async def create_snapshot(body: SnapshotCreateBody) -> dict[str, Any]:
        try:
            row = await asyncio.to_thread(
                hub.snapshots.create,
                name=body.name,
                task=body.task,
                note=body.note,
                notes=body.notes,
                description=body.description,
                metadata=body.metadata,
                origin=body.origin,
                source=body.source,
                joints=body.joints,
                cameras=[camera.model_dump() for camera in body.cameras],
            )
            return _merge_snapshot_metadata(row)
        except SnapshotTooLargeError as exc:
            raise HTTPException(413, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/api/snapshots/{snapshot_id}")
    async def get_snapshot(snapshot_id: str) -> dict[str, Any]:
        try:
            row = await asyncio.to_thread(hub.snapshots.get, snapshot_id)
            if not row.get("_notes_initialized"):
                row = await asyncio.to_thread(hub.snapshots.ensure_default_note, snapshot_id)
            return _merge_snapshot_metadata(row)
        except FileNotFoundError as exc:
            raise HTTPException(404, f"unknown snapshot '{snapshot_id}'") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.put("/api/snapshots/{snapshot_id}")
    async def update_snapshot(snapshot_id: str, body: SnapshotUpdateBody) -> dict[str, Any]:
        try:
            row = await asyncio.to_thread(
                hub.snapshots.update,
                snapshot_id,
                body.model_dump(exclude_none=True),
            )
            return _merge_snapshot_metadata(row)
        except FileNotFoundError as exc:
            raise HTTPException(404, f"unknown snapshot '{snapshot_id}'") from exc
        except SnapshotTooLargeError as exc:
            raise HTTPException(413, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/api/snapshots/{snapshot_id}")
    async def delete_snapshot(snapshot_id: str) -> dict[str, Any]:
        try:
            await asyncio.to_thread(hub.snapshots.delete, snapshot_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, f"unknown snapshot '{snapshot_id}'") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @router.post("/api/snapshots/{snapshot_id}/duplicate")
    async def duplicate_snapshot(snapshot_id: str) -> dict[str, Any]:
        try:
            row = await asyncio.to_thread(hub.snapshots.duplicate, snapshot_id)
            return _merge_snapshot_metadata(row)
        except FileNotFoundError as exc:
            raise HTTPException(404, f"unknown snapshot '{snapshot_id}'") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/api/snapshots/{snapshot_id}/camera/{key}")
    async def snapshot_camera_file(snapshot_id: str, key: str) -> FileResponse:
        try:
            path = await asyncio.to_thread(hub.snapshots.camera_path, snapshot_id, key)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return FileResponse(path, media_type="image/jpeg")

    @router.get("/api/snapshots/{snapshot_id}/preview")
    async def snapshot_preview(snapshot_id: str) -> FileResponse:
        try:
            path = await asyncio.to_thread(hub.snapshots.preview_path, snapshot_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return FileResponse(path, media_type="image/jpeg")

    @router.get("/api/cameras")
    async def list_cameras() -> list[dict[str, Any]]:
        return hub.cameras.snapshots()

    def require_record_camera_stable() -> None:
        if hub.loop.mode == "record" or hub.loop.pending == "record_start" or hub.loop.recording_mutation_lock.locked():
            raise HTTPException(409, "camera mapping and resolution are frozen while Record is active")

    @router.post("/api/cameras/rescan")
    async def rescan_cameras() -> list[dict[str, Any]]:
        require_record_camera_stable()
        hub.cameras.sync_remote_cameras()
        return hub.cameras.rescan()

    @router.get("/api/cameras/{name}/resolutions")
    async def camera_resolutions(name: str) -> list[dict[str, int]]:
        try:
            camera = hub.cameras.get(name)
        except KeyError:
            raise HTTPException(404, f"unknown camera '{name}'") from None
        if isinstance(camera, RemoteMjpegCamera):
            raise HTTPException(400, "Blender 相机的分辨率由 Blender 面板控制")
        modes = await asyncio.to_thread(supported_resolutions, camera.index)
        return [{"width": width, "height": height} for width, height in modes]

    @router.post("/api/cameras/{name}/focus")
    async def set_camera_focus(name: str, body: CameraFocusBody) -> dict[str, Any]:
        try:
            return hub.cameras.set_focus(name, autofocus=body.autofocus, focus=body.focus)
        except KeyError:
            raise HTTPException(404, f"unknown camera '{name}'") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/api/cameras/{name}/resolution")
    async def set_camera_resolution(name: str, body: CameraResolutionBody) -> dict[str, Any]:
        require_record_camera_stable()
        try:
            return hub.cameras.set_resolution(name, body.width, body.height)
        except KeyError:
            raise HTTPException(404, f"unknown camera '{name}'") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/api/cameras/{name}/label")
    async def set_camera_label(name: str, body: CameraLabelBody) -> dict[str, Any]:
        require_record_camera_stable()
        try:
            return hub.cameras.set_label(name, body.label)
        except KeyError:
            raise HTTPException(404, f"unknown camera '{name}'") from None

    @router.post("/api/cameras/{name}/flags")
    async def set_camera_flags(name: str, body: CameraFlagsBody) -> dict[str, Any]:
        require_record_camera_stable()
        try:
            return hub.cameras.set_flags(
                name,
                enabled=body.enabled,
                show_main=body.show_main,
                feed_robot=body.feed_robot,
            )
        except KeyError:
            raise HTTPException(404, f"unknown camera '{name}'") from None

    @router.get("/api/presets")
    async def list_presets() -> dict[str, Any]:
        return hub.store.presets()

    @router.put("/api/presets/{kind}/{name}")
    async def save_preset(kind: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            saved = await asyncio.to_thread(hub.store.put_preset, kind, name, body)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return saved

    @router.delete("/api/presets/{kind}/{name}")
    async def delete_preset(kind: str, name: str) -> dict[str, Any]:
        try:
            await asyncio.to_thread(hub.store.delete_preset, kind, name)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @router.post("/api/presets/{kind}/{name}/rename")
    async def rename_preset(kind: str, name: str, body: PresetRenameBody) -> dict[str, Any]:
        if kind not in PRESET_KINDS:
            raise HTTPException(400, f"unknown preset kind '{kind}'")
        try:
            saved = await asyncio.to_thread(hub.store.rename_preset, kind, name, body.name)
        except KeyError:
            raise HTTPException(404, f"preset '{name}' not found") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return saved

    @router.post("/api/hardware/apply")
    async def apply_hardware(body: HardwareApplyBody) -> dict[str, Any]:
        require_record_camera_stable()
        name = body.name.strip()
        presets = hub.store.presets("hardware")
        preset = presets.get(name)
        if not isinstance(preset, dict):
            raise HTTPException(404, f"hardware preset '{name}' not found")
        if body.force and await asyncio.to_thread(hub.loop.force_current_hardware_apply):
            return {"ok": True, "accepted": True, "forced": True}
        result = await asyncio.to_thread(
            hub.loop.submit,
            "hardware_apply",
            {"name": name, "preset": preset, "force": body.force},
            45.0,
        )
        if not result.get("ok", False):
            raise HTTPException(400, result.get("error") or "hardware preset failed")
        ui = hub.store.ui()
        ui["active_hardware_preset"] = name
        await asyncio.to_thread(hub.store.save_ui, ui)
        return result

    @router.post("/api/hardware/force_disconnect")
    async def force_disconnect(
        body: ForceDisconnectBody = ForceDisconnectBody(),
    ) -> dict[str, Any]:
        result = await asyncio.to_thread(hub.loop.request_force_disconnect, body.role)
        if not result.get("ok", False):
            raise HTTPException(400, result.get("error") or "force disconnect failed")
        return result

    @router.get("/api/ui")
    async def get_ui() -> dict[str, Any]:
        return hub.store.ui()

    @router.put("/api/ui")
    async def put_ui(body: UiStateBody) -> dict[str, Any]:
        current = hub.store.ui()
        data = body.model_dump(exclude_none=True)
        current.update(data)
        return await asyncio.to_thread(hub.store.save_ui, current)

    @router.post("/api/cameras/{name}/stream")
    async def set_camera_stream(name: str, body: CameraStreamBody) -> dict[str, Any]:
        require_record_camera_stable()
        try:
            return hub.cameras.set_stream(name, body.enable, body.port)
        except KeyError:
            raise HTTPException(404, f"unknown camera '{name}'") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/camera/{name}")
    async def camera_mjpeg(name: str) -> StreamingResponse:
        try:
            camera = hub.cameras.get(name)
        except KeyError:
            raise HTTPException(404, f"unknown camera '{name}'") from None

        async def generate():
            try:
                while True:
                    jpeg = camera.latest_jpeg()
                    if not jpeg:
                        await asyncio.sleep(1.0 / 25.0)
                        continue
                    yield (
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(jpeg)).encode("ascii")
                        + b"\r\n\r\n"
                        + jpeg
                        + b"\r\n"
                    )
                    await asyncio.sleep(1.0 / 25.0)
            finally:
                # No per-client camera resource exists; this finally block makes
                # generator finalization explicit while preserving cancellation.
                pass

        return StreamingResponse(
            generate(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @router.websocket("/ws")
    async def ws_status(ws: WebSocket) -> None:
        try:
            await ws.accept()
            while True:
                await ws.send_json(hub.snapshot())
                await asyncio.sleep(0.1)
        except WebSocketDisconnect:
            return

    app.include_router(router, prefix=prefix)
    if STATIC_DIR.is_dir():
        app.mount(f"{prefix}/static", StaticFiles(directory=STATIC_DIR), name="static")

    if prefix:

        @app.get("/")
        async def root_redirect() -> RedirectResponse:
            return RedirectResponse(prefix + "/")

    return app
