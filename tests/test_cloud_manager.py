from __future__ import annotations

import json
import io
import subprocess
import sys
import threading
import tarfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from lerobot_monitor.cloud_manager.app import create_app, safe_proxy_path
from lerobot_monitor.cloud_manager.artifacts import CLEANUP_UPLOAD, EXTRACT_UPLOAD, RUNTIME_REQUIREMENTS, VERIFY_UPLOAD, WRITE_RUNTIME_MANIFEST, checkpoint_archive
from lerobot_monitor.cloud_manager.manager import CloudManager, HostInput
from lerobot_monitor.cloud_manager.transport import SSHTransport, TransportError


class FakeProcess:
    def __init__(self) -> None:
        self.stopped = False

    def poll(self) -> int | None:
        return 0 if self.stopped else None


class FakeSSH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], Any]] = []
        self.processes: list[FakeProcess] = []

    def run(self, alias: str, arguments: list[str], **kwargs: Any) -> str:
        self.calls.append((alias, arguments, kwargs))
        return "sensitive-token-1234567890abcdef"

    def tunnel(self, alias: str, port: int) -> tuple[FakeProcess, int]:
        process = FakeProcess()
        self.processes.append(process)
        return process, 18991

    def stop(self, process: FakeProcess) -> None:
        process.stopped = True


class HealthyClient:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> HealthyClient:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        assert kwargs["headers"]["Authorization"].startswith("Bearer sensitive-")
        return httpx.Response(200, json={"status": "ok"}, request=httpx.Request("GET", url))


@pytest.mark.parametrize("alias", ["-oProxyCommand=evil", "host;echo bad", "user@host", "a\nb", "$(bad)"])
def test_alias_cannot_inject_ssh_arguments(alias: str) -> None:
    with pytest.raises(ValidationError):
        HostInput(alias=alias, root="/data/me/cloud")


@pytest.mark.parametrize("root", ["/", "/data", "../data/cloud", "/data/../cloud", "/data/me\n/cloud"])
def test_root_requires_dedicated_absolute_directory(root: str) -> None:
    with pytest.raises(ValidationError):
        HostInput(alias="server", root=root)


def test_remote_shell_arguments_are_quoted_and_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, b"ok", b"")

    monkeypatch.setattr(subprocess, "run", run)
    SSHTransport().run("server", ["python3", "-c", "print('ok')", "/data/a; touch /tmp/evil"], data=b"secret")
    command, options = calls[0]
    assert "StrictHostKeyChecking=yes" in command
    assert "BatchMode=yes" in command
    assert "'/data/a; touch /tmp/evil'" in command[-1]
    assert "secret" not in " ".join(command)
    assert options["input"] == b"secret"


def test_ssh_timeout_has_safe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired("secret command", 1, stderr=b"secret")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(TransportError, match="timed out") as result:
        SSHTransport().run("server", ["true"], timeout=1)
    assert "secret" not in str(result.value)


def test_reconnect_and_shutdown_clean_owned_tunnels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ssh = FakeSSH()
    monkeypatch.setattr(httpx, "Client", HealthyClient)
    manager = CloudManager(tmp_path, transport=ssh)
    manager.connect("8x4090-server")
    manager.connect("8x4090-server")
    assert ssh.processes[0].stopped
    assert not ssh.processes[1].stopped
    assert "sensitive" not in json.dumps(manager.hosts())
    assert "sensitive" not in json.dumps(manager.jobs())
    credentials = json.loads((tmp_path / "credentials.json").read_text())
    assert credentials["8x4090-server"].startswith("sensitive-")
    manager.close()
    assert ssh.processes[1].stopped
    assert all(call[0] == "8x4090-server" for call in ssh.calls)


def test_failed_health_check_does_not_leave_tunnel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenClient(HealthyClient):
        def get(self, url: str, **kwargs: Any) -> httpx.Response:
            raise httpx.ConnectError("secret")

    ssh = FakeSSH()
    monkeypatch.setattr(httpx, "Client", BrokenClient)
    manager = CloudManager(tmp_path, transport=ssh)
    with pytest.raises(TransportError, match="health check"):
        manager.connect("8x4090-server")
    assert ssh.processes[0].stopped
    assert manager.hosts()[0]["status"] == "disconnected"
    manager.close()


