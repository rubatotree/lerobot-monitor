from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from lerobot_monitor.app import create_app
from lerobot_monitor.config import CamerasConfig, MonitorConfig, RobotConfig
from lerobot_monitor.model_hub import ModelRegistry
from lerobot_monitor.monitor_cloud import (
    CloudRequestError,
    CloudTarget,
    MonitorCloudClient,
    RemoteRTCInferenceEngine,
    parse_cloud_uri,
)
from lerobot_monitor.store import JsonStore


def test_cloud_target_round_trip_escapes_components() -> None:
    target = CloudTarget("host name", "deployment-id", "GPU:0/test")
    assert parse_cloud_uri(target.uri) == target
    mounted = CloudTarget("host name", "deployment-id")
    assert parse_cloud_uri(mounted.uri) == mounted
    assert "gpu=" not in mounted.uri
    assert parse_cloud_uri("C:/models/local") is None


def test_cloud_model_registry_is_playable_without_local_path(tmp_path: Path) -> None:
    registry = ModelRegistry(JsonStore(tmp_path / "store.json"), [])
    target = CloudTarget("8x4090-server", "smolvla")
    row = registry.register_cloud(
        path=target.uri,
        host_id=target.host_id,
        deployment={"id": target.deployment_id, "name": "SmolVLA", "metadata": {"policy_type": "smolvla"}},
    )

    assert row["source"] == "cloud"
    assert row["playable"] is True
    assert row["missing"] is False
    assert registry.list()[0]["path"] == target.uri


def test_cloud_model_registry_migrates_legacy_gpu_binding(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "store.json")
    legacy = CloudTarget("8x4090-server", "act", "GPU-old")
    store.put_model(
        {
            "id": "remote-act",
            "name": "Remote ACT",
            "source": "cloud",
            "path": legacy.uri,
            "remote": legacy.uri,
            "cloud_host_id": legacy.host_id,
            "cloud_deployment_id": legacy.deployment_id,
            "cloud_gpu_uuid": legacy.gpu_uuid,
        }
    )
    registry = ModelRegistry(store, [])
    mounted = CloudTarget(legacy.host_id, legacy.deployment_id)

    migrated = registry.list()[0]
    assert migrated["path"] == mounted.uri
    assert "cloud_gpu_uuid" not in migrated

    row = registry.register_cloud(
        path=mounted.uri,
        host_id=mounted.host_id,
        deployment={"id": mounted.deployment_id, "name": "Remote ACT"},
    )

    assert row["id"] == "remote-act"
    assert row["path"] == mounted.uri
    assert "cloud_gpu_uuid" not in row
    assert len(registry.list()) == 1


