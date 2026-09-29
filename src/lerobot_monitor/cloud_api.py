"""Cloud host management API: the standalone port-8095 manager, hosted by Monitor.

The web panel and this router replace the separate management process. Host
definitions, SSH tunnels and background jobs still live in ``CloudManager``;
this module only exposes them through the Monitor FastAPI application so the
browser keeps talking to a single origin.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .cloud_manager.app import safe_proxy_path
from .cloud_manager.manager import HostInput
from .cloud_manager.transport import TransportError
from .monitor_cloud import CloudRequestError

# A cloud call may carry a 4xx from the remote service; anything else is a
# gateway failure. Mirror the standalone manager's bounded proxy timeout.
MAX_PROXY_BYTES = 64 * 1024 * 1024


class UploadInput(BaseModel):
    path: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)


class RuntimeInput(BaseModel):
    wheel_path: str
    profile: Literal["act", "smolvla", "pi"] = "smolvla"
    huggingface_home: str | None = None


def _cloud_failure(exc: BaseException) -> HTTPException:
    if isinstance(exc, CloudRequestError):
        status = int(getattr(exc, "status", 502))
        if not 400 <= status < 600:
            status = 502
        return HTTPException(status, str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(404, "Unknown cloud host")
    return HTTPException(502, str(exc))


def add_cloud_routes(router: APIRouter, hub: Any) -> None:
    """Register cloud management routes on the Monitor router."""

    def manager() -> Any:
        service = getattr(hub.cloud, "manager", None)
        if service is None:
            raise HTTPException(503, "cloud manager is unavailable")
        return service

    @router.get("/api/cloud/hosts")
    async def cloud_hosts() -> list[dict[str, Any]]:
        return await asyncio.to_thread(hub.cloud.hosts)

    @router.post("/api/cloud/hosts", status_code=201)
    async def add_cloud_host(body: HostInput) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(manager().add_host, body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/api/cloud/jobs")
    async def cloud_manager_jobs() -> list[dict[str, Any]]:
        return await asyncio.to_thread(manager().jobs)

    @router.post("/api/cloud/hosts/{host_id}/probe")
    async def probe_cloud_host(host_id: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(manager().probe, host_id)
        except (TransportError, KeyError, ValueError, RuntimeError) as exc:
            raise _cloud_failure(exc) from exc

    @router.post("/api/cloud/hosts/{host_id}/connect")
    async def connect_cloud_host(host_id: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(hub.cloud.connect, host_id)
        except (CloudRequestError, TransportError, KeyError, ValueError, RuntimeError) as exc:
            raise _cloud_failure(exc) from exc

    @router.post("/api/cloud/hosts/{host_id}/disconnect")
    async def disconnect_cloud_host(host_id: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(manager().disconnect, host_id)
        except (TransportError, KeyError, ValueError, RuntimeError) as exc:
            raise _cloud_failure(exc) from exc

    @router.post("/api/cloud/hosts/{host_id}/bootstrap", status_code=202)
    async def bootstrap_cloud_host(host_id: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                manager().submit, host_id, "bootstrap",
                lambda: manager().bootstrap(host_id),
            )
        except (KeyError, ValueError, RuntimeError) as exc:
            raise _cloud_failure(exc) from exc

    @router.post("/api/cloud/hosts/{host_id}/upgrade", status_code=202)
    async def upgrade_cloud_host(host_id: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                manager().submit, host_id, "upgrade",
                lambda: manager().bootstrap(host_id, upgrade=True),
            )
        except (KeyError, ValueError, RuntimeError) as exc:
            raise _cloud_failure(exc) from exc

    @router.post("/api/cloud/hosts/{host_id}/upload", status_code=202)
    async def upload_cloud_checkpoint(host_id: str, body: UploadInput) -> dict[str, Any]:
        source = Path(body.path).expanduser()
        if not source.is_absolute() or not source.is_dir():
            raise HTTPException(400, "Upload requires an existing absolute checkpoint directory")
        try:
            return await asyncio.to_thread(
                manager().submit, host_id, "upload",
                lambda: manager().upload(host_id, source, body.name),
            )
        except (KeyError, ValueError, RuntimeError) as exc:
            raise _cloud_failure(exc) from exc

    @router.post("/api/cloud/hosts/{host_id}/runtime", status_code=202)
    async def prepare_cloud_runtime(host_id: str, body: RuntimeInput) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                manager().submit, host_id, "runtime",
                lambda: manager().prepare_runtime(
                    host_id, Path(body.wheel_path), body.profile, body.huggingface_home,
                ),
            )
        except (KeyError, ValueError, RuntimeError) as exc:
            raise _cloud_failure(exc) from exc

    @router.get("/api/cloud/hosts/{host_id}/catalog")
    async def cloud_catalog(host_id: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(hub.cloud.catalog, host_id)
        except (CloudRequestError, TransportError, KeyError, ValueError, RuntimeError) as exc:
            raise _cloud_failure(exc) from exc

    @router.api_route(
        "/api/cloud/hosts/{host_id}/cloud/api/v1/{path:path}",
        methods=["GET", "POST", "DELETE"],
    )
    async def cloud_proxy(host_id: str, path: str, request: Request) -> Any:
        if not safe_proxy_path(path):
            raise HTTPException(404, "Unsupported cloud API path")
        payload: dict[str, Any] | None = None
        if request.method in {"POST", "PUT", "PATCH"}:
            chunks: list[bytes] = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_PROXY_BYTES:
                    raise HTTPException(413, "Cloud request exceeds 64 MiB")
                chunks.append(chunk)
            body = b"".join(chunks)
            if body:
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise HTTPException(400, "Cloud request body must be JSON") from exc
                if not isinstance(payload, dict):
                    raise HTTPException(400, "Cloud request body must be a JSON object")
        params = dict(request.query_params) or None
        try:
            return await asyncio.to_thread(
                hub.cloud.request, host_id, request.method, "/api/v1/" + path,
                json=payload, params=params, timeout=180,
            )
        except CloudRequestError as exc:
            raise _cloud_failure(exc) from exc
        except (TransportError, KeyError, ValueError, RuntimeError) as exc:
            raise _cloud_failure(exc) from exc
