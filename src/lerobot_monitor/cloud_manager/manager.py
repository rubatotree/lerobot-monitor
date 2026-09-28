"""Persistent host definitions, background jobs and owned SSH tunnels."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from email.parser import BytesParser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import httpx
from pydantic import BaseModel, Field, field_validator

from .artifacts import BOOTSTRAP, CLEANUP_UPLOAD, EXTRACT_UPLOAD, PREPARE_RUNTIME, STORE_WHEEL, VERIFY_UPLOAD, build_wheel, checkpoint_archive
from .transport import SSHTransport, TransportError


class HostInput(BaseModel):
    alias: str
    root: str
    port: int = Field(default=8091, ge=1024, le=65535)
    python: str = "python3.12"
    runtime_python: str | None = None

    @field_validator("alias")
    @classmethod
    def validate_alias(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
            raise ValueError("Use an existing SSH alias containing letters, digits, dots, underscores or hyphens")
        return value

    @field_validator("root")
    @classmethod
    def validate_root(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or len(path.parts) < 4 or ".." in path.parts or "\x00" in value or "\n" in value:
            raise ValueError("Use an absolute dedicated server directory at least three levels below /")
        return str(path)

    @field_validator("python", "runtime_python")
    @classmethod
    def validate_interpreter(cls, value: str | None) -> str | None:
        if value is not None and (not value or value.startswith("-") or "\x00" in value or "\n" in value):
            raise ValueError("Invalid Python interpreter")
        return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class CloudManager:
    def __init__(self, state_dir: Path, *, transport: SSHTransport | None = None,
                 project: Path | None = None) -> None:
        self.state_dir = state_dir
        self.transport = transport or SSHTransport()
        self.project = project
        self._lock = threading.RLock()
        self._host_locks: dict[str, threading.RLock] = {}
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cloud-manager")
        self._tunnels: dict[str, tuple[subprocess.Popen[bytes], int]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._errors: dict[str, str] = {}
        self._busy: set[str] = set()
        self._closed = False
        self._hosts: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, str] = {}
        hosts_path = state_dir / "hosts.json"
        if hosts_path.exists():
            for row in json.loads(hosts_path.read_text(encoding="utf-8")):
                validated = HostInput.model_validate(row)
                self._hosts[row["id"]] = {"id": row["id"], **validated.model_dump()}
        else:
            for alias, root in (("8x4090-server", "/data/zhuyutian/lerobot-monitor"),
                                ("8A6000-server", "/data2/zhuyutian/lerobot-monitor")):
                self._hosts[alias] = {"id": alias, **HostInput(alias=alias, root=root).model_dump()}
        tokens_path = state_dir / "credentials.json"
        if tokens_path.exists():
            self._tokens = json.loads(tokens_path.read_text(encoding="utf-8"))

    def _host_lock(self, identifier: str) -> threading.RLock:
        with self._lock:
            return self._host_locks.setdefault(identifier, threading.RLock())

    def host(self, identifier: str) -> dict[str, Any]:
        with self._lock:
            if identifier not in self._hosts:
                raise KeyError("Unknown host")
            return dict(self._hosts[identifier])

    def hosts(self) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for identifier, row in self._hosts.items():
                tunnel = self._tunnels.get(identifier)
                connected = tunnel is not None and tunnel[0].poll() is None
                result.append({**row, "status": "connected" if connected else "disconnected",
                               "operation_status": "busy" if identifier in self._busy else "idle",
                               "error": self._errors.get(identifier)})
            return result

    def add_host(self, value: HostInput) -> dict[str, Any]:
        with self._lock:
            if any(row["alias"] == value.alias for row in self._hosts.values()):
                raise ValueError("This SSH alias is already registered")
            identifier = uuid.uuid4().hex
            row = {"id": identifier, **value.model_dump()}
            self._hosts[identifier] = row
            atomic_json(self.state_dir / "hosts.json", list(self._hosts.values()))
            return {**row, "status": "disconnected", "error": None}

    def probe(self, identifier: str) -> dict[str, Any]:
        row = self.host(identifier)
        script = ("import json,os,pathlib,shutil,sys; p=pathlib.Path(sys.argv[1]); "
                  "a=next((x for x in [p,*p.parents] if x.exists()),pathlib.Path('/')); "
                  "print(json.dumps({'python':sys.version.split()[0], "
                  "'uv':bool(shutil.which('uv') or (pathlib.Path.home()/'.local/bin/uv').is_file()), "
                  "'root_exists':p.exists(),'free_bytes':shutil.disk_usage(a).free,"
                  "'writable':os.access(a,os.W_OK),'runtime_configured':(p/'runtime.json').is_file()}))")
        result = json.loads(self.transport.run(row["alias"], [row["python"], "-c", script, row["root"]]))
        with self._lock:
            self._errors.pop(identifier, None)
        return result

    def disconnect(self, identifier: str) -> dict[str, str]:
        self.host(identifier)
        with self._host_lock(identifier):
            with self._lock:
                tunnel = self._tunnels.pop(identifier, None)
            if tunnel:
                self.transport.stop(tunnel[0])
        return {"status": "disconnected"}

    def connect(self, identifier: str) -> dict[str, Any]:
        row = self.host(identifier)
        with self._host_lock(identifier):
            if self._closed:
                raise RuntimeError("Manager is shutting down")
            self.disconnect(identifier)
            # Read through SSH; token never occurs in subprocess arguments or API output.
            token = self.transport.run(row["alias"], [row["python"], "-c",
                "import pathlib,sys; print((pathlib.Path(sys.argv[1])/'token').read_text().strip())",
                row["root"]]).strip()
            if len(token) < 20 or len(token) > 256 or any(char.isspace() for char in token):
                raise TransportError("Cloud token is missing or invalid; initialize the server first")
            process, port = self.transport.tunnel(row["alias"], row["port"])
            try:
                with httpx.Client(timeout=10, trust_env=False) as client:
                    response = client.get(f"http://127.0.0.1:{port}/api/v1/health",
                                          headers={"Authorization": f"Bearer {token}"})
                    response.raise_for_status()
                    health = response.json()
            except Exception as exc:
                self.transport.stop(process)
                raise TransportError("Cloud health check failed; verify service status and token") from exc
            with self._lock:
                if self._closed:
                    self.transport.stop(process)
                    raise RuntimeError("Manager is shutting down")
                self._tunnels[identifier] = (process, port)
                self._tokens[identifier] = token
                atomic_json(self.state_dir / "credentials.json", self._tokens)
                self._errors.pop(identifier, None)
            return {"status": "connected", "health": health}

    def endpoint(self, identifier: str) -> tuple[str, str]:
        self.host(identifier)
        with self._lock:
            tunnel = self._tunnels.get(identifier)
            if tunnel is None or tunnel[0].poll() is not None:
                raise TransportError("Host is disconnected; connect it first")
            return f"http://127.0.0.1:{tunnel[1]}", self._tokens[identifier]

    def submit(self, identifier: str, kind: str, action: Callable[[], Any]) -> dict[str, Any]:
        self.host(identifier)
        with self._lock:
            if self._closed:
                raise RuntimeError("Manager is shutting down")
            if identifier in self._busy:
                raise ValueError("This host already has a management job in progress")
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "host_id": identifier, "kind": kind, "status": "queued",
                   "created_at": time.time(), "updated_at": time.time(), "message": "Waiting", "error": None}
            self._jobs[job_id] = job
            self._busy.add(identifier)
            self._pool.submit(self._run_job, identifier, job_id, action)
            return dict(job)

    def _run_job(self, identifier: str, job_id: str, action: Callable[[], Any]) -> None:
        with self._lock:
            self._jobs[job_id].update(status="running", message="Running", updated_at=time.time())
        try:
            result = action()
            with self._lock:
                self._jobs[job_id].update(status="succeeded", message="Completed", result=result,
                                          updated_at=time.time())
        except Exception as exc:
            # Only our own safe exceptions are surfaced. Third-party errors may embed headers.
            message = str(exc) if isinstance(exc, (ValueError, TransportError)) else f"{type(exc).__name__}: operation failed"
            with self._lock:
                for token in self._tokens.values():
                    message = message.replace(token, "[redacted]")
                self._jobs[job_id].update(status="failed", error=message, message=message, updated_at=time.time())
                self._errors[identifier] = message
        finally:
            with self._lock:
                self._busy.discard(identifier)

    def jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(job) for job in reversed(list(self._jobs.values()))]

    def bootstrap(self, identifier: str, *, upgrade: bool = False) -> dict[str, Any]:
        row = self.host(identifier)
        with self._host_lock(identifier), tempfile.TemporaryDirectory(prefix="lerobot-cloud-wheel-") as temporary:
            wheel = build_wheel(Path(temporary), self.project)
            with wheel.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                stream.seek(0)
                remote_wheel = f"{row['root']}/packages/{digest}/{wheel.name}"
                self.transport.run(row["alias"], [row["python"], "-c", STORE_WHEEL, remote_wheel, digest],
                                   data=stream, timeout=180)
            settings = {**row, "wheel": remote_wheel, "digest": digest, "token": secrets.token_urlsafe(32),
                        "upgrade": upgrade}
            output = self.transport.run(row["alias"], [row["python"], "-c", BOOTSTRAP],
                                        data=json.dumps(settings).encode(), timeout=900)
            result = json.loads(output)
            if result.get("error"):
                raise ValueError(result["error"])
            return self.connect(identifier)

    def upload(self, identifier: str, source: Path, name: str) -> dict[str, Any]:
        row = self.host(identifier)
        self.endpoint(identifier)
        with self._host_lock(identifier), tempfile.TemporaryDirectory(prefix="lerobot-checkpoint-") as temporary:
            archive = Path(temporary) / "checkpoint.tar"
            manifest = checkpoint_archive(source, archive)
            remote = f"{row['root']}/uploads/{uuid.uuid4().hex}"
            try:
                with archive.open("rb") as stream:
                    self.transport.run(row["alias"], [row["python"], "-c", EXTRACT_UPLOAD, remote],
                                       data=stream, timeout=3600)
                self.transport.run(row["alias"], [row["python"], "-c", VERIFY_UPLOAD, remote],
                                   data=json.dumps(manifest).encode(), timeout=600)
                url, token = self.endpoint(identifier)
            except Exception:
                self._cleanup_upload(row, remote)
                raise
            # Once POST has started, a timeout or 5xx may hide a successful registration.
            # Preserve that directory rather than racing the service moving it into assets.
            recovery = (f"Registration outcome is unknown; upload preserved at {remote}. "
                        "Check cloud deployments for that source before retrying or removing it.")
            try:
                with httpx.Client(timeout=30, trust_env=False) as client:
                    response = client.post(url + "/api/v1/deployments",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"name": name, "source_kind": "path", "source": remote, "owned_upload": True})
            except httpx.HTTPError as exc:
                raise ValueError(recovery) from exc
            if 400 <= response.status_code < 500:
                self._cleanup_upload(row, remote)
                raise ValueError(f"Cloud rejected upload registration (HTTP {response.status_code}); staging removed")
            if not response.is_success:
                raise ValueError(recovery)
            try:
                return response.json()
            except ValueError as exc:
                raise ValueError(recovery) from exc

    def _cleanup_upload(self, row: dict[str, Any], remote: str) -> None:
        try:
            self.transport.run(row["alias"], [row["python"], "-c", CLEANUP_UPLOAD, row["root"], remote], timeout=30)
        except Exception as exc:
            raise ValueError(f"Upload failed and staging cleanup failed; inspect owned recovery directory {remote}") from exc

    def close(self) -> None:
        with self._lock:
            self._closed = True
            identifiers = list(self._tunnels)
        for identifier in identifiers:
            self.disconnect(identifier)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def prepare_runtime(self, identifier: str, wheel: Path, profile: str,
                        huggingface_home: str | None = None) -> dict[str, Any]:
        row = self.host(identifier)
        if profile not in {"act", "smolvla", "pi"}:
            raise ValueError("Unsupported runtime profile")
        if not wheel.is_absolute() or not wheel.is_file() or not re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", wheel.name):
            raise ValueError("Provide an absolute path to a LeRobot wheel")
        with zipfile.ZipFile(wheel) as package:
            metadata = [name for name in package.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata) != 1 or BytesParser().parsebytes(package.read(metadata[0])).get("Name", "").lower() != "lerobot":
                raise ValueError("Runtime artifact must be a LeRobot wheel")
        if huggingface_home:
            HostInput.validate_root(huggingface_home)
        with self._host_lock(identifier), wheel.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            stream.seek(0)
            remote_wheel = f"{row['root']}/packages/{digest}/{wheel.name}"
            self.transport.run(row["alias"], [row["python"], "-c", STORE_WHEEL, remote_wheel, digest],
                               data=stream, timeout=300)
            payload = {"root": row["root"], "wheel": remote_wheel, "digest": digest,
                       "profile": profile, "huggingface_home": huggingface_home}
            output = self.transport.run(row["alias"], [row["python"], "-c", PREPARE_RUNTIME],
                                       data=json.dumps(payload).encode(), timeout=5400)
            result = json.loads(output)
            if result.get("error"):
                raise ValueError(result["error"])
            return result
