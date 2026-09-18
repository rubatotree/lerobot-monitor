"""FastAPI application: REST, WebSocket telemetry, MJPEG cameras."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import MonitorConfig
from .hub import RuntimeHub
from .types import JOINT_ORDER

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


class JogBody(BaseModel):
    joints: dict[str, float]
    duration_s: float | None = None


class PresetBody(BaseModel):
    name: str = "home"
    duration_s: float | None = None


class HoldBody(BaseModel):
    enabled: bool = True


class RecordStartBody(BaseModel):
    task: str = ""
    repo_id: str = ""
    episode_time_s: float | None = None
    fps: int | None = None


class RolloutStartBody(BaseModel):
    policy_path: str
    task: str = ""
    duration_s: float | None = None
    device: str | None = None
    record: bool = True
    fps: int | None = None


def create_app(config: MonitorConfig) -> FastAPI:
    hub = RuntimeHub(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.start()
        yield
        hub.stop()

    app = FastAPI(title="LeRobot Monitor", lifespan=lifespan)
    app.state.hub = hub

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        page = STATIC_DIR / "index.html"
        if not page.is_file():
            raise HTTPException(404, "frontend not packaged")
        return FileResponse(page)

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return hub.snapshot()

    @app.get("/api/meta")
    async def meta() -> dict[str, Any]:
        return hub.static_meta()

    @app.get("/api/sessions")
    async def sessions() -> list[dict[str, Any]]:
        return hub.sessions()

    def _submit(kind: str, payload: dict[str, Any] | None = None, timeout: float = 8.0) -> dict[str, Any]:
        result = hub.loop.submit(kind, payload, timeout=timeout)
        if not result.get("ok", False):
            raise HTTPException(400, result.get("error") or "command failed")
        return result

    @app.post("/api/robot/connect")
    async def robot_connect() -> dict[str, Any]:
        return _submit("connect_robot", timeout=15.0)

    @app.post("/api/robot/disconnect")
    async def robot_disconnect() -> dict[str, Any]:
        return _submit("disconnect_robot")

    @app.post("/api/leader/connect")
    async def leader_connect() -> dict[str, Any]:
        return _submit("connect_leader", timeout=15.0)

    @app.post("/api/leader/disconnect")
    async def leader_disconnect() -> dict[str, Any]:
        return _submit("disconnect_leader")

    @app.post("/api/joints")
    async def joints(body: JogBody) -> dict[str, Any]:
        unknown = [name for name in body.joints if name not in JOINT_ORDER]
        if unknown:
            raise HTTPException(400, f"unknown joints: {unknown}")
        return _submit("jog", {"joints": body.joints, "duration_s": body.duration_s})

    @app.post("/api/joints/preset")
    async def preset(body: PresetBody) -> dict[str, Any]:
        return _submit("preset", {"name": body.name, "duration_s": body.duration_s})

    @app.post("/api/hold")
    async def hold(body: HoldBody) -> dict[str, Any]:
        return _submit("hold", {"enabled": body.enabled})

    @app.post("/api/estop")
    async def estop() -> dict[str, Any]:
        return _submit("estop")

    @app.post("/api/resume")
    async def resume() -> dict[str, Any]:
        return _submit("resume")

    @app.post("/api/teleop/start")
    async def teleop_start() -> dict[str, Any]:
        return _submit("teleop_start", timeout=15.0)

    @app.post("/api/teleop/stop")
    async def teleop_stop() -> dict[str, Any]:
        return _submit("teleop_stop")

    @app.post("/api/record/start")
    async def record_start(body: RecordStartBody) -> dict[str, Any]:
        return _submit("record_start", body.model_dump(), timeout=15.0)

    @app.post("/api/record/stop")
    async def record_stop() -> dict[str, Any]:
        return _submit("record_stop")

    @app.post("/api/record/next")
    async def record_next() -> dict[str, Any]:
        return _submit("record_next")

    @app.post("/api/rollout/start")
    async def rollout_start(body: RolloutStartBody) -> dict[str, Any]:
        return _submit("rollout_start", body.model_dump(), timeout=120.0)

    @app.post("/api/rollout/stop")
    async def rollout_stop() -> dict[str, Any]:
        return _submit("rollout_stop")

    @app.get("/camera/{name}")
    async def camera_mjpeg(name: str) -> StreamingResponse:
        if name not in hub.cameras.streams:
            raise HTTPException(404, f"unknown camera '{name}'")

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

    @app.websocket("/ws")
    async def ws_status(ws: WebSocket) -> None:
        await ws.accept()
        try:
            while True:
                await ws.send_json(hub.snapshot())
                await asyncio.sleep(0.1)
        except WebSocketDisconnect:
            return

    return app
