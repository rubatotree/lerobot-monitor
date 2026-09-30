"""Exercise the standalone cloud lifecycle without CUDA or Monitor initialization."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from lerobot_monitor.cloud.app import ApiBoundary, create_app
from lerobot_monitor.cloud import gpu as gpu_module
from lerobot_monitor.cloud.gpu import probe_gpus
from lerobot_monitor.cloud.runtime import CloudRuntime
from lerobot_monitor.cloud.schemas import DeploymentCreate, LoadRequest, SessionOpen
from lerobot_monitor.cloud.worker import SubprocessWorker, WorkerError, WORKER_BOOTSTRAP, worker_code_hash


class FakeWorker:
    instances: list[FakeWorker] = []
    metadata = {"action_keys": ["x", "y"], "state_keys": ["x", "y"], "action_dim": 2,
                "capabilities": {"select_action": True, "rtc_chunk": True, "debug_chunk": True}}

    def __init__(self, path: str, gpu: str, device: str, log_path: Path, **kwargs: Any) -> None:
        self.running = True
        self.stopped = threading.Event()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.calls: list[str] = []
        self.settings = kwargs
        self.instances.append(self)

    def alive(self) -> bool:
        return self.running

    def stop(self) -> None:
        self.running = False
        self.stopped.set()
        self.release.set()

    def call(self, operation: str, payload: dict[str, Any], *, timeout: float = 120) -> dict[str, Any]:
        self.calls.append(operation)
        if operation == "open":
            return self.metadata
        if operation == "infer":
            self.entered.set()
            if self.block:
                self.release.wait(timeout=3)
            return {"actions": [[1, 2]], "raw_actions": [[1, 2]], "shape": [1, 2], "action_keys": ["x", "y"]}
        return {}


def gpu_inventory() -> list[dict[str, Any]]:
    return [{"index": 3, "uuid": "GPU-test", "healthy": True, "busy": False, "memory_used_mb": 0}]


def wait_job(client: TestClient, response: Any) -> dict[str, Any]:
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        jobs = client.get("/api/v1/jobs").json()
        job = next(job for job in jobs if job["id"] == job_id)
        if job["status"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(tmp_path / "service", gpu_probe=gpu_inventory, worker_factory=FakeWorker)
    with TestClient(app, headers={"Authorization": f"Bearer {app.state.runtime.token}"}) as value:
        yield value


def deploy(client: TestClient, tmp_path: Path) -> str:
    path = tmp_path / f"checkpoint-{time.time_ns()}"
    path.mkdir()
    (path / "config.json").write_text('{"type":"act"}', encoding="utf-8")
    response = client.post("/api/v1/deployments", json={"name": "Test", "source_kind": "path", "source": str(path)})
    assert wait_job(client, response)["status"] == "succeeded"
    return response.json()["id"]


def loaded(client: TestClient, tmp_path: Path) -> str:
    deployment_id = deploy(client, tmp_path)
    response = client.post(f"/api/v1/deployments/{deployment_id}/load", json={"gpu_uuid": "GPU-test"})
    assert wait_job(client, response)["status"] == "succeeded"
    return deployment_id


def session(client: TestClient, deployment_id: str, mode: str = "select_action") -> str:
    response = client.post(f"/api/v1/deployments/{deployment_id}/sessions", json={"mode": mode})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def test_auth_and_independent_health(client: TestClient) -> None:
    assert client.get("/api/ui-config").json() == {"mode": "cloud"}
    response = client.get("/api/v1/health", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    response = client.post("/api/v1/deployments", content="not-json", headers={"Authorization": "bad"})
    assert response.status_code == 401
    assert client.get("/api/v1/health").json()["loaded_models"] == 0


def test_deploy_load_session_reset_unload(client: TestClient, tmp_path: Path) -> None:
    deployment_id = loaded(client, tmp_path)
    session_id = session(client, deployment_id)
    request = {"epoch": 0, "request_id": "first", "state": {"x": 0.0, "y": 0.0}}
    result = client.post(f"/api/v1/sessions/{session_id}/infer", json=request)
    assert result.status_code == 200
    assert result.json()["actions"] == [[1, 2]]
    timings = result.json()["timings"]
    # The body upload, the request validation and the whole service call are measured
    # separately; the fake worker reports no compute window of its own.
    assert {"read", "parse", "service"} <= set(timings)
    assert all(isinstance(value, float) and value >= 0 for value in timings.values())
    assert timings["worker"] == 0.0
    assert client.post(f"/api/v1/sessions/{session_id}/infer", json=request).status_code == 409
    assert client.post(f"/api/v1/sessions/{session_id}/reset", json={"epoch": 0}).json() == {"epoch": 1}
    assert client.post(f"/api/v1/sessions/{session_id}/heartbeat", json={"epoch": 0}).status_code == 409
    assert client.post(f"/api/v1/sessions/{session_id}/heartbeat", json={"epoch": 1}).status_code == 200
    assert client.delete(f"/api/v1/sessions/{session_id}").status_code == 200
    assert wait_job(client, client.post(f"/api/v1/deployments/{deployment_id}/unload"))["status"] == "succeeded"


def test_exclusive_model_lease_and_gpu_reservation(client: TestClient, tmp_path: Path) -> None:
    deployment_id = loaded(client, tmp_path)
    session_id = session(client, deployment_id)
    assert client.post(f"/api/v1/deployments/{deployment_id}/sessions", json={}).status_code == 409
    assert client.post(f"/api/v1/deployments/{deployment_id}/unload").status_code == 409
    assert client.delete(f"/api/v1/deployments/{deployment_id}").status_code == 409
    other = deploy(client, tmp_path)
    assert client.post(f"/api/v1/deployments/{other}/load", json={"gpu_uuid": "GPU-test"}).status_code == 409
    assert client.delete(f"/api/v1/sessions/{session_id}").status_code == 200


@pytest.mark.parametrize("prefix", [[], [[1, 2], [3]], [[1] * 257]])
def test_invalid_rtc_prefix(client: TestClient, tmp_path: Path, prefix: list[list[float]]) -> None:
    session_id = session(client, loaded(client, tmp_path), "rtc_chunk")
    request = {"epoch": 0, "request_id": "bad", "state": {"x": 0}, "prefix_raw": prefix, "prefix_absolute": prefix}
    assert client.post(f"/api/v1/sessions/{session_id}/infer", json=request).status_code == 422


def test_prefix_pair_shape_and_finite_validation(client: TestClient, tmp_path: Path) -> None:
    session_id = session(client, loaded(client, tmp_path), "rtc_chunk")
    base = {"epoch": 0, "request_id": "bad", "state": {"x": 0}}
    for extra in ({"prefix_raw": [[1, 2]]}, {"prefix_raw": [[1]], "prefix_absolute": [[1, 2]]}):
        assert client.post(f"/api/v1/sessions/{session_id}/infer", json={**base, **extra}).status_code == 422
    assert client.post(f"/api/v1/sessions/{session_id}/infer", content=json.dumps({**base, "state": {"x": float("nan")}})).status_code == 422


def test_expiry_discards_late_result_and_stops_only_owned_worker(client: TestClient, tmp_path: Path) -> None:
    runtime = client.app.state.runtime
    deployment_id = loaded(client, tmp_path)
    session_id = session(client, deployment_id)
    worker = runtime.workers[deployment_id]
    worker.block = True
    results: list[Any] = []
    thread = threading.Thread(target=lambda: results.append(client.post(f"/api/v1/sessions/{session_id}/infer",
        json={"epoch": 0, "request_id": "slow", "state": {"x": 0}})))
    thread.start()
    assert worker.entered.wait(2)
    assert client.post(f"/api/v1/sessions/{session_id}/infer", json={"epoch": 0, "request_id": "parallel", "state": {"x": 0}}).status_code == 409
    with runtime.lock:
        runtime.sessions[session_id].expires_at = 0
    runtime.tick()
    thread.join(3)
    assert not thread.is_alive()
    assert results[0].status_code == 410
    assert worker.stopped.is_set()
    assert not runtime.reserved
    assert not runtime.sessions


def test_idle_unload_preserves_weights_and_active_session(client: TestClient, tmp_path: Path) -> None:
    runtime = client.app.state.runtime
    deployment_id = loaded(client, tmp_path)
    session_id = session(client, deployment_id)
    runtime.idle_seconds = 0
    runtime.tick()
    assert deployment_id in runtime.workers
    client.delete(f"/api/v1/sessions/{session_id}")
    runtime.tick()
    deadline = time.monotonic() + 2
    while deployment_id in runtime.workers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert deployment_id not in runtime.workers
    assert Path(runtime.deployments[deployment_id]["path"]).exists()


def test_worker_death_releases_gpu(client: TestClient, tmp_path: Path) -> None:
    runtime = client.app.state.runtime
    deployment_id = loaded(client, tmp_path)
    runtime.workers[deployment_id].running = False
    runtime.tick()
    deadline = time.monotonic() + 2
    while runtime.reserved and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not runtime.reserved
    assert runtime.deployments[deployment_id]["status"] == "error"


def test_external_files_preserved_and_managed_deletion_bounded(client: TestClient, tmp_path: Path) -> None:
    deployment_id = deploy(client, tmp_path)
    path = Path(client.app.state.runtime.deployments[deployment_id]["path"])
    assert client.delete(f"/api/v1/deployments/{deployment_id}?delete_files=true").status_code == 422
    assert wait_job(client, client.delete(f"/api/v1/deployments/{deployment_id}"))["status"] == "succeeded"
    assert path.exists()
    with pytest.raises(ValueError, match="outside"):
        client.app.state.runtime._delete_managed(path, client.app.state.runtime.root / "assets")


def test_owned_upload_moves_and_deletes_only_upload(client: TestClient) -> None:
    runtime = client.app.state.runtime
    upload = runtime.root / "uploads" / "upload1"
    upload.mkdir()
    (upload / "config.json").write_text('{"type":"act"}', encoding="utf-8")
    response = client.post("/api/v1/deployments", json={"name": "upload", "source_kind": "path", "source": str(upload), "owned_upload": True})
    assert wait_job(client, response)["status"] == "succeeded"
    deployment_id = response.json()["id"]
    destination = runtime.root / "assets" / deployment_id
    assert destination.exists() and not upload.exists()
    assert wait_job(client, client.delete(f"/api/v1/deployments/{deployment_id}?delete_files=true"))["status"] == "succeeded"
    assert not destination.exists()


def test_invalid_model_and_owned_path(client: TestClient, tmp_path: Path) -> None:
    for config in (None, "[]", "{}", '{"type":3}'):
        path = tmp_path / f"invalid-{time.time_ns()}"
        path.mkdir()
        if config is not None:
            (path / "config.json").write_text(config, encoding="utf-8")
        assert client.post("/api/v1/deployments", json={"name": "invalid", "source_kind": "path", "source": str(path)}).status_code == 422
    path = tmp_path / "valid-external"
    path.mkdir()
    (path / "config.json").write_text('{"type":"act"}', encoding="utf-8")
    assert client.post("/api/v1/deployments", json={"name": "bad-own", "source_kind": "path", "source": str(path), "owned_upload": True}).status_code == 422


def test_restart_reconciles_residency_and_interrupted_jobs(tmp_path: Path) -> None:
    root = tmp_path / "root"
    app = create_app(root, gpu_probe=gpu_inventory, worker_factory=FakeWorker)
    with TestClient(app, headers={"Authorization": f"Bearer {app.state.runtime.token}"}) as client:
        deployment_id = loaded(client, tmp_path)
    # Simulate a crash snapshot rather than graceful unload.
    runtime = CloudRuntime(root, gpu_probe=gpu_inventory, worker_factory=FakeWorker)
    with runtime.lock:
        row = runtime.deployments[deployment_id]
        row.update(status="loaded", gpu_uuid="GPU-test")
        runtime._save("deployments", row)
        runtime._save("jobs", {"id": "interrupted", "status": "running"})
    runtime.close()
    restored = CloudRuntime(root, gpu_probe=gpu_inventory, worker_factory=FakeWorker)
    try:
        assert restored.deployments[deployment_id]["status"] == "ready"
        assert restored.deployments[deployment_id]["gpu_uuid"] is None
        assert restored.jobs["interrupted"]["status"] == "failed"
    finally:
        restored.close()


def test_manifest_read_at_each_load(client: TestClient, tmp_path: Path) -> None:
    runtime = client.app.state.runtime
    (runtime.root / "runtime.json").write_text(json.dumps({"python": sys.executable, "profile": "act", "huggingface_home": "/cache"}), encoding="utf-8")
    deployment_id = loaded(client, tmp_path)
    assert runtime.workers[deployment_id].settings == {"python": sys.executable, "hf_home": "/cache"}
    assert client.get("/api/v1/health").json()["runtime"]["configured"] is True


def test_partial_nvidia_error_keeps_idle_healthy_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = iter([
        subprocess.CompletedProcess([], 1, "3, GPU-good, RTX 4090, 24564, 504\nGPU error\n", "faulty GPU0"),
        subprocess.CompletedProcess([], 1, "", "faulty GPU0"),
        subprocess.CompletedProcess([], 0, "", ""),
    ])
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return next(outputs)

    monkeypatch.setattr(subprocess, "run", run)
    rows = probe_gpus()
    assert rows[0]["healthy"] is True and rows[0]["busy"] is False
    assert calls[-1][1:3] == ["-i", "GPU-good"]


def test_gpu_inventory_attributes_primary_user_and_program(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = iter([
        subprocess.CompletedProcess([], 0, "1, GPU-used, RTX 4090, 24564, 12288\n", ""),
        subprocess.CompletedProcess(
            [],
            0,
            "GPU-used, 42, /opt/runtime/python, 10240\nGPU-used, 84, renderer, 1024\n",
            "",
        ),
    ])
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: next(outputs))
    monkeypatch.setattr(
        gpu_module,
        "_process_owners",
        lambda pids: {
            42: {"user": "researcher", "program": "python"},
            84: {"user": "artist", "program": "renderer"},
        },
    )

    row = probe_gpus()[0]

    assert row["busy"] is True
    assert row["primary_user"] == "researcher"
    assert row["primary_program"] == "python"
    assert row["process_memory_used_mb"] == 11264
    assert [process["pid"] for process in row["processes"]] == [42, 84]


def test_ipc_subprocess_timeout_terminates_child(tmp_path: Path) -> None:
    fixture = tmp_path / "worker.py"
    fixture.write_text("import sys,json,time,os\nfor line in sys.stdin:\n m=json.loads(line)\n if m['operation']=='infer': time.sleep(10)\n print(json.dumps({'ok':True,'protocol_version':1,'worker_code_hash':" + repr(worker_code_hash()) + ",'result':{'gpu':os.environ['CUDA_VISIBLE_DEVICES']}}),flush=True)\n", encoding="utf-8")
    worker = SubprocessWorker("unused", "GPU-test", "cuda", tmp_path / "log", command=[sys.executable, "-u", str(fixture)], startup_timeout=3)
    assert worker.metadata["gpu"] == "GPU-test"
    with pytest.raises(WorkerError, match="timed out"):
        worker.call("infer", {}, timeout=0.05)
    assert not worker.alive()
    worker.stop()


def test_chunked_body_cap_and_early_auth() -> None:
    async def run(auth: bool) -> list[dict[str, Any]]:
        async def unreachable(scope: Any, receive: Any, send: Any) -> None:
            raise AssertionError("must reject before application")

        incoming = iter([{"type": "http.request", "body": b"123", "more_body": True},
                         {"type": "http.request", "body": b"456", "more_body": False}])
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return next(incoming)

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        middleware = ApiBoundary(unreachable, "test", max_bytes=5)
        await middleware({"type": "http", "path": "/api/v1/deployments", "headers": [(b"authorization", b"Bearer test")] if auth else []}, receive, send)
        return sent

    assert asyncio.run(run(True))[0]["status"] == 413
    assert asyncio.run(run(False))[0]["status"] == 401


def test_managed_subdirectory_cannot_be_registered_externally(client: TestClient) -> None:
    runtime = client.app.state.runtime
    for name in ("assets", "uploads", "staging"):
        path = runtime.root / name / "owner" / "nested"
        path.mkdir(parents=True)
        (path / "config.json").write_text('{"type":"act"}', encoding="utf-8")
        response = client.post("/api/v1/deployments", json={"name": "nested", "source_kind": "path", "source": str(path)})
        assert response.status_code == 422
        assert "managed" in response.json()["detail"]


def test_runtime_profiles_select_by_policy(client: TestClient, tmp_path: Path) -> None:
    runtime = client.app.state.runtime
    (runtime.root / "runtime.json").write_text(json.dumps({"profiles": {
        "act": {"python": "/runtime/act/python", "huggingface_home": "/cache"},
        "smolvla": {"python": "/runtime/vla/python", "huggingface_home": "/cache"},
    }}), encoding="utf-8")
    deployment_id = loaded(client, tmp_path)
    assert runtime.workers[deployment_id].settings["python"] == "/runtime/act/python"
    assert wait_job(client, client.post(f"/api/v1/deployments/{deployment_id}/unload"))["status"] == "succeeded"
    config = Path(runtime.deployments[deployment_id]["path"]) / "config.json"
    config.write_text('{"type":"smolvla"}', encoding="utf-8")
    assert wait_job(client, client.post(f"/api/v1/deployments/{deployment_id}/load", json={"gpu_uuid": "GPU-test"}))["status"] == "succeeded"
    assert runtime.workers[deployment_id].settings["python"] == "/runtime/vla/python"


def test_pending_load_blocks_mutations(client: TestClient, tmp_path: Path) -> None:
    runtime = client.app.state.runtime
    entered, release = threading.Event(), threading.Event()

    def factory(*args: Any, **kwargs: Any) -> FakeWorker:
        entered.set()
        assert release.wait(3)
        return FakeWorker(*args, **kwargs)

    runtime.worker_factory = factory
    deployment_id = deploy(client, tmp_path)
    response = client.post(f"/api/v1/deployments/{deployment_id}/load", json={"gpu_uuid": "GPU-test"})
    try:
        assert entered.wait(2)
        assert client.post(f"/api/v1/deployments/{deployment_id}/unload").status_code == 409
        assert client.delete(f"/api/v1/deployments/{deployment_id}").status_code == 409
        assert client.post(f"/api/v1/deployments/{deployment_id}/sessions", json={}).status_code == 409
    finally:
        release.set()
    assert wait_job(client, response)["status"] == "succeeded"


def test_job_queue_saturation_leaves_no_deployment(client: TestClient, tmp_path: Path) -> None:
    runtime = client.app.state.runtime
    for _ in range(32):
        assert runtime.slots.acquire(blocking=False)
    try:
        path = tmp_path / "checkpoint"
        path.mkdir()
        (path / "config.json").write_text('{"type":"act"}', encoding="utf-8")
        result = client.post("/api/v1/deployments", json={"name": "too many", "source_kind": "path", "source": str(path)})
        assert result.status_code == 429
        assert not runtime.deployments
    finally:
        for _ in range(32):
            runtime.slots.release()


def test_worker_bootstrap_preserves_runtime_dependency_environment(tmp_path: Path) -> None:
    release = tmp_path / "service-site-packages"
    package = release / "lerobot_monitor"
    (package / "cloud").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cloud" / "__init__.py").write_text("", encoding="utf-8")
    (package / "cloud" / "worker.py").write_text("import cloud_dependency, json\nprint(json.dumps({'dependency':cloud_dependency.VALUE,'worker':'new-release'}))\n", encoding="utf-8")
    (release / "cloud_dependency.py").write_text("VALUE='wrong-management-version'", encoding="utf-8")
    runtime = tmp_path / "runtime-site-packages"
    runtime.mkdir()
    (runtime / "cloud_dependency.py").write_text("VALUE='correct-runtime-version'", encoding="utf-8")
    result = subprocess.run([sys.executable, "-c", WORKER_BOOTSTRAP, str(package)],
        env={**os.environ, "PYTHONPATH": str(runtime)}, capture_output=True, text=True, timeout=5, check=True)
    assert json.loads(result.stdout) == {"dependency": "correct-runtime-version", "worker": "new-release"}


def test_daemon_stop_waits_for_root_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import lerobot_monitor.cloud.__main__ as cli

    attempts: list[str] = []
    statuses = iter([{"running": True, "port": 8091}, {"running": False}, {"running": False}])
    monkeypatch.setattr(cli, "daemon_status", lambda root, port: next(statuses))
    monkeypatch.setattr(cli, "request_service", lambda *args, **kwargs: {})
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)

    class Lock:
        def __init__(self, root: Path) -> None:
            attempts.append("lock")
            if len(attempts) == 1:
                raise RuntimeError("still shutting down")

        def close(self) -> None:
            attempts.append("closed")

    monkeypatch.setattr(cli, "RootLock", Lock)
    assert cli.daemon("stop", tmp_path, 8091)["running"] is False
    assert attempts == ["lock", "lock", "closed"]


def test_daemon_real_start_status_stop(tmp_path: Path) -> None:
    root = tmp_path / "daemon"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}

    def run(action: str) -> dict[str, Any]:
        result = subprocess.run([sys.executable, "-m", "lerobot_monitor.cloud", "daemon", action,
            "--root", str(root), "--port", str(port)], env=environment, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    try:
        started = run("start")
        assert started["running"] is True
        assert run("status")["instance_id"] == started["instance_id"]
        assert run("start")["instance_id"] == started["instance_id"]
    finally:
        assert run("stop")["running"] is False
    assert run("status")["running"] is False


def test_cloud_import_never_imports_monitor_hub() -> None:
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    result = subprocess.run([sys.executable, "-c", "import lerobot_monitor.cloud.app, sys; assert 'lerobot_monitor.hub' not in sys.modules"],
        env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
