from __future__ import annotations

import itertools
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from lerobot_monitor.app import create_app
from lerobot_monitor.config import CamerasConfig, MonitorConfig, RobotConfig
from lerobot_monitor.model_hub import ModelRegistry
from lerobot_monitor.monitor_cloud import (
    CloudRequestError,
    CloudTarget,
    MonitorCloudClient,
    RemoteChunkEvent,
    RemoteRTCInferenceEngine,
    RemoteSession,
    RemoteSyncInferenceEngine,
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


class LifecycleRemoteSession(FakeRemoteSession):
    """Scripts whole chunks so the engine's lifecycle events can be asserted."""

    def __init__(self, *, delay_s: float = 0.0, error: Exception | None = None) -> None:
        super().__init__()
        self.delay_s = delay_s
        self.error = error
        self.stages: list[Any] = []
        self.prefix_calls: list[Any] = []

    def infer(self, observation: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        self.prefix_calls.append(kwargs.get("prefix_raw"))
        if self.error is not None:
            raise self.error
        if self.delay_s:
            time.sleep(self.delay_s)
        rows = [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [7.0, 8.0]]
        self.stages = [_LifecycleStage("cloud_compute", 100.0, 100.1)]
        return {"raw_actions": rows, "actions": rows, "action_keys": self.action_keys}


class _LifecycleStage:
    """Minimal cloud-stage stand-in: note_stage reads name/start/end/gpu_ms."""

    def __init__(self, name: str, start: float, end: float) -> None:
        self.name = name
        self.start = start
        self.end = end
        self.gpu_ms = None


def test_remote_rtc_engine_reports_chunk_lifecycle() -> None:
    session = LifecycleRemoteSession(delay_s=0.2)
    engine = RemoteRTCInferenceEngine(session, fps=1, queue_threshold=2)  # type: ignore[arg-type]
    events: list[RemoteChunkEvent] = []
    engine.chunk_observer = events.append
    engine.observation_provider = lambda observation: observation
    engine.start()
    try:
        engine.notify_observation({"shoulder_pan": 0.0, "gripper": 0.0})
        engine.resume()
        deadline = time.monotonic() + 2
        while engine.qsize() == 0 and time.monotonic() < deadline:
            time.sleep(0.01)

        assert engine.get_action(None) == {"shoulder_pan": 1.0, "gripper": 2.0}
        assert engine.get_action(None) == {"shoulder_pan": 3.0, "gripper": 4.0}
        # Refilling from the two leftover steps makes the 0.2 s round trip the delay.
        deadline = time.monotonic() + 3
        while engine.qsize() < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert engine.qsize() == 3
        assert engine.stop() is True
    finally:
        engine.stop()

    assert session.closed is True
    lifecycle = [(event.kind, event.chunk_id) for event in events if event.kind != "stage"]
    assert lifecycle == [
        ("started", 1), ("ready", 1), ("accepted", 1),
        ("consumed", 1), ("consumed", 1),
        ("started", 2), ("ready", 2), ("accepted", 2),
    ]
    by_key = {(event.kind, event.chunk_id): event for event in events}
    assert by_key[("ready", 1)].steps == 4
    first = by_key[("accepted", 1)]
    assert (first.steps, first.merge.prefix_trimmed, first.merge.replaced) == (4, 0, ())  # type: ignore[union-attr]
    assert first.actions[0] == {"shoulder_pan": 1.0, "gripper": 2.0}
    assert [event.action_index for event in events if event.kind == "consumed"] == [0, 1]
    # One step elapsed during the round trip at fps=1, and the two steps the consumer
    # left behind are the replaced steps LeRobot's merge would report.
    second = by_key[("accepted", 2)]
    assert (second.steps, second.merge.prefix_trimmed, second.merge.replaced) == (3, 1, ((1, 2),))  # type: ignore[union-attr]
    assert session.prefix_calls == [None, [[5.0, 6.0], [7.0, 8.0]]]
    assert {event.chunk_id for event in events if event.kind == "stage"} == {1, 2}


def test_remote_rtc_engine_reports_a_failed_chunk() -> None:
    session = LifecycleRemoteSession(error=CloudRequestError("cloud policy failed"))
    engine = RemoteRTCInferenceEngine(session, fps=1, queue_threshold=2)  # type: ignore[arg-type]
    events: list[RemoteChunkEvent] = []
    engine.chunk_observer = events.append
    engine.observation_provider = lambda observation: observation
    engine.start()
    try:
        engine.notify_observation({"shoulder_pan": 0.0, "gripper": 0.0})
        engine.resume()
        deadline = time.monotonic() + 2
        while not engine.failed and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        engine.stop()

    assert engine.failed is True
    assert [(event.kind, event.chunk_id) for event in events] == [("started", 1), ("failed", 1)]


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


def _wait_for_state(client: MonitorCloudClient, uri: str, state: str) -> dict[str, Any]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        residency = client.residency(uri)
        if residency["state"] == state:
            return residency
        time.sleep(0.05)
    raise AssertionError(f"residency never became {state}: {client.residency(uri)}")


def test_cloud_residency_shows_loading_then_ready_after_submit(tmp_path: Path) -> None:
    """The sidebar needs a loading phase and must see the load finish on the host."""
    client = MonitorCloudClient(tmp_path, manager=FakeManager())  # type: ignore[arg-type]
    target = CloudTarget("host", "model")
    remote = {"id": "model", "status": "loading", "gpu_uuid": "GPU-1", "error": None}
    client.deployment = MagicMock(  # type: ignore[method-assign]
        return_value={"id": "model", "status": "ready", "gpu_uuid": None}
    )

    def request(_host: str, method: str, path: str, **_kwargs: Any) -> Any:
        if method == "POST":
            return {"job_id": "job-1"}
        return [dict(remote)]

    client.request = MagicMock(side_effect=request)  # type: ignore[method-assign]
    client.submit_load(target, gpu_uuid="GPU-1")

    loading = client.residency(target.uri)
    assert loading["state"] == "loading"
    assert loading["can_unload"] is False
    instance = loading["instances"][0]
    assert instance["state"] == "loading" and instance["device"] == "GPU-1"
    assert instance["remote"] is True and instance["elapsed_ms"] >= 0

    remote.update(status="loaded")
    ready = _wait_for_state(client, target.uri, "ready")
    assert ready["can_unload"] is True
    assert "elapsed_ms" not in ready["instances"][0]


def test_cloud_residency_reports_a_failed_load(tmp_path: Path) -> None:
    client = MonitorCloudClient(tmp_path, manager=FakeManager())  # type: ignore[arg-type]
    target = CloudTarget("host", "model")
    client._deployments[("host", "model")] = {"status": "error", "gpu_uuid": None, "error": "CUDA out of memory"}
    client.request = MagicMock(return_value=[])  # type: ignore[method-assign]

    failed = client.residency(target.uri)

    assert failed["state"] == "error"
    assert failed["instances"][0]["error"] == "CUDA out of memory"


def test_cloud_residency_refresh_skips_disconnected_hosts(tmp_path: Path) -> None:
    class Disconnected(FakeManager):
        def endpoint(self, identifier: str) -> tuple[str, str]:
            raise RuntimeError("Host is disconnected; connect it first")

    client = MonitorCloudClient(tmp_path, manager=Disconnected())  # type: ignore[arg-type]
    target = CloudTarget("host", "model")
    client._deployments[("host", "model")] = {"status": "loaded", "gpu_uuid": "GPU-1"}
    client.request = MagicMock()  # type: ignore[method-assign]

    assert client.residency(target.uri)["state"] == "ready"
    time.sleep(0.2)
    client.request.assert_not_called()


def test_remote_sync_engine_exposes_the_queued_actions_as_preview() -> None:
    from lerobot_monitor.policy import inference_leftover_poses

    class QueuedSession(FakeRemoteSession):
        def infer(self, observation: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            return {
                "actions": [[10.0, 20.0]],
                "queued_actions": [[11.0, 21.0], [12.0, 22.0]],
                "action_keys": self.action_keys,
            }

    engine = RemoteSyncInferenceEngine(QueuedSession())  # type: ignore[arg-type]
    assert engine.leftover_poses({"shoulder_pan": 0.0}) == []
    assert engine.get_action({"shoulder_pan": 0.0}) == {"shoulder_pan": 10.0, "gripper": 20.0}

    preview = inference_leftover_poses(engine, None, {"wrist_roll": 5.0})  # type: ignore[arg-type]
    assert preview == [
        {"wrist_roll": 5.0, "shoulder_pan": 11.0, "gripper": 21.0},
        {"wrist_roll": 5.0, "shoulder_pan": 12.0, "gripper": 22.0},
    ]


def test_remote_sync_engine_without_a_reported_queue_has_an_empty_preview() -> None:
    engine = RemoteSyncInferenceEngine(FakeRemoteSession())  # type: ignore[arg-type]
    engine.get_action({"shoulder_pan": 0.0})
    assert engine.leftover_poses({}) == []


def test_remote_rtc_engine_previews_its_pending_queue() -> None:
    from lerobot_monitor.policy import inference_leftover_poses

    session = FakeRemoteSession()
    engine = RemoteRTCInferenceEngine(session, fps=1, queue_threshold=0)  # type: ignore[arg-type]
    engine._queue.extend([([1.0, 2.0], {"shoulder_pan": 3.0, "gripper": 4.0}, 1, 0)])
    assert inference_leftover_poses(engine, None, {"wrist_roll": 1.0}) == [  # type: ignore[arg-type]
        {"wrist_roll": 1.0, "shoulder_pan": 3.0, "gripper": 4.0}
    ]


def test_cloud_request_forwards_params_and_preserves_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MonitorCloudClient(tmp_path, manager=FakeManager())  # type: ignore[arg-type]
    captured: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        captured.update({"method": method, "url": url, **kwargs})
        return httpx.Response(
            409, json={"detail": "deployment already exists"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("lerobot_monitor.monitor_cloud.httpx.request", fake_request)
    with pytest.raises(CloudRequestError) as error:
        client.request("host", "DELETE", "/api/v1/deployments/x", params={"delete_files": "true"})
    assert error.value.status == 409
    assert "already exists" in str(error.value)
    assert captured["params"] == {"delete_files": "true"}
    assert captured["headers"]["Authorization"] == "Bearer secret"
    client.close()


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
    with pytest.raises(CloudRequestError, match="not loaded"):
        client.ensure_loaded(target)

    result = client.ensure_loaded(target, gpu_uuid="GPU-selected")

    assert result == loaded
    assert target.uri == "cloud://host/model"


def test_unloaded_cloud_reuses_remembered_gpu(tmp_path: Path) -> None:
    """A deployment loaded once can be reloaded without re-prompting for a GPU."""
    client = MonitorCloudClient(tmp_path, manager=FakeManager())  # type: ignore[arg-type]
    target = CloudTarget("host", "model")
    ready = {"id": "model", "status": "ready", "gpu_uuid": None}
    loaded = {"id": "model", "status": "loaded", "gpu_uuid": "GPU-selected"}
    client.deployment = MagicMock(side_effect=[ready, loaded])  # type: ignore[method-assign]
    client._remember_gpu(target, "GPU-selected")

    seen: list[str] = []

    def request(_host: str, method: str, path: str, **kwargs: Any) -> Any:
        if method == "POST" and path.endswith("/load"):
            seen.append(kwargs["json"]["gpu_uuid"])
            return {"job_id": "job"}
        if method == "GET" and path == "/api/v1/jobs":
            return [{"id": "job", "status": "succeeded"}]
        raise AssertionError((method, path))

    client.request = MagicMock(side_effect=request)  # type: ignore[method-assign]
    assert client.ensure_loaded(target) == loaded
    assert seen == ["GPU-selected"]


def test_loaded_deployment_remembers_its_gpu(tmp_path: Path) -> None:
    client = MonitorCloudClient(tmp_path, manager=FakeManager())  # type: ignore[arg-type]
    target = CloudTarget("host", "model")
    client.deployment = MagicMock(  # type: ignore[method-assign]
        return_value={"id": "model", "status": "loaded", "gpu_uuid": "GPU-live"}
    )

    client.ensure_loaded(target)

    assert client._remembered_gpu(target) == "GPU-live"


def test_encode_images_names_the_camera_that_has_no_frame() -> None:
    """A missing frame must name the camera, not read as a cloud-side failure."""
    import numpy as np

    from lerobot_monitor.monitor_cloud import _encode_images

    good = np.zeros((480, 640, 3), dtype=np.uint8)
    assert list(_encode_images({"front": good}, camera_names=["front"])) == ["front"]

    with pytest.raises(CloudRequestError, match=r"camera 'side' has no usable RGB frame \(no frame\)"):
        _encode_images({"front": good, "side": None}, camera_names=["front", "side"])
    with pytest.raises(CloudRequestError, match="shape \\(480, 640\\)"):
        _encode_images({"front": np.zeros((480, 640), dtype=np.uint8)}, camera_names=["front"])

    # A camera the policy does not require must not fail the request.
    assert list(_encode_images({"front": good, "side": None}, camera_names=["front"])) == ["front"]

    # A required camera absent from the observation entirely is still reported.
    with pytest.raises(CloudRequestError, match=r"no usable frame for policy camera\(s\): \['front'\]"):
        _encode_images({"other": good}, camera_names=["front"])


def test_cloud_inference_reports_transfer_and_compute_stages() -> None:
    """The profiling timeline must show upload/download separately from GPU compute."""
    import numpy as np

    from lerobot_monitor.rollout_timeline import RolloutTimeline

    class Owner:
        def request(self, _host: str, method: str, path: str, **_kwargs: Any) -> Any:
            if method == "POST" and path.endswith("/sessions"):
                return {"session_id": "s", "epoch": 0, "action_keys": ["a.pos"]}
            if method == "POST" and path.endswith("/infer"):
                time.sleep(0.04)
                return {"actions": [[1.0]], "action_keys": ["a.pos"], "compute_seconds": 0.02}
            return {}

        def _session_opened(self, _target: Any) -> None:
            pass

        def _session_closed(self, _target: Any) -> None:
            pass

    session = RemoteSession(
        Owner(), CloudTarget("h", "d"), "select_action", "t", ["a"], {}, ["front"],  # type: ignore[arg-type]
    )
    engine = RemoteSyncInferenceEngine(session)
    timeline = RolloutTimeline()
    timeline.set_enabled(True)
    token = timeline.note_inference_start(kind="cloud-sync", step_s=0.1)
    engine.stage_observer = lambda stage: timeline.note_stage(token, stage)
    try:
        engine.get_action({"a": 1.0, "front": np.zeros((480, 640, 3), dtype=np.uint8)})
        timeline.note_inference_end(token, ok=True, steps=1)

        stages = {stage["name"]: stage for stage in timeline.snapshot()["blocks"][0]["stages"]}
        assert {"cloud_encode", "cloud_upload", "cloud_compute", "cloud_download"} <= set(stages)
        for stage in stages.values():
            assert stage["end"] >= stage["start"]
        # The worker's own compute window is surfaced as the GPU duration.
        assert stages["cloud_compute"]["gpu_ms"] == pytest.approx(20.0, abs=0.001)
        # Phases must not overlap, or the chart would double-count the round trip.
        ordered = sorted(stages.values(), key=lambda stage: stage["start"])
        for earlier, later in itertools.pairwise(ordered):
            assert later["start"] >= earlier["end"] - 1e-6
    finally:
        session.close()


def test_submit_load_returns_without_waiting_for_the_job(tmp_path: Path) -> None:
    """A cold load must not hold the request open while the job runs."""
    client = MonitorCloudClient(tmp_path, manager=FakeManager())  # type: ignore[arg-type]
    target = CloudTarget("host", "model")
    client.deployment = MagicMock(  # type: ignore[method-assign]
        return_value={"id": "model", "status": "ready", "gpu_uuid": None}
    )
    seen: list[str] = []

    def request(_host: str, method: str, path: str, **kwargs: Any) -> Any:
        seen.append(f"{method} {path}")
        if method == "POST" and path.endswith("/load"):
            return {"job_id": "job-1"}
        raise AssertionError(f"submit_load must not poll jobs, saw {method} {path}")

    client.request = MagicMock(side_effect=request)  # type: ignore[method-assign]

    result = client.submit_load(target, gpu_uuid="GPU-1")

    assert result == {"status": "loading", "job_id": "job-1", "gpu_uuid": "GPU-1"}
    # Exactly one call: no job polling happened before returning.
    assert seen == ["POST /api/v1/deployments/model/load"]


def test_ensure_loaded_still_waits_for_completion(tmp_path: Path) -> None:
    """Rollout depends on ensure_loaded returning only usable weights."""
    client = MonitorCloudClient(tmp_path, manager=FakeManager())  # type: ignore[arg-type]
    target = CloudTarget("host", "model")
    ready = {"id": "model", "status": "ready", "gpu_uuid": None}
    loaded = {"id": "model", "status": "loaded", "gpu_uuid": "GPU-1"}
    # submit_load reads the deployment once; wait_for_load reads it again on success.
    client.deployment = MagicMock(side_effect=[ready, loaded])  # type: ignore[method-assign]

    def request(_host: str, method: str, path: str, **_kwargs: Any) -> Any:
        if method == "POST" and path.endswith("/load"):
            return {"job_id": "job-1"}
        if method == "GET" and path == "/api/v1/jobs":
            return [{"id": "job-1", "status": "succeeded"}]
        raise AssertionError((method, path))

    client.request = MagicMock(side_effect=request)  # type: ignore[method-assign]

    assert client.ensure_loaded(target, gpu_uuid="GPU-1") == loaded



def test_remote_session_reports_a_required_camera_with_no_frame() -> None:
    """The payload that reaches the cloud must never silently omit a policy camera."""
    import numpy as np

    class Owner:
        def __init__(self) -> None:
            self.payloads: list[dict[str, Any]] = []

        def request(self, _host: str, method: str, path: str, **kwargs: Any) -> Any:
            if method == "POST" and path.endswith("/sessions"):
                return {"session_id": "s", "epoch": 0, "action_keys": ["a.pos"]}
            if method == "POST" and path.endswith("/infer"):
                self.payloads.append(kwargs["json"])
                return {"actions": [[1.0]], "action_keys": ["a.pos"]}
            return {}

        def _session_opened(self, _target: Any) -> None:
            pass

        def _session_closed(self, _target: Any) -> None:
            pass

    owner = Owner()
    session = RemoteSession(
        owner, CloudTarget("h", "d"), "select_action", "t", ["a"], {}, ["front"],  # type: ignore[arg-type]
    )
    try:
        session.infer({"a": 1.0, "front": np.zeros((480, 640, 3), dtype=np.uint8)})
        assert list(owner.payloads[-1]["images"]) == ["front"]

        with pytest.raises(CloudRequestError, match="no usable frame for policy camera"):
            session.infer({"a": 1.0})
    finally:
        session.close()


def test_labelled_camera_keys_survive_the_cloud_round_trip() -> None:
    """The exact failure: a device named '0' labelled 'front' must not read as missing."""
    import numpy as np

    from lerobot_monitor.cameras import CameraHub, DeviceCamera, rollout_camera_key
    from lerobot_monitor.cloud.worker import decode_images
    from lerobot_monitor.config import CamerasConfig
    from lerobot_monitor.monitor_cloud import _encode_images

    device = DeviceCamera(0, width=4, height=4, jpeg_quality=80, port=5000)
    device.label = "front"
    # The label is the policy-facing key, not the opaque device index.
    assert rollout_camera_key(device) == "front"

    unlabelled = DeviceCamera(1, width=4, height=4, jpeg_quality=80, port=5001)
    unlabelled.label = ""
    assert rollout_camera_key(unlabelled) == "1"

    hub = CameraHub(CamerasConfig(probe=False))
    device.enabled, device.feed_robot = True, True
    hub.streams = {"0": device}
    device._bgr = np.zeros((4, 4, 3), dtype=np.uint8)
    device.frame_received_at = time.perf_counter()

    # CameraHub keys by label, so the model's declared image key is satisfied.
    frames = hub.rollout_rgb_map(max_age_s=5, max_skew_s=5)
    assert set(frames) == {"front"}

    payload = _encode_images(frames, camera_names=["front"])
    decoded = decode_images(payload)
    mapped = {
        name if name.startswith("observation.images.") else f"observation.images.{name}"
        for name in decoded
    }
    assert "observation.images.front" in mapped


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

    def submit_load(self, target: CloudTarget, *, gpu_uuid: str | None = None) -> dict[str, Any]:
        """The API submits without waiting, so it must not block on the load."""
        assert target.gpu_uuid == ""
        assert gpu_uuid
        self.loaded_gpu = gpu_uuid
        return {"status": "loading", "job_id": "cloud-job-1", "gpu_uuid": gpu_uuid}

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
            # The endpoint submits the load and returns at once; progress is polled
            # separately, so a slow cold load never blocks this response.
            assert loaded.json()["job_id"] == "cloud-job-1"
            assert loaded.json()["loading"] is True
            assert loaded.json()["residency"]["instances"][0]["device"] == "GPU-test"
    finally:
        real_cloud.close()