def test_guard_and_persistence(tmp_path: Path) -> None:
    manager = CloudManager(tmp_path)
    with TestClient(create_app(manager=manager), base_url="http://127.0.0.1:8095") as client:
        assert client.get("/api/ui-config").json() == {"mode": "manager"}
        assert client.get("/api/hosts", headers={"Host": "evil.example"}).status_code == 403
        payload = {"alias": "mine", "root": "/data/me/cloud"}
        assert client.post("/api/hosts", json=payload, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/hosts", json=payload, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        response = client.post("/api/hosts", json=payload, headers={"Origin": "http://127.0.0.1:8095"})
        assert response.status_code == 201
        assert client.post("/api/hosts/unknown/probe").status_code == 404
        assert client.get("/api/hosts/8x4090-server/cloud/api/v1/../../etc/passwd").status_code == 404
        assert client.post("/api/hosts/8x4090-server/upload", json={"path": "relative", "name": "x"}).status_code == 400
    restored = CloudManager(tmp_path)
    assert len(restored.hosts()) == 3
    assert "token" not in (tmp_path / "hosts.json").read_text()
    restored.close()


def test_checkpoint_archive_manifest_and_symlink_boundary(tmp_path: Path) -> None:
    source = tmp_path / "checkpoint"
    source.mkdir()
    (source / "config.json").write_text("{}")
    (source / "weights.bin").write_bytes(b"abc")
    destination = tmp_path / "checkpoint.tar"
    manifest = checkpoint_archive(source, destination)
    assert manifest["weights.bin"]["size"] == 3
    assert manifest["weights.bin"]["sha256"] == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    with tarfile.open(destination) as archive:
        assert set(archive.getnames()) == {"config.json", "weights.bin"}


def test_checkpoint_link_rejected_without_reading_outside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "checkpoint"
    source.mkdir()
    (source / "config.json").write_text("{}")
    escape = source / "linked_weights.bin"
    escape.write_bytes(b"must not upload")
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == escape or original(path))
    with pytest.raises(ValueError, match="links"):
        checkpoint_archive(source, tmp_path / "checkpoint.tar")


