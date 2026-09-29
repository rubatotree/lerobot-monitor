"""Monitor-hosted cloud management API: the former port-8095 manager surface."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from lerobot_monitor.app import create_app
from lerobot_monitor.cloud_manager.transport import TransportError
from lerobot_monitor.config import CamerasConfig, MonitorConfig, RobotConfig
from lerobot_monitor.monitor_cloud import CloudRequestError


HOST = {
    "id": "h1",
    "alias": "8x4090-server",
    "root": "/data/me/lerobot-monitor",
    "port": 8091,
    "python": "python3.12",
    "runtime_python": None,
    "status": "connected",
    "operation_status": "idle",
    "error": None,
}


class FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.submitted: list[tuple[str, str]] = []

    def hosts(self) -> list[dict[str, Any]]:
        return [dict(HOST)]

    def add_host(self, value: Any) -> dict[str, Any]:
        self.calls.append(("add_host", value.alias))
        return {"id": "new", "alias": value.alias, "root": value.root, "status": "disconnected", "error": None}

    def jobs(self) -> list[dict[str, Any]]:
        return [{"id": "j1", "kind": "bootstrap", "status": "succeeded", "created_at": 1.0}]

    def probe(self, host_id: str) -> dict[str, Any]:
        self.calls.append(("probe", host_id))
        return {"python": "3.12.4", "uv": True, "root_exists": True, "writable": True, "runtime_configured": False}

    def disconnect(self, host_id: str) -> dict[str, str]:
        self.calls.append(("disconnect", host_id))
        return {"status": "disconnected"}

    def submit(self, host_id: str, kind: str, action: Any) -> dict[str, Any]:
        self.submitted.append((host_id, kind))
        return {"id": f"job-{kind}", "host_id": host_id, "kind": kind, "status": "queued"}

    def bootstrap(self, host_id: str, *, upgrade: bool = False) -> dict[str, Any]:
        return {"status": "started"}

    def upload(self, host_id: str, source: Path, name: str) -> dict[str, Any]:
        return {"id": "deployment"}

    def prepare_runtime(self, host_id: str, wheel: Path, profile: str, huggingface_home: str | None) -> dict[str, Any]:
        return {"profile": profile}


class FakeCloud:
    def __init__(self) -> None:
        self.manager = FakeManager()
        self.proxy: list[dict[str, Any]] = []
        self.failure: BaseException | None = None

    def hosts(self) -> list[dict[str, Any]]:
        return self.manager.hosts()

    def connect(self, host_id: str) -> dict[str, Any]:
        return {"host": HOST, "gpus": [{"index": 0, "uuid": "GPU-1"}], "deployments": [{"id": "act", "status": "ready"}]}

    def catalog(self, host_id: str) -> dict[str, Any]:
        return self.connect(host_id)

    def request(self, host_id: str, method: str, path: str, *, json: Any = None,
                params: Any = None, timeout: float = 30) -> Any:
        self.proxy.append({"host_id": host_id, "method": method, "path": path, "json": json,
                           "params": params, "timeout": timeout})
        if self.failure is not None:
            raise self.failure
        return {"host_id": host_id, "method": method, "path": path}


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(
        MonitorConfig(
            store_path=tmp_path / "store.json",
            robot=RobotConfig(auto_connect=False),
            cameras=CamerasConfig(probe=False),
        ),
        apply_prefix=False,
    )
    real_cloud = app.state.hub.cloud
    cloud = FakeCloud()
    app.state.hub.cloud = cloud
    app.state.fake_cloud = cloud
    app.state.hub.start = MagicMock()
    app.state.hub.stop = MagicMock()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        real_cloud.close()


def test_hosts_add_probe_and_jobs(client) -> None:
    assert client.get("/api/cloud/hosts").json()[0]["alias"] == "8x4090-server"
    created = client.post("/api/cloud/hosts", json={"alias": "a6000", "root": "/data2/me/lerobot-monitor"})
    assert created.status_code == 201, created.text
    assert created.json()["alias"] == "a6000"
    invalid = client.post("/api/cloud/hosts", json={"alias": "bad alias", "root": "/data/me/cloud"})
    assert invalid.status_code == 422
    probe = client.post("/api/cloud/hosts/h1/probe")
    assert probe.status_code == 200 and probe.json()["python"] == "3.12.4"
    jobs = client.get("/api/cloud/jobs").json()
    assert jobs[0]["kind"] == "bootstrap"


def test_bootstrap_upgrade_runtime_and_upload_submit_jobs(client) -> None:
    assert client.post("/api/cloud/hosts/h1/bootstrap").status_code == 202
    assert client.post("/api/cloud/hosts/h1/upgrade").status_code == 202
    runtime = client.post(
        "/api/cloud/hosts/h1/runtime",
        json={"wheel_path": "/tmp/lerobot-0.4.4-py3-none-any.whl", "profile": "smolvla"},
    )
    assert runtime.status_code == 202, runtime.text
    assert [kind for _, kind in client.app.state.fake_cloud.manager.submitted] == ["bootstrap", "upgrade", "runtime"]
    missing = client.post("/api/cloud/hosts/h1/upload", json={"path": "relative/dir", "name": "ckpt"})
    assert missing.status_code == 400
    valid = client.post("/api/cloud/hosts/h1/upload", json={"path": str(Path.cwd()), "name": "ckpt"})
    assert valid.status_code == 202, valid.text


def test_cloud_proxy_forwards_body_query_and_status(client) -> None:
    deploy = client.post(
        "/api/cloud/hosts/h1/cloud/api/v1/deployments",
        json={"name": "SmolVLA", "source_kind": "huggingface", "source": "lerobot/smolvla_base"},
    )
    assert deploy.status_code == 200, deploy.text
    forwarded = client.app.state.fake_cloud.proxy[-1]
    assert forwarded["method"] == "POST"
    assert forwarded["path"] == "/api/v1/deployments"
    assert forwarded["json"]["source"] == "lerobot/smolvla_base"
    removed = client.delete("/api/cloud/hosts/h1/cloud/api/v1/deployments/act", params={"delete_files": "true"})
    assert removed.status_code == 200
    assert client.app.state.fake_cloud.proxy[-1]["params"] == {"delete_files": "true"}
    assert client.delete("/api/cloud/hosts/h1/cloud/api/v1/../../etc/passwd").status_code == 404
    assert client.get("/api/cloud/hosts/h1/cloud/api/v1/shutdown").status_code == 404


def test_cloud_proxy_surfaces_remote_and_transport_failures(client) -> None:
    client.app.state.fake_cloud.failure = CloudRequestError("deployment already exists", 409)
    conflict = client.post("/api/cloud/hosts/h1/cloud/api/v1/deployments", json={"name": "x"})
    assert conflict.status_code == 409
    assert "already exists" in conflict.json()["detail"]
    client.app.state.fake_cloud.failure = TransportError("Host is disconnected; connect it first")
    gateway = client.get("/api/cloud/hosts/h1/cloud/api/v1/gpus")
    assert gateway.status_code == 502


def test_cloud_reports_unavailable_manager(client) -> None:
    client.app.state.fake_cloud.manager = None
    response = client.get("/api/cloud/jobs")
    assert response.status_code == 503
