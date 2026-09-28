"""Loopback-only management UI and token-preserving proxy."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .manager import CloudManager, HostInput
from .transport import TransportError


class UploadInput(BaseModel):
    path: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)


class RuntimeInput(BaseModel):
    wheel_path: str
    profile: Literal["act", "smolvla", "pi"] = "smolvla"
    huggingface_home: str | None = None


def safe_proxy_path(path: str) -> bool:
    return re.fullmatch(
        r"(?:health|gpus|deployments|jobs|sessions)(?:/[A-Za-z0-9_-]+)?(?:/(?:load|unload|logs|sessions|reset|infer|heartbeat))?",
        path) is not None


def create_app(state_dir: Path | None = None, *, manager: CloudManager | None = None,
               project: Path | None = None) -> FastAPI:
    service = manager or CloudManager(state_dir or Path.home() / ".lerobot-cloud-manager", project=project)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await run_in_threadpool(service.close)

    app = FastAPI(title="LeRobot Cloud Manager", lifespan=lifespan)
    app.state.manager = service

    @app.middleware("http")
    async def loopback_guard(request: Request, call_next: Any) -> Response:
        host = request.headers.get("host", "")
        try:
            parsed = urlsplit("http://" + host)
            allowed = parsed.hostname in {"127.0.0.1", "localhost", "::1"} and not parsed.username
        except ValueError:
            allowed = False
        if not allowed:
            return JSONResponse({"detail": "Manager requires a loopback Host"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{host}"
            if origin is not None and origin.rstrip("/") != expected:
                return JSONResponse({"detail": "Cross-origin management requests are forbidden"}, status_code=403)
            if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
                return JSONResponse({"detail": "Cross-site management requests are forbidden"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(KeyError)
    async def unknown_host(request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": "Unknown host"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid_operation(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(TransportError)
    async def transport_error(request: Request, exc: TransportError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.get("/api/ui-config")
    def ui_config() -> dict[str, str]:
        return {"mode": "manager"}

    @app.get("/api/hosts")
    def hosts() -> list[dict[str, Any]]:
        return service.hosts()

    @app.post("/api/hosts", status_code=201)
    def add_host(body: HostInput) -> dict[str, Any]:
        return service.add_host(body)

    @app.get("/api/jobs")
    def jobs() -> list[dict[str, Any]]:
        return service.jobs()

    @app.post("/api/hosts/{identifier}/probe")
    def probe(identifier: str) -> dict[str, Any]:
        return service.probe(identifier)

    @app.post("/api/hosts/{identifier}/connect")
    def connect(identifier: str) -> dict[str, Any]:
        return service.connect(identifier)

    @app.post("/api/hosts/{identifier}/disconnect")
    def disconnect(identifier: str) -> dict[str, Any]:
        return service.disconnect(identifier)

    @app.post("/api/hosts/{identifier}/bootstrap", status_code=202)
    def bootstrap(identifier: str) -> dict[str, Any]:
        return service.submit(identifier, "bootstrap", lambda: service.bootstrap(identifier))

    @app.post("/api/hosts/{identifier}/upgrade", status_code=202)
    def upgrade(identifier: str) -> dict[str, Any]:
        return service.submit(identifier, "upgrade", lambda: service.bootstrap(identifier, upgrade=True))

    @app.post("/api/hosts/{identifier}/upload", status_code=202)
    def upload(identifier: str, body: UploadInput) -> dict[str, Any]:
        source = Path(body.path).expanduser()
        if not source.is_absolute() or not source.is_dir():
            raise ValueError("Upload requires an existing absolute checkpoint directory")
        return service.submit(identifier, "upload", lambda: service.upload(identifier, source, body.name))

    @app.post("/api/hosts/{identifier}/runtime", status_code=202)
    def runtime(identifier: str, body: RuntimeInput) -> dict[str, Any]:
        return service.submit(identifier, "runtime", lambda: service.prepare_runtime(
            identifier, Path(body.wheel_path), body.profile, body.huggingface_home))

    @app.api_route("/api/hosts/{identifier}/cloud/api/v1/{path:path}", methods=["GET", "POST", "DELETE"])
    async def proxy(identifier: str, path: str, request: Request) -> Response:
        if not safe_proxy_path(path):
            raise HTTPException(404, "Unsupported cloud API path")
        endpoint, token = service.endpoint(identifier)
        parts: list[bytes] = []
        size = 0
        async for part in request.stream():
            size += len(part)
            if size > 64 * 1024 * 1024:
                raise HTTPException(413, "Request exceeds 64 MiB")
            parts.append(part)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=120, trust_env=False, follow_redirects=False) as client:
                upstream = await client.request(request.method, endpoint + "/api/v1/" + path,
                    params=request.query_params, content=b"".join(parts), headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(502, "Cloud request failed; inspect connection and service status") from exc
        # Do not forward redirect/auth headers or accidentally echo credentials from an error.
        content = upstream.content.replace(token.encode(), b"[redacted]")
        return Response(content, status_code=upstream.status_code,
                        media_type=upstream.headers.get("content-type", "application/json"))

    web = Path(__file__).resolve().parent.parent / "cloud" / "web"
    if web.is_dir():
        app.mount("/static", StaticFiles(directory=web), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(web / "index.html")

    return app
