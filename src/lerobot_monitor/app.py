"""FastAPI application: REST, WebSocket telemetry, MJPEG cameras."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import MonitorConfig
from .hub import RuntimeHub
from .preview import find_lerobot_video, lerobot_episode_payload, local_episode_payload
from .types import JOINT_ORDER

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


class JogBody(BaseModel):
    joints: dict[str, float]
    duration_s: float | None = None
    live: bool = False


class PresetBody(BaseModel):
    name: str = "home"
    duration_s: float | None = None


class HoldBody(BaseModel):
    enabled: bool = True


class RecordStartBody(BaseModel):
    task: str = ""
    repo_id: str = ""
    episode_time_s: float | None = None
    reset_time_s: float | None = None
    num_episodes: int | None = None
    fps: int | None = None
    resume: bool = False
    dataset_id: str | None = None
    format: str | None = None
    root: str | None = None
    streaming_encoding: bool | None = None
    encoder_threads: int | None = None
    video: bool | None = None
    merge: bool | None = None
    auto_record: bool | None = None


class RolloutStartBody(BaseModel):
    policy_path: str
    task: str = ""
    duration_s: float | None = None
    device: str | None = None
    record: bool | None = None
    auto_record: bool | None = None
    fps: int | None = None
    extra: dict[str, str] | None = None
    dataset_id: str | None = None
    format: str | None = None


class DatasetCreateBody(BaseModel):
    name: str = ""
    task: str = ""
    repo_id: str = ""
    fps: int | None = None


class ReorderBody(BaseModel):
    order: list[int] = Field(default_factory=list)


class AutoRecordBody(BaseModel):
    enabled: bool = True


class CaptureStartBody(BaseModel):
    fps: int | None = None
    format: str | None = None
    dataset_id: str | None = None
    resume: bool = False
    task: str = ""
    name: str = ""
    merge: bool = True
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
    hold: bool | None = None
    hardware: dict[str, Any] | None = None
    auto_record: bool | None = None
    selected_dataset: str | None = None


def _normalize_prefix(base_path: str) -> str:
    prefix = (base_path or "").strip()
    if not prefix or prefix == "/":
        return ""
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    return prefix.rstrip("/")


def create_app(config: MonitorConfig, *, apply_prefix: bool = True) -> FastAPI:
    hub = RuntimeHub(config)
    prefix = _normalize_prefix(config.server.base_path) if apply_prefix else ""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.start()
        yield
        hub.stop()

    app = FastAPI(title="LeRobot Monitor", lifespan=lifespan)
    app.state.hub = hub
    app.state.base_path = prefix or _normalize_prefix(config.server.base_path)

    router = APIRouter()

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
        return hub.sessions()

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
        hub.loop.note_pending(kind, message)
        try:
            return await _submit(kind, payload, timeout=timeout)
        finally:
            hub.loop.clear_pending()

    def _enqueue(kind: str, message: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        hub.loop.note_pending(kind, message)
        hub.loop.submit_nowait(kind, payload or {})
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
        return await _submit(
            "jog",
            {"joints": body.joints, "duration_s": body.duration_s, "live": body.live},
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

    @router.post("/api/scan")
    async def scan_devices() -> dict[str, Any]:
        from .ports import list_serial_ports

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
        return await _submit_logged("teleop_start", "teleop requested", body.model_dump(exclude_none=True), timeout=15.0)

    @router.post("/api/teleop/stop")
    async def teleop_stop() -> dict[str, Any]:
        return await _submit("teleop_stop")

    @router.post("/api/record/start")
    async def record_start(body: RecordStartBody) -> dict[str, Any]:
        return await _submit_logged("record_start", "record requested — opening video session", body.model_dump(), timeout=15.0)

    @router.post("/api/record/stop")
    async def record_stop() -> dict[str, Any]:
        return await _submit("record_stop")

    @router.post("/api/record/next")
    async def record_next() -> dict[str, Any]:
        return await _submit("record_next")

    @router.post("/api/rollout/start")
    async def rollout_start(body: RolloutStartBody) -> dict[str, Any]:
        path = body.policy_path or "(no policy)"
        return _enqueue("rollout_start", f"rollout requested — loading {path}", body.model_dump())

    @router.post("/api/rollout/stop")
    async def rollout_stop() -> dict[str, Any]:
        return await _submit("rollout_stop")

    @router.post("/api/capture/start")
    async def capture_start(body: CaptureStartBody = CaptureStartBody()) -> dict[str, Any]:
        return await _submit_logged("capture_start", "video capture requested", body.model_dump(exclude_none=True), timeout=15.0)

    @router.post("/api/capture/stop")
    async def capture_stop() -> dict[str, Any]:
        return await _submit("capture_stop")

    @router.post("/api/auto_record")
    async def auto_record(body: AutoRecordBody) -> dict[str, Any]:
        return await _submit("auto_record", {"enabled": body.enabled})

    @router.get("/api/videos")
    async def list_videos() -> list[dict[str, Any]]:
        return hub.videos.list()

    @router.post("/api/videos")
    async def create_video(body: DatasetCreateBody) -> dict[str, Any]:
        fps = body.fps if body.fps is not None else hub.config.recording.fps
        return hub.videos.create(body.name, fps=fps, task=body.task, repo_id=body.repo_id)

    @router.get("/api/videos/{video_id}")
    async def get_video(video_id: str) -> dict[str, Any]:
        try:
            return hub.videos.get(video_id)
        except FileNotFoundError:
            raise HTTPException(404, f"unknown video '{video_id}'") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/api/videos/{video_id}")
    async def delete_video(video_id: str) -> dict[str, Any]:
        try:
            hub.videos.delete(video_id)
        except FileNotFoundError:
            raise HTTPException(404, f"unknown video '{video_id}'") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @router.delete("/api/videos/{video_id}/episodes/{index}")
    async def delete_video_episode(video_id: str, index: int) -> dict[str, Any]:
        try:
            return hub.videos.delete_episode(video_id, index)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/api/videos/{video_id}/episodes/reorder")
    async def reorder_video_episodes(video_id: str, body: ReorderBody) -> dict[str, Any]:
        try:
            return hub.videos.reorder(video_id, body.order)
        except FileNotFoundError:
            raise HTTPException(404, f"unknown video '{video_id}'") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/api/videos/{video_id}/episodes/{index}/video/{cam}")
    async def video_episode_file(video_id: str, index: int, cam: str) -> FileResponse:
        try:
            path = hub.videos.episode_video(video_id, index, cam)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return FileResponse(path, media_type="video/mp4" if path.suffix == ".mp4" else "video/x-msvideo")

    @router.get("/api/videos/{video_id}/episodes/{index}/preview")
    async def video_episode_preview(video_id: str, index: int) -> FileResponse:
        try:
            path = hub.videos.episode_preview(video_id, index)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return FileResponse(path, media_type="image/jpeg")

    @router.get("/api/datasets")
    async def list_datasets() -> list[dict[str, Any]]:
        return hub.hf_datasets()

    @router.get("/api/preview")
    async def preview(kind: str, id: str, episode: int = 0) -> dict[str, Any]:
        try:
            if kind == "video":
                meta = hub.videos.get(id)
                payload = local_episode_payload(Path(meta["path"]), episode)
                payload["episodes"] = len(meta.get("episodes") or [])
                payload["title"] = meta.get("name") or id
                payload["id"] = id
                payload["kind"] = "video"
                return payload
            row = hub.resolve_dataset(id)
            root = Path(row["path"])
            payload = lerobot_episode_payload(root, episode)
            payload["title"] = row.get("repo_id") or id
            payload["id"] = id
            payload["path"] = str(root)
            payload["kind"] = "dataset"
            return payload
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/api/preview/file")
    async def preview_file(kind: str, id: str, episode: int, cam: str) -> FileResponse:
        try:
            if kind == "video":
                path = hub.videos.episode_video(id, episode, cam)
            else:
                row = hub.resolve_dataset(id)
                path = find_lerobot_video(Path(row["path"]), episode, cam)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return FileResponse(path, media_type="video/mp4" if path.suffix == ".mp4" else "video/x-msvideo")

    @router.get("/api/models")
    async def list_models() -> list[dict[str, Any]]:
        return hub.models()

    @router.get("/api/cameras")
    async def list_cameras() -> list[dict[str, Any]]:
        return hub.cameras.snapshots()

    @router.post("/api/cameras/rescan")
    async def rescan_cameras() -> list[dict[str, Any]]:
        return hub.cameras.rescan()

    @router.post("/api/cameras/{name}/focus")
    async def set_camera_focus(name: str, body: CameraFocusBody) -> dict[str, Any]:
        try:
            return hub.cameras.set_focus(name, autofocus=body.autofocus, focus=body.focus)
        except KeyError:
            raise HTTPException(404, f"unknown camera '{name}'") from None

    @router.post("/api/cameras/{name}/resolution")
    async def set_camera_resolution(name: str, body: CameraResolutionBody) -> dict[str, Any]:
        try:
            return hub.cameras.set_resolution(name, body.width, body.height)
        except KeyError:
            raise HTTPException(404, f"unknown camera '{name}'") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/api/cameras/{name}/label")
    async def set_camera_label(name: str, body: CameraLabelBody) -> dict[str, Any]:
        try:
            return hub.cameras.set_label(name, body.label)
        except KeyError:
            raise HTTPException(404, f"unknown camera '{name}'") from None

    @router.post("/api/cameras/{name}/flags")
    async def set_camera_flags(name: str, body: CameraFlagsBody) -> dict[str, Any]:
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
            saved = hub.store.put_preset(kind, name, body)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return saved

    @router.delete("/api/presets/{kind}/{name}")
    async def delete_preset(kind: str, name: str) -> dict[str, Any]:
        try:
            hub.store.delete_preset(kind, name)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @router.get("/api/ui")
    async def get_ui() -> dict[str, Any]:
        return hub.store.ui()

    @router.put("/api/ui")
    async def put_ui(body: UiStateBody) -> dict[str, Any]:
        current = hub.store.ui()
        data = body.model_dump(exclude_none=True)
        current.update(data)
        return hub.store.save_ui(current)

    @router.post("/api/cameras/{name}/stream")
    async def set_camera_stream(name: str, body: CameraStreamBody) -> dict[str, Any]:
        try:
            return hub.cameras.set_stream(name, body.enable, body.port)
        except KeyError:
            raise HTTPException(404, f"unknown camera '{name}'") from None
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/camera/{name}")
    async def camera_mjpeg(name: str) -> StreamingResponse:
        try:
            hub.cameras.get(name)
        except KeyError:
            raise HTTPException(404, f"unknown camera '{name}'") from None

        async def generate():
            while True:
                jpeg = hub.cameras.latest_jpeg(name)
                if jpeg:
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                else:
                    yield b"--frame\r\n\r\n"
                await asyncio.sleep(1.0 / 25.0)

        return StreamingResponse(
            generate(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @router.websocket("/ws")
    async def ws_status(ws: WebSocket) -> None:
        await ws.accept()
        try:
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