class FakeRemoteSession:
    action_keys = ["shoulder_pan.pos", "gripper.pos"]
    metadata: dict[str, Any] = {"rtc_training_max_delay": 0}
    overrides: dict[str, str] = {}

    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    def infer(self, observation: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "raw_actions": [[1.0, 2.0], [3.0, 4.0]],
            "actions": [[10.0, 20.0], [30.0, 40.0]],
            "action_keys": self.action_keys,
        }

    def reset(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_remote_rtc_engine_produces_monitor_joint_names_and_closes() -> None:
    session = FakeRemoteSession()
    engine = RemoteRTCInferenceEngine(session, fps=1, queue_threshold=0)  # type: ignore[arg-type]
    engine.observation_provider = lambda observation: observation
    engine.start()
    engine.notify_observation({"shoulder_pan": 0.0, "gripper": 0.0})
    engine.resume()
    deadline = time.monotonic() + 2
    while engine.qsize() == 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert engine.get_action(None) == {"shoulder_pan": 10.0, "gripper": 20.0}
    assert engine.stop() is True
    assert engine.wait_stopped(1) is True
    assert session.closed is True


class FakeManager:
    def hosts(self) -> list[dict[str, Any]]:
        return [{"id": "host", "alias": "host", "status": "connected"}]

    def endpoint(self, identifier: str) -> tuple[str, str]:
        return "http://127.0.0.1:1", "secret"

    def close(self) -> None:
        pass


def test_cached_cloud_residency_tracks_active_sessions(tmp_path: Path) -> None:
    client = MonitorCloudClient(tmp_path, manager=FakeManager())  # type: ignore[arg-type]
    target = CloudTarget("host", "model", "GPU-test")
    client._deployments[("host", "model")] = {"status": "loaded", "gpu_uuid": "GPU-test"}
    assert client.residency(target.uri)["state"] == "ready"
    client._session_opened(target)
    assert client.residency(target.uri)["state"] == "in_use"
    client._session_closed(target)
    assert client.residency(target.uri)["can_unload"] is True


def test_cloud_load_requires_explicit_gpu_and_keeps_identity_unbound(tmp_path: Path) -> None:
    client = MonitorCloudClient(tmp_path, manager=FakeManager())  # type: ignore[arg-type]
    target = CloudTarget("host", "model")
    ready = {"id": "model", "status": "ready", "gpu_uuid": None}
    loaded = {"id": "model", "status": "loaded", "gpu_uuid": "GPU-selected"}
    client.deployment = MagicMock(side_effect=[ready, ready, loaded])  # type: ignore[method-assign]

    def request(_host: str, method: str, path: str, **kwargs: Any) -> Any:
        if method == "POST" and path.endswith("/load"):
            assert kwargs["json"]["gpu_uuid"] == "GPU-selected"
            return {"job_id": "job"}
        if method == "GET" and path == "/api/v1/jobs":
            return [{"id": "job", "status": "succeeded"}]
        raise AssertionError((method, path))

    client.request = MagicMock(side_effect=request)  # type: ignore[method-assign]
    with pytest.raises(CloudRequestError, match="choose a cloud GPU"):
        client.ensure_loaded(target)

    result = client.ensure_loaded(target, gpu_uuid="GPU-selected")

    assert result == loaded
    assert target.uri == "cloud://host/model"


class FakeAppCloud:
    def __init__(self) -> None:
        self.loaded_gpu = ""

    def hosts(self) -> list[dict[str, Any]]:
        return [{"id": "8x4090-server", "alias": "8x4090-server", "status": "disconnected"}]

    def connect(self, host_id: str) -> dict[str, Any]:
        assert host_id == "8x4090-server"
        return {
            "host": self.hosts()[0],
            "gpus": [{"uuid": "GPU-test", "index": 1, "healthy": True, "busy": False}],
            "deployments": [
                {"id": "act-large", "name": "ACT Large", "status": "ready", "metadata": {"policy_type": "act"}}
            ],
        }

    def residency(self, uri: str) -> dict[str, Any]:
        instances = [] if not self.loaded_gpu else [{"id": "cloud:test", "device": self.loaded_gpu}]
        return {
            "state": "ready" if self.loaded_gpu else "unloaded",
            "instances": instances,
            "gpu_bytes": 0,
            "can_unload": bool(self.loaded_gpu),
            "source_paths": [uri],
        }

    def ensure_loaded(self, target: CloudTarget, *, gpu_uuid: str | None = None) -> dict[str, Any]:
        assert target.gpu_uuid == ""
        assert gpu_uuid
        self.loaded_gpu = gpu_uuid
        return {"status": "loaded", "gpu_uuid": gpu_uuid}

    def unload(self, target: CloudTarget) -> None:
        self.loaded_gpu = ""


def test_monitor_api_registers_cloud_deployment_as_library_model(tmp_path: Path) -> None:
    app = create_app(
        MonitorConfig(
            store_path=tmp_path / "store.json",
            robot=RobotConfig(auto_connect=False),
            cameras=CamerasConfig(probe=False),
        ),
        apply_prefix=False,
    )
    real_cloud = app.state.hub.cloud
    app.state.hub.cloud = FakeAppCloud()
    app.state.hub.start = MagicMock()
    app.state.hub.stop = MagicMock()
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/models/cloud",
                json={
                    "host_id": "8x4090-server",
                    "deployment_id": "act-large",
                    "name": "Remote ACT",
                },
            )
            assert response.status_code == 200, response.text
            row = response.json()
            assert row["source"] == "cloud" and row["playable"] is True
            listed = client.get("/api/models").json()
            assert listed[0]["path"] == CloudTarget("8x4090-server", "act-large").uri
            assert "cloud_gpu_uuid" not in listed[0]
            assert listed[0]["residency"]["state"] == "unloaded"
            deleted = client.delete("/api/library", params={"kind": "model", "id": row["id"]})
            assert deleted.status_code == 200, deleted.text
            assert client.get("/api/models").json() == []
    finally:
        real_cloud.close()


def test_monitor_api_selects_gpu_when_loading_cloud_model(tmp_path: Path) -> None:
    app = create_app(
        MonitorConfig(
            store_path=tmp_path / "store.json",
            robot=RobotConfig(auto_connect=False),
            cameras=CamerasConfig(probe=False),
        ),
        apply_prefix=False,
    )
    real_cloud = app.state.hub.cloud
    cloud = FakeAppCloud()
    app.state.hub.cloud = cloud
    app.state.hub.start = MagicMock()
    app.state.hub.stop = MagicMock()
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/models/cloud",
                json={
                    "host_id": "8x4090-server",
                    "deployment_id": "act-large",
                },
            )
            assert response.status_code == 200, response.text
            model_id = response.json()["id"]
            missing_gpu = client.post(f"/api/models/{model_id}/load", json={})
            assert missing_gpu.status_code == 400
            loaded = client.post(f"/api/models/{model_id}/load", json={"device": "GPU-test"})
            assert loaded.status_code == 202, loaded.text
            assert cloud.loaded_gpu == "GPU-test"
            assert loaded.json()["residency"]["instances"][0]["device"] == "GPU-test"
    finally:
        real_cloud.close()
