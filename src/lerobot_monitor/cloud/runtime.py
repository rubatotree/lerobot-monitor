"""Persistent deployments and exclusive, leased access to isolated model workers."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .gpu import probe_gpus
from .schemas import DeploymentCreate, InferRequest, LoadRequest, SessionOpen
from .worker import SubprocessWorker


class CloudError(RuntimeError):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Session:
    id: str
    deployment_id: str
    epoch: int
    expires_at: float
    mode: str
    inflight: bool = False
    closing: bool = False
    requests: set[str] = field(default_factory=set)


def read_token(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / "token"
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        token = target.read_text(encoding="utf-8").strip()
        if len(token) < 32 or any(character.isspace() for character in token):
            raise ValueError("root/token must contain a token of at least 32 non-whitespace characters")
        if os.name != "nt":
            target.chmod(0o600)
        return token
    token = secrets.token_urlsafe(48)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(token)
    return token


class CloudRuntime:
    """All state transitions are serialized; expensive work runs outside the lock."""

    def __init__(self, root: Path, *, gpu_probe: Callable[[], list[dict[str, Any]]] = probe_gpus,
                 worker_factory: Callable[..., Any] = SubprocessWorker, lease_seconds: float = 30,
                 idle_seconds: float = 900, request_timeout: float = 120,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.root = root.expanduser().resolve()
        self.token = read_token(self.root)
        for name in ("assets", "staging", "uploads", "logs"):
            (self.root / name).mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(self.root / "state.sqlite3", check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        for table in ("deployments", "jobs"):
            self.connection.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.connection.commit()
        self.deployments = self._read_table("deployments")
        self.jobs = self._read_table("jobs")
        self.workers: dict[str, Any] = {}
        self.sessions: dict[str, Session] = {}
        self.owners: dict[str, str] = {}
        self.last_used: dict[str, float] = {}
        self.busy: set[str] = set()
        self.reserved: dict[str, str] = {}
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.idle_seconds = idle_seconds
        self.request_timeout = request_timeout
        self.gpu_probe = gpu_probe
        self.worker_factory = worker_factory
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cloud-job")
        self.slots = threading.BoundedSemaphore(32)
        self.stop_event = threading.Event()
        self._closed = False
        for row in self.deployments.values():
            if row["status"] in {"loaded", "loading", "unloading"}:
                row.update(status="ready", gpu_uuid=None, error="Service restarted; model is unloaded")
            elif row["status"] in {"deploying", "deleting"}:
                row.update(status="error", gpu_uuid=None, error="Operation interrupted by service restart")
            self._save("deployments", row)
        for job in self.jobs.values():
            if job["status"] in {"queued", "running"}:
                job.update(status="failed", error="Service restarted before operation completed", updated_at=time.time())
                self._save("jobs", job)
        self.thread = threading.Thread(target=self._maintenance, name="cloud-maintenance", daemon=True)
        self.thread.start()

    def _read_table(self, table: str) -> dict[str, dict[str, Any]]:
        return {key: json.loads(data) for key, data in self.connection.execute(f"SELECT id,data FROM {table}")}

    def _save(self, table: str, row: dict[str, Any]) -> None:
        self.connection.execute(f"INSERT OR REPLACE INTO {table}(id,data) VALUES (?,?)", (row["id"], json.dumps(row)))
        self.connection.commit()

    def clean_error(self, value: object) -> str:
        return str(value).replace(self.token, "[redacted]")[:2000]

    def _deployment(self, deployment_id: str) -> dict[str, Any]:
        row = self.deployments.get(deployment_id)
        if row is None:
            raise CloudError("deployment not found", 404)
        return row

    def list_deployments(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(row) for row in self.deployments.values()]

    def list_jobs(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(row) for row in sorted(self.jobs.values(), key=lambda item: item["created_at"], reverse=True)[:200]]

    def gpus(self) -> list[dict[str, Any]]:
        inventory = self.gpu_probe()
        with self.lock:
            return [{**row, "deployment_id": self.reserved.get(row["uuid"])} for row in inventory]

    def runtime_profile(self) -> dict[str, Any]:
        target = self.root / "runtime.json"
        if not target.exists():
            return {"configured": False, "profile": None}
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
            profiles = value.get("profiles") or {value.get("profile", "default"): value}
            available = {key: {"configured": bool(row.get("python") and Path(row["python"]).is_file()),
                               "profile": row.get("profile")} for key, row in profiles.items()}
            return {"configured": any(row["configured"] for row in available.values()), "profiles": available,
                    "profile": value.get("profile"), "python": value.get("python"),
                    "lerobot_wheel_sha256": value.get("lerobot_wheel_sha256")}
        except (ValueError, OSError, TypeError):
            return {"configured": False, "profile": None, "error": "Invalid runtime manifest"}

    def health(self) -> dict[str, Any]:
        with self.lock:
            return {"status": "ok", "api_version": 1, "version": "0.1.0", "active_sessions": len(self.sessions),
                    "loaded_models": len(self.workers), "runtime": self.runtime_profile(),
                    "capabilities": ["deploy", "gpu_assignment", "http_sessions", "idle_unload"]}

    def _submit(self, deployment_id: str, kind: str, operation: Callable[[], None]) -> dict[str, Any]:
        # Caller holds lock, so claiming a job and its deployment is atomic.
        if self._closed or deployment_id in self.busy:
            raise CloudError("deployment already has an operation in progress")
        if not self.slots.acquire(blocking=False):
            raise CloudError("job queue is full", 429)
        self.busy.add(deployment_id)
        job = {"id": uuid.uuid4().hex, "deployment_id": deployment_id, "kind": kind, "status": "queued",
               "created_at": time.time(), "updated_at": time.time(), "error": None}
        self.jobs[job["id"]] = job
        self._save("jobs", job)

        def execute() -> None:
            try:
                with self.lock:
                    job.update(status="running", updated_at=time.time())
                    self._save("jobs", job)
                operation()
                with self.lock:
                    job.update(status="succeeded", updated_at=time.time())
            except Exception as exc:
                with self.lock:
                    error = self.clean_error(exc)
                    job.update(status="failed", error=error, updated_at=time.time())
                    row = self.deployments.get(deployment_id)
                    if row is not None:
                        row.update(status="loaded" if deployment_id in self.workers else "error", error=error)
                        self._save("deployments", row)
            finally:
                with self.lock:
                    self.busy.discard(deployment_id)
                    self._save("jobs", job)
                self.slots.release()

        self.executor.submit(execute)
        return {"job_id": job["id"], "status": "queued", "deployment_id": deployment_id}

    @staticmethod
    def validate_model(path: Path) -> None:
        if not path.is_dir() or not (path / "config.json").is_file():
            raise ValueError("model directory must contain config.json")
        if (path / "config.json").stat().st_size > 1024 * 1024:
            raise ValueError("model config exceeds 1 MiB")
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
        if not isinstance(config, dict) or not isinstance(config.get("type"), str) or not config["type"]:
            raise ValueError("model config must contain a LeRobot policy type")

    def create_deployment(self, request: DeploymentCreate) -> dict[str, Any]:
        if request.owned_upload and request.source_kind != "path":
            raise CloudError("owned_upload is only valid for uploaded paths", 422)
        source_path: Path | None = None
        if request.source_kind == "path":
            source_path = Path(request.source).expanduser().resolve()
            try:
                self.validate_model(source_path)
                if not request.owned_upload and any(source_path.is_relative_to(self.root / name)
                                                     for name in ("assets", "uploads", "staging")):
                    raise ValueError("external model paths cannot reference managed assets, uploads or staging")
                if request.owned_upload:
                    if source_path.parent != self.root / "uploads" or Path(request.source).is_symlink():
                        raise ValueError("owned upload must be a direct child of root/uploads")
                    if any(path.is_symlink() or (not path.is_file() and not path.is_dir()) for path in source_path.rglob("*")):
                        raise ValueError("owned uploads cannot contain symbolic links or special files")
            except (OSError, ValueError) as exc:
                raise CloudError(str(exc), 422) from exc
        with self.lock:
            deployment_id = uuid.uuid4().hex
            row = {"id": deployment_id, "name": request.name, "source_kind": request.source_kind,
                   "source": request.source, "revision": request.revision, "status": "deploying", "gpu_uuid": None,
                   "error": None, "path": None, "managed": request.source_kind == "huggingface" or request.owned_upload,
                   "created_at": time.time(), "metadata": None}
            self.deployments[deployment_id] = row
            self._save("deployments", row)

            def deploy() -> None:
                staging = self.root / "staging" / deployment_id
                destination = self.root / "assets" / deployment_id
                try:
                    if request.source_kind == "huggingface":
                        from huggingface_hub import HfApi, snapshot_download
                        revision = HfApi().model_info(request.source, revision=request.revision).sha
                        snapshot_download(request.source, revision=revision, local_dir=staging)
                        self.validate_model(staging)
                        staging.rename(destination)
                        path = destination
                    elif request.owned_upload:
                        assert source_path is not None
                        source_path.rename(destination)
                        path, revision = destination, None
                    else:
                        assert source_path is not None
                        path, revision = source_path, request.revision
                    with self.lock:
                        row.update(status="ready", path=str(path), revision=revision, error=None)
                        self._save("deployments", row)
                finally:
                    if staging.exists():
                        self._delete_managed(staging, self.root / "staging")

            try:
                job = self._submit(deployment_id, "deploy", deploy)
            except Exception:
                self.deployments.pop(deployment_id)
                self.connection.execute("DELETE FROM deployments WHERE id=?", (deployment_id,))
                self.connection.commit()
                raise
            return {**row, "job_id": job["job_id"]}

    def load(self, deployment_id: str, request: LoadRequest) -> dict[str, Any]:
        inventory = self.gpu_probe() if request.device == "cuda" else []
        with self.lock:
            row = self._deployment(deployment_id)
            if deployment_id in self.workers:
                raise CloudError("model is already loaded")
            if not row.get("path") or row["status"] not in {"ready", "error"}:
                raise CloudError("model has not completed deployment")
            if request.device == "cuda":
                gpu = next((gpu for gpu in inventory if gpu["uuid"] == request.gpu_uuid), None)
                if gpu is None or not gpu.get("healthy"):
                    raise CloudError("selected GPU is unavailable or unhealthy")
                if gpu.get("busy", True) or request.gpu_uuid in self.reserved:
                    raise CloudError("selected GPU is in use")
            elif request.gpu_uuid != "cpu":
                raise CloudError("CPU device requires gpu_uuid='cpu'", 422)

            def load_worker() -> None:
                worker: Any = None
                try:
                    settings: dict[str, Any] = {}
                    manifest = self.root / "runtime.json"
                    if manifest.exists():
                        profile = json.loads(manifest.read_text(encoding="utf-8"))
                        profiles = profile.get("profiles")
                        if profiles and not os.environ.get("LEROBOT_CLOUD_RUNTIME_PYTHON"):
                            config = json.loads((Path(row["path"]) / "config.json").read_text(encoding="utf-8"))
                            policy_type = config["type"]
                            preferred = "pi" if policy_type in {"pi", "pi0", "pi05", "pi0_fast"} else policy_type
                            candidates = [preferred] if preferred != "act" else ["act", "smolvla", "pi"]
                            selected = next((name for name in candidates if name in profiles), None)
                            if selected is None:
                                raise ValueError(f"No compatible runtime profile for {policy_type}; prepare the {preferred} runtime")
                            profile = profiles[selected]
                        settings = {"python": profile.get("python"), "hf_home": profile.get("huggingface_home")}
                    worker = self.worker_factory(row["path"], request.gpu_uuid, request.device,
                        self.root / "logs" / f"{deployment_id}.log", **settings)
                    with self.lock:
                        if self._closed:
                            raise CloudError("service is shutting down")
                        self.workers[deployment_id] = worker
                        self.last_used[deployment_id] = self.clock()
                        row.update(status="loaded", error=None, metadata=worker.metadata)
                        self._save("deployments", row)
                except BaseException:
                    if worker is not None:
                        worker.stop()
                    with self.lock:
                        if self.reserved.get(request.gpu_uuid) == deployment_id:
                            self.reserved.pop(request.gpu_uuid)
                        row["gpu_uuid"] = None
                    raise

            result = self._submit(deployment_id, "load", load_worker)
            if request.device == "cuda":
                self.reserved[request.gpu_uuid] = deployment_id
            row.update(status="loading", gpu_uuid=request.gpu_uuid, error=None)
            self._save("deployments", row)
            return result

    def _stop_worker(self, deployment_id: str, *, error: str | None = None) -> None:
        with self.lock:
            worker = self.workers.pop(deployment_id, None)
            # Reservation stays until process termination is confirmed.
            row = self._deployment(deployment_id)
        if worker is not None:
            worker.stop()
        with self.lock:
            gpu = row.get("gpu_uuid")
            if self.reserved.get(gpu) == deployment_id:
                self.reserved.pop(gpu)
            self.last_used.pop(deployment_id, None)
            row.update(status="error" if error else "ready", gpu_uuid=None, error=error)
            self._save("deployments", row)

    def unload(self, deployment_id: str) -> dict[str, Any]:
        with self.lock:
            row = self._deployment(deployment_id)
            if deployment_id in self.owners:
                raise CloudError("model has an active session")
            result = self._submit(deployment_id, "unload", lambda: self._stop_worker(deployment_id))
            row["status"] = "unloading"
            self._save("deployments", row)
            return result

    def _delete_managed(self, path: Path, parent: Path) -> None:
        resolved = path.resolve()
        if path.is_symlink() or resolved.parent != parent.resolve() or resolved == parent.resolve():
            raise ValueError("refusing deletion outside managed directory")
        shutil.rmtree(resolved)

    def delete(self, deployment_id: str, delete_files: bool) -> dict[str, Any]:
        with self.lock:
            row = self._deployment(deployment_id)
            if deployment_id in self.owners:
                raise CloudError("model has an active session")
            if delete_files and not row["managed"]:
                raise CloudError("externally registered directories cannot be deleted", 422)

            def remove() -> None:
                self._stop_worker(deployment_id)
                if delete_files and row.get("path"):
                    target = Path(row["path"])
                    if target.name != deployment_id:
                        raise ValueError("managed directory identity mismatch")
                    with self.lock:
                        if any(other["id"] != deployment_id and other.get("path") == str(target)
                               for other in self.deployments.values()):
                            raise ValueError("model files are referenced by another deployment")
                    if target.exists():
                        self._delete_managed(target, self.root / "assets")
                with self.lock:
                    self.deployments.pop(deployment_id)
                    self.connection.execute("DELETE FROM deployments WHERE id=?", (deployment_id,))
                    self.connection.commit()

            result = self._submit(deployment_id, "delete", remove)
            row["status"] = "deleting"
            self._save("deployments", row)
            return result

    def logs(self, deployment_id: str) -> dict[str, Any]:
        with self.lock:
            self._deployment(deployment_id)
        path = self.root / "logs" / f"{deployment_id}.log"
        if not path.exists():
            return {"text": "", "lines": []}
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 65536))
            text = stream.read(65536).decode("utf-8", errors="replace").replace(self.token, "[redacted]")
        return {"text": text, "lines": text.splitlines()[-300:]}

    def _session(self, session_id: str, epoch: int | None = None) -> Session:
        session = self.sessions.get(session_id)
        if session is None or session.closing or session.expires_at <= self.clock():
            raise CloudError("session is closed or expired", 410)
        if epoch is not None and session.epoch != epoch:
            raise CloudError("session epoch mismatch")
        return session

    def open_session(self, deployment_id: str, request: SessionOpen) -> dict[str, Any]:
        with self.lock:
            self._deployment(deployment_id)
            if deployment_id in self.busy or deployment_id in self.owners:
                raise CloudError("model is busy or already has an exclusive session")
            worker = self.workers.get(deployment_id)
            if worker is None or not worker.alive():
                raise CloudError("model is not loaded")
            session = Session(uuid.uuid4().hex, deployment_id, 0, self.clock() + self.lease_seconds,
                              request.mode, inflight=True)
            self.sessions[session.id] = session
            self.owners[deployment_id] = session.id
        try:
            metadata = worker.call("open", request.model_dump(), timeout=self.request_timeout)
            with self.lock:
                self._session(session.id)
                session.inflight = False
                return {"id": session.id, "session_id": session.id, "epoch": 0,
                        "lease_seconds": self.lease_seconds, "heartbeat_seconds": 5, **metadata}
        except Exception as exc:
            self.close_session(session.id)
            raise CloudError(self.clean_error(exc), 422) from exc

    def heartbeat(self, session_id: str, epoch: int) -> dict[str, Any]:
        with self.lock:
            session = self._session(session_id, epoch)
            session.expires_at = self.clock() + self.lease_seconds
            return {"epoch": session.epoch, "lease_seconds": self.lease_seconds}

    def infer(self, session_id: str, request: InferRequest) -> dict[str, Any]:
        with self.lock:
            session = self._session(session_id, request.epoch)
            if session.inflight:
                raise CloudError("session already has an in-flight request")
            if request.request_id in session.requests:
                raise CloudError("request_id already used; ambiguous retries are rejected")
            if len(session.requests) >= 100000:
                raise CloudError("session request limit reached; reset session")
            if session.mode != "rtc_chunk" and request.prefix_raw is not None:
                raise CloudError("prefixes require an rtc_chunk session", 422)
            session.requests.add(request.request_id)
            session.inflight = True
            worker = self.workers[session.deployment_id]
        try:
            result = worker.call("infer", request.model_dump(), timeout=self.request_timeout)
            with self.lock:
                self._session(session_id, request.epoch)
                return {**result, "epoch": session.epoch, "request_id": request.request_id}
        except CloudError:
            raise
        except Exception as exc:
            self.close_session(session_id)
            raise CloudError(self.clean_error(exc), 502) from exc
        finally:
            with self.lock:
                session.inflight = False

    def reset(self, session_id: str, epoch: int) -> dict[str, Any]:
        with self.lock:
            session = self._session(session_id, epoch)
            if session.inflight:
                raise CloudError("cannot reset an in-flight session")
            session.inflight = True
            worker = self.workers[session.deployment_id]
        try:
            worker.call("reset", {}, timeout=self.request_timeout)
            with self.lock:
                self._session(session_id, epoch)
                session.epoch += 1
                session.requests.clear()
                session.expires_at = self.clock() + self.lease_seconds
                return {"epoch": session.epoch}
        except Exception as exc:
            self.close_session(session_id)
            raise CloudError(self.clean_error(exc), 502) from exc
        finally:
            with self.lock:
                session.inflight = False

    def close_session(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None or session.closing:
                return {"status": "closed"}
            session.closing = True
            session.epoch += 1
            worker = self.workers.get(session.deployment_id)
        try:
            if worker is not None:
                if session.inflight:
                    self._stop_worker(session.deployment_id, error="In-flight session cancelled; model unloaded")
                else:
                    try:
                        worker.call("close", {}, timeout=min(self.request_timeout, 5))
                    except Exception:
                        self._stop_worker(session.deployment_id, error="Worker failed while closing session")
        finally:
            with self.lock:
                self.sessions.pop(session_id, None)
                self.owners.pop(session.deployment_id, None)
                self.last_used[session.deployment_id] = self.clock()
        return {"status": "closed"}

    def tick(self) -> None:
        with self.lock:
            expired = [session.id for session in self.sessions.values() if session.expires_at <= self.clock()]
            dead = [key for key, worker in self.workers.items() if not worker.alive() and key not in self.busy]
        for session_id in expired:
            self.close_session(session_id)
        for deployment_id in dead:
            with self.lock:
                owner = self.owners.get(deployment_id)
            if owner:
                self.close_session(owner)
            with self.lock:
                if deployment_id in self.workers and deployment_id not in self.busy and deployment_id not in self.owners:
                    self._submit(deployment_id, "worker_exit", lambda key=deployment_id: self._stop_worker(key, error="Model worker exited"))
        with self.lock:
            for deployment_id in list(self.workers):
                if (deployment_id not in self.owners and deployment_id not in self.busy
                        and self.clock() - self.last_used.get(deployment_id, self.clock()) >= self.idle_seconds):
                    self.unload(deployment_id)

    def _maintenance(self) -> None:
        while not self.stop_event.wait(1):
            try:
                self.tick()
            except Exception:
                # One failing child must not stop expiry of other sessions.
                continue

    def close(self) -> None:
        with self.lock:
            if self._closed:
                return
            self._closed = True
        self.stop_event.set()
        self.thread.join(timeout=10)
        for session_id in list(self.sessions):
            self.close_session(session_id)
        for deployment_id in list(self.workers):
            self._stop_worker(deployment_id)
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.connection.close()
