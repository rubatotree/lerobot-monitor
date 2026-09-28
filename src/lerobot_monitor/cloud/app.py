"""Standalone authenticated management and inference HTTP API."""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .runtime import CloudError, CloudRuntime
from .schemas import DeploymentCreate, InferRequest, LoadRequest, SessionEpoch, SessionOpen


class ApiBoundary:
    """Authenticate before parsing and cap actual bytes, including chunked requests."""

    def __init__(self, app: Any, token: str, max_bytes: int = 40 * 1024 * 1024) -> None:
        self.app, self.token, self.max_bytes = app, token, max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/v1"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        if not secrets.compare_digest(headers.get(b"authorization", b""), f"Bearer {self.token}".encode()):
            await JSONResponse({"detail": "Bearer authentication required"}, status_code=401)(scope, receive, send)
            return
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                await JSONResponse({"detail": "request exceeds 40 MiB"}, status_code=413)(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay() -> dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(root: str | Path = "~/.lerobot-monitor-cloud", *, runtime: CloudRuntime | None = None,
               shutdown: Callable[[], None] | None = None, instance_id: str | None = None,
               code_hash: str | None = None, **runtime_options: Any) -> FastAPI:
    service = runtime or CloudRuntime(Path(root), **runtime_options)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        service.close()

    app = FastAPI(title="LeRobot Cloud Models", version="1.0", lifespan=lifespan)
    app.state.runtime = service
    app.add_middleware(ApiBoundary, token=service.token)

    @app.exception_handler(CloudError)
    async def cloud_error(request: Request, exc: CloudError) -> JSONResponse:
        return JSONResponse({"detail": service.clean_error(exc)}, status_code=exc.status)

    @app.middleware("http")
    async def bound_request(request: Request, call_next: Any) -> Any:
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > 40 * 1024 * 1024:
                    return JSONResponse({"detail": "request exceeds 40 MiB"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "invalid Content-Length"}, status_code=400)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def authorize(request: Request) -> None:
        value = request.headers.get("authorization", "")
        if not secrets.compare_digest(value, f"Bearer {service.token}"):
            raise HTTPException(status_code=401, detail="Bearer authentication required", headers={"WWW-Authenticate": "Bearer"})

    api = APIRouter(prefix="/api/v1", dependencies=[Depends(authorize)])

    @api.get("/health")
    def health() -> dict[str, Any]:
        return {**service.health(), "instance_id": instance_id, "code_hash": code_hash}

    @api.get("/gpus")
    def gpus() -> list[dict[str, Any]]:
        return service.gpus()

    @api.get("/deployments")
    def deployments() -> list[dict[str, Any]]:
        return service.list_deployments()

    @api.post("/deployments", status_code=202)
    def deploy(request: DeploymentCreate) -> dict[str, Any]:
        return service.create_deployment(request)

    @api.post("/deployments/{deployment_id}/load", status_code=202)
    def load(deployment_id: str, request: LoadRequest) -> dict[str, Any]:
        return service.load(deployment_id, request)

    @api.post("/deployments/{deployment_id}/unload", status_code=202)
    def unload(deployment_id: str) -> dict[str, Any]:
        return service.unload(deployment_id)

    @api.delete("/deployments/{deployment_id}", status_code=202)
    def delete(deployment_id: str, delete_files: bool = False) -> dict[str, Any]:
        return service.delete(deployment_id, delete_files)

    @api.get("/deployments/{deployment_id}/logs")
    def logs(deployment_id: str) -> dict[str, Any]:
        return service.logs(deployment_id)

    @api.get("/jobs")
    def jobs() -> list[dict[str, Any]]:
        return service.list_jobs()

    @api.post("/deployments/{deployment_id}/sessions", status_code=201)
    def open_session(deployment_id: str, request: SessionOpen) -> dict[str, Any]:
        return service.open_session(deployment_id, request)

    @api.post("/sessions/{session_id}/heartbeat")
    def heartbeat(session_id: str, request: SessionEpoch) -> dict[str, Any]:
        return service.heartbeat(session_id, request.epoch)

    @api.post("/sessions/{session_id}/infer")
    def infer(session_id: str, request: InferRequest) -> dict[str, Any]:
        return service.infer(session_id, request)

    @api.post("/sessions/{session_id}/reset")
    def reset(session_id: str, request: SessionEpoch) -> dict[str, Any]:
        return service.reset(session_id, request.epoch)

    @api.delete("/sessions/{session_id}")
    def close_session(session_id: str) -> dict[str, Any]:
        return service.close_session(session_id)

    @api.post("/shutdown")
    def stop() -> dict[str, str]:
        if shutdown is None:
            raise HTTPException(409, "This service is not managed by the daemon launcher")
        with service.lock:
            if service.sessions or service.busy:
                raise HTTPException(409, "Close active sessions and wait for jobs before stopping")
            shutdown()
        return {"status": "stopping"}

    app.include_router(api)

    @app.get("/api/ui-config")
    def ui_config() -> dict[str, str]:
        return {"mode": "cloud"}

    web = Path(__file__).parent / "web"
    app.mount("/static", StaticFiles(directory=web, check_dir=False), name="static")

    @app.get("/")
    def index() -> FileResponse:
        if not (web / "index.html").is_file():
            raise HTTPException(503, "Management UI is not installed")
        return FileResponse(web / "index.html")

    return app