def test_job_failures_redact_tokens(tmp_path: Path) -> None:
    manager = CloudManager(tmp_path)
    manager._tokens["8x4090-server"] = "sensitive-token"

    def fail() -> None:
        raise ValueError("failed sensitive-token")

    job = manager.submit("8x4090-server", "test", fail)
    deadline = time.monotonic() + 3
    while manager.jobs()[0]["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.jobs()[0]["id"] == job["id"]
    assert manager.jobs()[0]["status"] == "failed"
    assert "sensitive-token" not in json.dumps(manager.jobs())
    assert "sensitive-token" not in json.dumps(manager.hosts())
    manager.close()


@pytest.mark.parametrize("path", ["../health", "health?token=x", "https://evil/", "health/../../../secrets", "arbitrary/command"])
def test_proxy_limits_paths(path: str) -> None:
    assert not safe_proxy_path(path)


def test_manager_cloud_session_contract_and_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lerobot_monitor.cloud.app import create_app as create_cloud

    class Runtime:
        token = "server-token-kept-on-backend-123"

        def open_session(self, deployment: str, request: Any) -> dict[str, Any]:
            return {"id": "sess-1", "epoch": 0}

        def heartbeat(self, session: str, epoch: int) -> dict[str, Any]:
            return {"epoch": epoch, "status": "ok"}

        def infer(self, session: str, request: Any) -> dict[str, Any]:
            return {"request_id": request.request_id, "action": [1.0]}

        def reset(self, session: str, epoch: int) -> dict[str, Any]:
            return {"epoch": epoch + 1}

        def close_session(self, session: str) -> dict[str, Any]:
            return {"status": "closed"}

        def close(self) -> None:
            pass

    cloud = create_cloud(tmp_path / "cloud", runtime=Runtime())
    original_client = httpx.AsyncClient

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return original_client(transport=httpx.ASGITransport(app=cloud), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    manager = CloudManager(tmp_path / "manager", transport=FakeSSH())
    manager._tunnels["8x4090-server"] = (FakeProcess(), 18991)
    manager._tokens["8x4090-server"] = Runtime.token
    with TestClient(create_app(manager=manager), base_url="http://127.0.0.1:8095") as client:
        base = "/api/hosts/8x4090-server/cloud/api/v1"
        response = client.post(base + "/deployments/model-1/sessions", json={"mode": "select_action"})
        assert response.status_code == 201
        assert response.json()["id"] == "sess-1"
        assert client.post(base + "/sessions/sess-1/heartbeat", json={"epoch": 0}).status_code == 200
        response = client.post(base + "/sessions/sess-1/infer", json={"epoch": 0, "request_id": "r1", "state": {"j": 0}})
        assert response.status_code == 200
        assert response.json()["action"] == [1.0]
        assert client.post(base + "/sessions/sess-1/reset", json={"epoch": 0}).json()["epoch"] == 1
        assert client.delete(base + "/sessions/sess-1").status_code == 200
        assert client.post(base + "/shutdown").status_code == 404
        assert Runtime.token not in client.get("/api/hosts").text


def test_remote_upload_extraction_and_digest_verification(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.json").write_text("{}")
    (source / "weights.bin").write_bytes(b"original checkpoint")
    archive = tmp_path / "checkpoint.tar"
    manifest = checkpoint_archive(source, archive)
    remote = tmp_path / "remote" / "upload-1"
    with archive.open("rb") as stream:
        extracted = subprocess.run([sys.executable, "-c", EXTRACT_UPLOAD, str(remote)],
                                   stdin=stream, capture_output=True, timeout=10)
    assert extracted.returncode == 0, extracted.stderr.decode()
    verified = subprocess.run([sys.executable, "-c", VERIFY_UPLOAD, str(remote)],
                              input=json.dumps(manifest).encode(), capture_output=True, timeout=10)
    assert verified.returncode == 0, verified.stderr.decode()
    (remote / ".cloud-upload-manifest.json").unlink()
    (remote / "weights.bin").write_bytes(b"changed after transfer")
    corrupted = subprocess.run([sys.executable, "-c", VERIFY_UPLOAD, str(remote)],
                               input=json.dumps(manifest).encode(), capture_output=True, timeout=10)
    assert corrupted.returncode != 0


@pytest.mark.parametrize("member_name", ["../outside", "/absolute"])
def test_remote_upload_rejects_archive_escape(tmp_path: Path, member_name: str) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(member_name)
        info.size = 3
        archive.addfile(info, io.BytesIO(b"bad"))
    remote = tmp_path / "upload"
    result = subprocess.run([sys.executable, "-c", EXTRACT_UPLOAD, str(remote)],
                            input=buffer.getvalue(), capture_output=True, timeout=10)
    assert result.returncode != 0
    assert not (tmp_path / "outside").exists()


def test_runtime_rejects_unrelated_wheel_before_ssh(tmp_path: Path) -> None:
    import zipfile

    wheel = tmp_path / "other-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("other-1.0.dist-info/METADATA", "Name: other\nVersion: 1.0\n")
    transport = FakeSSH()
    manager = CloudManager(tmp_path / "state", transport=transport)
    with pytest.raises(ValueError, match="LeRobot wheel"):
        manager.prepare_runtime("8x4090-server", wheel, "smolvla")
    assert not transport.calls
    manager.close()


def test_management_job_keeps_connected_transport_visible(tmp_path: Path) -> None:
    manager = CloudManager(tmp_path, transport=FakeSSH())
    manager._tunnels["8x4090-server"] = (FakeProcess(), 18991)
    release = threading.Event()
    manager.submit("8x4090-server", "runtime", lambda: release.wait(5))
    try:
        row = manager.hosts()[0]
        assert row["status"] == "connected"
        assert row["operation_status"] == "busy"
    finally:
        release.set()
        manager.close()


def test_runtime_manifest_keeps_prior_profiles_and_migrates_legacy(tmp_path: Path) -> None:
    legacy = {"python": "/old/act/python", "profile": "act", "lerobot_wheel_sha256": "original"}
    (tmp_path / "runtime.json").write_text(json.dumps(legacy))
    for profile in ("smolvla", "pi"):
        payload = {"root": str(tmp_path), "runtime": f"/dedicated/{profile}", "profile": profile, "digest": profile}
        script = ("import json,os,pathlib,sys; config=json.load(sys.stdin); root=pathlib.Path(config['root']);"
                  "runtime=pathlib.Path(config['runtime']); python=runtime/'venv/bin/python'\n") + WRITE_RUNTIME_MANIFEST
        result = subprocess.run([sys.executable, "-c", script], input=json.dumps(payload).encode(),
                                capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr.decode()
    manifest = json.loads((tmp_path / "runtime.json").read_text())
    assert set(manifest["profiles"]) == {"act", "smolvla", "pi"}
    assert manifest["profiles"]["act"] == legacy
    assert manifest["profiles"]["smolvla"]["python"].replace("\\", "/") == "/dedicated/smolvla/venv/bin/python"
    assert manifest["default_profile"] == "pi"


@pytest.mark.parametrize("stage", [EXTRACT_UPLOAD, VERIFY_UPLOAD])
def test_upload_failure_cleans_exact_staging_before_registration(tmp_path: Path, stage: str) -> None:
    class FailingSSH(FakeSSH):
        def run(self, alias: str, arguments: list[str], **kwargs: Any) -> str:
            result = super().run(alias, arguments, **kwargs)
            if arguments[2] == stage:
                raise TransportError("simulated preparation failure")
            return result

    source = tmp_path / "checkpoint"
    source.mkdir()
    (source / "config.json").write_text("{}")
    ssh = FailingSSH()
    manager = CloudManager(tmp_path / "state", transport=ssh)
    manager._tunnels["8x4090-server"] = (FakeProcess(), 18991)
    manager._tokens["8x4090-server"] = "secret-backend-only"
    with pytest.raises(TransportError, match="preparation failure"):
        manager.upload("8x4090-server", source, "model")
    cleanup = ssh.calls[-1][1]
    assert cleanup[2] == CLEANUP_UPLOAD
    assert cleanup[3] == "/data/zhuyutian/lerobot-monitor"
    assert cleanup[4].startswith(cleanup[3] + "/uploads/")
    assert len(cleanup[4].split("/")[-1]) == 32
    manager.close()


@pytest.mark.parametrize("failure", ["timeout", "server", "rejected"])
def test_upload_registration_preserves_ambiguous_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    class RegistrationClient(HealthyClient):
        def post(self, url: str, **kwargs: Any) -> httpx.Response:
            if failure == "timeout":
                raise httpx.ReadTimeout("server may already have registered")
            return httpx.Response(503 if failure == "server" else 422,
                                  json={"detail": "failed"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "Client", RegistrationClient)
    source = tmp_path / "checkpoint"
    source.mkdir()
    (source / "config.json").write_text("{}")
    ssh = FakeSSH()
    manager = CloudManager(tmp_path / "state", transport=ssh)
    manager._tunnels["8x4090-server"] = (FakeProcess(), 18991)
    manager._tokens["8x4090-server"] = "secret-backend-only"
    with pytest.raises(ValueError) as result:
        manager.upload("8x4090-server", source, "model")
    scripts = [call[1][2] for call in ssh.calls]
    if failure == "rejected":
        assert CLEANUP_UPLOAD in scripts
        assert "staging removed" in str(result.value)
    else:
        assert CLEANUP_UPLOAD not in scripts
        assert "outcome is unknown" in str(result.value)
        assert "/data/zhuyutian/lerobot-monitor/uploads/" in str(result.value)
    manager.close()


def test_cleanup_script_refuses_non_owned_paths(tmp_path: Path) -> None:
    root = tmp_path / "owned-root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("must survive")
    result = subprocess.run([sys.executable, "-c", CLEANUP_UPLOAD, str(root), str(outside)],
                            capture_output=True, timeout=10)
    assert result.returncode != 0
    assert (outside / "keep.txt").read_text() == "must survive"
    exact = root / "uploads" / ("a" * 32)
    exact.mkdir(parents=True)
    (exact / "weights.bin").write_bytes(b"partial")
    result = subprocess.run([sys.executable, "-c", CLEANUP_UPLOAD, str(root), str(exact)],
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    assert not exact.exists()


@pytest.mark.parametrize("profile,extras", [("act", "dataset"), ("smolvla", "dataset,smolvla"), ("pi", "dataset,pi")])
def test_runtime_requirements_include_native_inference_import_dependencies(tmp_path: Path, profile: str, extras: str) -> None:
    namespace = {"config": {"profile": profile, "wheel": "/packages/lerobot.whl"},
                 "installation": {"wheel": "/packages/monitor.whl"}, "runtime": tmp_path}
    exec(RUNTIME_REQUIREMENTS, namespace)
    lines = (tmp_path / "requirements.in").read_text().splitlines()
    assert lines[0] == f"/packages/lerobot.whl[{extras}]"
    assert "/packages/monitor.whl[cloud]" in lines
    assert "torch==2.11.0+cu128" in lines
    assert ("transformers==5.5.4" in lines) == (profile != "act")
