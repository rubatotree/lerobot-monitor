"""Monitor-side SSH transport and leased remote inference engines."""

from __future__ import annotations

import base64
import json
import logging
import math
import threading
import time
import traceback
import uuid
from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

from .cloud_manager.manager import CloudManager
from .policy import ActionChunk, LoadedPolicy

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CloudTarget:
    host_id: str
    deployment_id: str
    gpu_uuid: str = ""

    @property
    def uri(self) -> str:
        base = f"cloud://{quote(self.host_id, safe='')}/{quote(self.deployment_id, safe='')}"
        return f"{base}?gpu={quote(self.gpu_uuid, safe='')}" if self.gpu_uuid else base


@dataclass(frozen=True)
class RemoteInferenceSettings:
    type: str
    queue_threshold: int = 30


def remote_inference_settings(extra: Mapping[str, str]) -> RemoteInferenceSettings:
    kind = str(extra.get("inference.type", "sync")).strip().lower()
    if kind not in {"sync", "rtc"}:
        raise ValueError(f"unsupported remote inference type '{kind}'")
    try:
        threshold = int(str(extra.get("inference.queue_threshold", "30")))
    except ValueError as exc:
        raise ValueError("inference.queue_threshold must be an integer") from exc
    if threshold < 0:
        raise ValueError("inference.queue_threshold must be non-negative")
    return RemoteInferenceSettings(kind, threshold)


def parse_cloud_uri(value: str) -> CloudTarget | None:
    parsed = urlparse(str(value))
    if parsed.scheme != "cloud":
        return None
    host_id = unquote(parsed.netloc).strip()
    deployment_id = unquote(parsed.path.lstrip("/")).strip()
    gpu_uuid = unquote((parse_qs(parsed.query).get("gpu") or [""])[0]).strip()
    if not host_id or not deployment_id or "/" in deployment_id:
        raise ValueError("invalid cloud model address")
    return CloudTarget(host_id, deployment_id, gpu_uuid)


class CloudRequestError(RuntimeError):
    """A cloud API call failed; carries the upstream status when one exists."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def _image_shape(value: Any) -> str:
    """Describe a camera value without importing numpy at module scope."""

    if value is None:
        return "no frame"
    shape = getattr(value, "shape", None)
    return f"shape {tuple(shape)}" if shape is not None else f"type {type(value).__name__}"


def _is_camera_frame(value: Any) -> bool:
    """A camera frame is a 3-channel image array; scalars and states are not."""
    shape = getattr(value, "shape", None)
    return shape is not None and len(shape) == 3 and shape[2] == 3


def _encode_images(observation: Mapping[str, Any], *, camera_names: Sequence[str] = ()) -> dict[str, str]:
    """Encode camera frames as PNG.

    Only 3-channel image arrays are encoded; scalar observations (joints, state)
    are skipped silently, as they always were. A name listed in ``camera_names``
    that yields no usable frame is an error naming that camera, instead of a
    silently imageless request the cloud rejects as "missing required images".
    """
    import cv2
    import numpy as np

    required = {str(name) for name in camera_names}
    images: dict[str, str] = {}
    for key, value in observation.items():
        name = str(key)
        if name.startswith(("_", "observation.")):
            continue
        array = np.asarray(value)
        if array.ndim != 3 or array.shape[2] != 3:
            if name in required:
                raise CloudRequestError(
                    f"camera {name!r} has no usable RGB frame ({_image_shape(value)}); "
                    "check that it is enabled and receives frames"
                )
            continue
        # CameraHub exposes RGB. OpenCV writes BGR input into a standards-compliant PNG.
        ok, encoded = cv2.imencode(".png", np.ascontiguousarray(array[:, :, ::-1]))
        if not ok:
            raise CloudRequestError(f"could not encode camera {name}")
        images[name] = base64.b64encode(encoded.tobytes()).decode("ascii")
    if required:
        missing = [name for name in required if name not in images]
        if missing:
            raise CloudRequestError(
                f"no usable frame for policy camera(s): {sorted(missing)}; "
                "check that each camera is enabled and receives frames"
            )
    return images


class _CloudStage:
    """A completed inference phase, mirroring the native profiler's stage shape."""

    __slots__ = ("name", "start", "end", "gpu_ms")

    def __init__(self, name: str, start: float, end: float, gpu_ms: float | None = None) -> None:
        self.name = name
        self.start = start
        self.end = end
        self.gpu_ms = gpu_ms


class RemoteSession:
    def __init__(
        self,
        owner: MonitorCloudClient,
        target: CloudTarget,
        mode: str,
        task: str,
        state_keys: list[str],
        overrides: Mapping[str, str],
        camera_names: Sequence[str] = (),
    ) -> None:
        self.owner = owner
        self.target = target
        self.mode = mode
        self.task = task
        self.overrides = dict(overrides)
        self.camera_names = [str(name) for name in camera_names]
        # Cloud inference is one synchronous round trip, so its phases are reported
        # as stages the same way the local profiler reports them. Set by the caller
        # (the rollout worker) to the currently charted chunk.
        self.stages: list[_CloudStage] = []
        result = owner.request(
            target.host_id,
            "POST",
            f"/api/v1/deployments/{quote(target.deployment_id, safe='')}/sessions",
            json={"mode": mode, "task": task, "state_keys": state_keys, "overrides": dict(overrides)},
            timeout=180,
        )
        self.session_id = str(result["session_id"])
        self.epoch = int(result.get("epoch", 0))
        self._epoch_lock = threading.Lock()
        self._reset_lock = threading.Lock()
        self.action_keys = [str(key) for key in result.get("action_keys") or []]
        self.metadata = result
        self._closed = threading.Event()
        self._heartbeat_seconds = max(1.0, float(result.get("heartbeat_seconds", 5)))
        self._heartbeat_error: BaseException | None = None
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name=f"cloud-heartbeat-{self.session_id[:8]}",
            daemon=True,
        )
        self._heartbeat.start()
        self.owner._session_opened(target)

    def _heartbeat_loop(self) -> None:
        while not self._closed.wait(self._heartbeat_seconds):
            with self._epoch_lock:
                epoch = self.epoch
            try:
                self.owner.request(
                    self.target.host_id,
                    "POST",
                    f"/api/v1/sessions/{self.session_id}/heartbeat",
                    json={"epoch": epoch},
                    timeout=15,
                )
            except BaseException as exc:  # surfaced on the next inference call
                with self._epoch_lock:
                    if epoch != self.epoch:
                        continue
                self._heartbeat_error = exc
                return

    def infer(
        self,
        observation: Mapping[str, Any],
        *,
        chunk_size: int = 32,
        prefix_raw: list[list[float]] | None = None,
        prefix_absolute: list[list[float]] | None = None,
        inference_delay: int = 0,
    ) -> dict[str, Any]:
        if self._closed.is_set():
            raise CloudRequestError("remote inference session is closed")
        if self._heartbeat_error is not None:
            raise CloudRequestError(f"remote inference heartbeat failed: {self._heartbeat_error}")
        stages: list[_CloudStage] = []
        self.stages = stages

        started = time.perf_counter()
        state = {
            str(key): float(value)
            for key, value in observation.items()
            if not str(key).startswith("_") and not hasattr(value, "shape")
        }
        with self._epoch_lock:
            epoch = self.epoch
        # Encoding PNG payloads is local work; it must not be mistaken for network time.
        encoded_at = time.perf_counter()
        images = _encode_images(observation, camera_names=self.camera_names)
        encoded_end = time.perf_counter()
        stages.append(_CloudStage("cloud_encode", encoded_at, encoded_end))
        payload: dict[str, Any] = {
            "epoch": epoch,
            "request_id": uuid.uuid4().hex,
            "state": state,
            "images": images,
            "task": self.task,
            "chunk_size": int(chunk_size),
            "inference_delay": max(0, int(inference_delay)),
        }
        if prefix_raw is not None and prefix_absolute is not None:
            payload["prefix_raw"] = prefix_raw
            payload["prefix_absolute"] = prefix_absolute
        sent_at = time.perf_counter()
        result = self.owner.request(
            self.target.host_id,
            "POST",
            f"/api/v1/sessions/{self.session_id}/infer",
            json=payload,
            timeout=180,
        )
        received_at = time.perf_counter()
        # The worker reports its own GPU compute window, so the remainder of the
        # round trip is transfer plus server-side queueing.
        compute_s = float(result.get("compute_seconds") or 0.0)
        compute_s = compute_s if math.isfinite(compute_s) and compute_s > 0.0 else 0.0
        round_trip = received_at - sent_at
        compute_s = min(compute_s, round_trip)
        transfer = max(0.0, round_trip - compute_s)
        # Split the non-compute time across the request and response legs; the exact
        # boundary is invisible from here, so attribute it by payload proportion.
        request_bytes = len(json.dumps(payload))
        response_bytes = len(json.dumps(result))
        total_bytes = max(1, request_bytes + response_bytes)
        upload = transfer * (request_bytes / total_bytes)
        stages.append(_CloudStage("cloud_upload", sent_at, sent_at + upload))
        stages.append(
            _CloudStage("cloud_compute", sent_at + upload, sent_at + upload + compute_s, compute_s * 1000.0)
        )
        stages.append(_CloudStage("cloud_download", sent_at + upload + compute_s, received_at))
        return result

    def reset(self) -> None:
        with self._reset_lock:
            with self._epoch_lock:
                epoch = self.epoch
            result = self.owner.request(
                self.target.host_id,
                "POST",
                f"/api/v1/sessions/{self.session_id}/reset",
                json={"epoch": epoch},
                timeout=30,
            )
            with self._epoch_lock:
                self.epoch = int(result["epoch"])

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self.owner.request(
                self.target.host_id,
                "DELETE",
                f"/api/v1/sessions/{self.session_id}",
                timeout=30,
            )
        finally:
            if self._heartbeat is not threading.current_thread():
                self._heartbeat.join(timeout=2)
            self.owner._session_closed(self.target)


class _RemotePolicyDescriptor:
    """Human-readable policy identity used by existing rollout status text."""

    def reset(self) -> None:
        pass


class MonitorCloudClient:
    def __init__(self, state_dir: Path, *, project: Path | None = None, manager: CloudManager | None = None) -> None:
        self.manager = manager or CloudManager(state_dir, project=project)
        self._connect_locks: dict[str, threading.Lock] = {}
        self._lock = threading.RLock()
        self._deployments: dict[tuple[str, str], dict[str, Any]] = {}
        self._active_sessions: dict[tuple[str, str], int] = {}
        self._gpu_by_deployment: dict[tuple[str, str], str] = {}
        self._loading_since: dict[tuple[str, str], float] = {}
        self._refreshing: set[str] = set()
        self._refreshed_at: dict[str, float] = {}
        self._local_changes: dict[str, int] = {}

    def _remembered_gpu(self, target: CloudTarget) -> str:
        """Reuse the GPU this deployment was last loaded on, if we know it."""
        with self._lock:
            remembered = self._gpu_by_deployment.get((target.host_id, target.deployment_id), "")
            if remembered:
                return remembered
            row = self._deployments.get((target.host_id, target.deployment_id)) or {}
        return str(row.get("gpu_uuid") or "")

    def _remember_gpu(self, target: CloudTarget, gpu_uuid: str) -> None:
        if gpu_uuid:
            with self._lock:
                self._gpu_by_deployment[(target.host_id, target.deployment_id)] = gpu_uuid

    def hosts(self) -> list[dict[str, Any]]:
        return self.manager.hosts()

    def connect(self, host_id: str) -> dict[str, Any]:
        lock = self._connect_locks.setdefault(host_id, threading.Lock())
        with lock:
            try:
                self.manager.endpoint(host_id)
            except Exception:
                self.manager.connect(host_id)
        return self.catalog(host_id)

    def request(
        self,
        host_id: str,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        timeout: float = 30,
    ) -> Any:
        self.connect_endpoint(host_id)
        url, token = self.manager.endpoint(host_id)
        try:
            response = httpx.request(
                method,
                url + path,
                headers={"Authorization": f"Bearer {token}"},
                json=json,
                params=params,
                timeout=timeout,
                trust_env=False,
            )
        except httpx.HTTPError as exc:
            raise CloudRequestError(f"cloud request failed: {exc}") from exc
        if not response.is_success:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            raise CloudRequestError(
                str(detail or f"cloud returned HTTP {response.status_code}"),
                response.status_code,
            )
        return response.json()

    def connect_endpoint(self, host_id: str) -> None:
        try:
            self.manager.endpoint(host_id)
        except Exception:
            lock = self._connect_locks.setdefault(host_id, threading.Lock())
            with lock:
                try:
                    self.manager.endpoint(host_id)
                except Exception:
                    self.manager.connect(host_id)

    def catalog(self, host_id: str) -> dict[str, Any]:
        self.connect_endpoint(host_id)
        deployments = self.request(host_id, "GET", "/api/v1/deployments")
        with self._lock:
            for row in deployments:
                self._deployments[(host_id, str(row.get("id")))] = dict(row)
        return {
            "host": next(row for row in self.hosts() if row["id"] == host_id),
            "gpus": self.request(host_id, "GET", "/api/v1/gpus"),
            "deployments": deployments,
        }

    def deployment(self, target: CloudTarget) -> dict[str, Any]:
        rows = self.request(target.host_id, "GET", "/api/v1/deployments")
        row = next((item for item in rows if str(item.get("id")) == target.deployment_id), None)
        if row is None:
            raise CloudRequestError("cloud deployment no longer exists")
        with self._lock:
            self._deployments[(target.host_id, target.deployment_id)] = dict(row)
        return row

    def _session_opened(self, target: CloudTarget) -> None:
        key = (target.host_id, target.deployment_id)
        with self._lock:
            self._active_sessions[key] = self._active_sessions.get(key, 0) + 1

    def _session_closed(self, target: CloudTarget) -> None:
        key = (target.host_id, target.deployment_id)
        with self._lock:
            count = self._active_sessions.get(key, 0) - 1
            if count > 0:
                self._active_sessions[key] = count
            else:
                self._active_sessions.pop(key, None)

    def _refresh_host_async(self, host_id: str, *, interval: float) -> None:
        """Keep cached deployment states current without blocking a status push.

        Loads finish on the cloud host, so the cache written at submit time goes
        stale. Only connected hosts are polled: a status push must never open an
        SSH tunnel.
        """
        now = time.monotonic()
        with self._lock:
            if host_id in self._refreshing or now - self._refreshed_at.get(host_id, -interval) < interval:
                return
            self._refreshing.add(host_id)
            self._refreshed_at[host_id] = now
            generation = self._local_changes.get(host_id, 0)

        def refresh() -> None:
            try:
                self.manager.endpoint(host_id)
                rows = self.request(host_id, "GET", "/api/v1/deployments", timeout=5)
                with self._lock:
                    if self._local_changes.get(host_id, 0) != generation:
                        return  # a load was submitted while this answer was in flight
                    for row in rows:
                        self._deployments[(host_id, str(row.get("id")))] = dict(row)
            except Exception:  # noqa: BLE001 - a disconnected host keeps its last known state
                pass
            finally:
                with self._lock:
                    self._refreshing.discard(host_id)

        threading.Thread(target=refresh, name=f"cloud-refresh-{host_id}", daemon=True).start()

    def residency(self, uri: str) -> dict[str, Any]:
        target = parse_cloud_uri(uri)
        if target is None:
            raise ValueError("not a cloud model address")
        key = (target.host_id, target.deployment_id)
        with self._lock:
            row = dict(self._deployments.get(key) or {})
            active = self._active_sessions.get(key, 0)
        status = row.get("status")
        transitioning = status in {"loading", "unloading"}
        self._refresh_host_async(target.host_id, interval=1.0 if transitioning else 3.0)
        with self._lock:
            if status == "loading":
                started = self._loading_since.setdefault(key, time.monotonic())
            else:
                self._loading_since.pop(key, None)
                started = None
        loaded = status == "loaded" and bool(row.get("gpu_uuid"))
        state = (
            "in_use" if active and loaded
            else "ready" if loaded
            else "loading" if status == "loading"
            else "unloading" if status == "unloading"
            else "error" if status == "error" and row.get("error")
            else "unloaded"
        )
        instances = []
        if loaded or state in {"loading", "unloading"}:
            instance: dict[str, Any] = {
                "id": f"cloud:{target.host_id}:{target.deployment_id}",
                "device": str(row.get("gpu_uuid")),
                "state": state,
                "gpu_bytes": 0,
                "can_unload": loaded and not active,
                "can_release": active > 0,
                "can_cancel_load": False,
                "overrides": {},
                "remote": True,
            }
            if started is not None:
                instance["elapsed_ms"] = (time.monotonic() - started) * 1000.0
            instances.append(instance)
        elif state == "error":
            instances.append(
                {
                    "id": f"cloud:{target.host_id}:{target.deployment_id}",
                    "device": "cloud",
                    "state": "error",
                    "error": str(row.get("error")),
                    "gpu_bytes": 0,
                    "can_unload": False,
                    "can_release": False,
                    "can_cancel_load": False,
                    "overrides": {},
                    "remote": True,
                }
            )
        return {
            "state": state,
            "instances": instances,
            "gpu_bytes": 0,
            "can_unload": loaded and not active,
            "can_release": active > 0,
            "can_cancel_load": False,
            "source_paths": [uri],
        }

    def submit_load(
        self,
        target: CloudTarget,
        *,
        gpu_uuid: str | None = None,
    ) -> dict[str, Any]:
        """Request a deployment load and return immediately with its job id.

        Callers that must wait for usable weights use :meth:`ensure_loaded`; the
        API uses this so a slow load never holds an HTTP request open.
        """
        row = self.deployment(target)
        if row.get("status") == "loaded":
            if gpu_uuid and row.get("gpu_uuid") != gpu_uuid:
                raise CloudRequestError("cloud deployment is loaded on a different GPU")
            self._remember_gpu(target, str(row.get("gpu_uuid") or gpu_uuid or ""))
            return {"status": "loaded", "deployment": row}
        if row.get("status") not in {"ready", "error"}:
            raise CloudRequestError(f"cloud deployment is not ready ({row.get('status')})")
        selected_gpu = str(gpu_uuid or "").strip() or self._remembered_gpu(target)
        if not selected_gpu:
            raise CloudRequestError(
                "this cloud model is not loaded; load it from the Library and select a GPU first"
            )
        self._remember_gpu(target, selected_gpu)
        job = self.request(
            target.host_id,
            "POST",
            f"/api/v1/deployments/{quote(target.deployment_id, safe='')}/load",
            json={"gpu_uuid": selected_gpu, "device": "cuda"},
            timeout=30,
        )
        # Show the load immediately; the background refresh reports completion.
        key = (target.host_id, target.deployment_id)
        with self._lock:
            self._deployments[key] = {**row, "status": "loading", "gpu_uuid": selected_gpu, "error": None}
            self._loading_since[key] = time.monotonic()
            self._refreshed_at.pop(target.host_id, None)
            self._local_changes[target.host_id] = self._local_changes.get(target.host_id, 0) + 1
        return {"status": "loading", "job_id": str(job["job_id"]), "gpu_uuid": selected_gpu}

    def wait_for_load(self, target: CloudTarget, job_id: str, *, timeout: float = 900) -> dict[str, Any]:
        """Poll a previously submitted load job until it succeeds or fails."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            jobs = self.request(target.host_id, "GET", "/api/v1/jobs", timeout=30)
            current = next((item for item in jobs if str(item.get("id")) == job_id), None)
            if current and current.get("status") == "succeeded":
                return self.deployment(target)
            if current and current.get("status") == "failed":
                raise CloudRequestError(str(current.get("error") or "cloud model load failed"))
            time.sleep(0.25)
        raise CloudRequestError("cloud model load timed out")

    def ensure_loaded(
        self,
        target: CloudTarget,
        *,
        gpu_uuid: str | None = None,
        timeout: float = 900,
    ) -> dict[str, Any]:
        """Block until the deployment is loaded; used by rollout and debug paths."""
        submitted = self.submit_load(target, gpu_uuid=gpu_uuid)
        if submitted["status"] == "loaded":
            return submitted["deployment"]
        return self.wait_for_load(target, submitted["job_id"], timeout=timeout)

    def unload(self, target: CloudTarget, *, timeout: float = 180) -> None:
        row = self.deployment(target)
        if row.get("status") != "loaded":
            return
        job = self.request(
            target.host_id,
            "POST",
            f"/api/v1/deployments/{quote(target.deployment_id, safe='')}/unload",
            timeout=30,
        )
        job_id = str(job["job_id"])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = next(
                (item for item in self.request(target.host_id, "GET", "/api/v1/jobs") if str(item.get("id")) == job_id),
                None,
            )
            if current and current.get("status") == "succeeded":
                self.deployment(target)
                return
            if current and current.get("status") == "failed":
                raise CloudRequestError(str(current.get("error") or "cloud model unload failed"))
            time.sleep(0.25)
        raise CloudRequestError("cloud model unload timed out")

    def open_session(
        self,
        target: CloudTarget,
        *,
        mode: str,
        task: str,
        state_keys: list[str],
        overrides: Mapping[str, str],
        camera_names: Sequence[str] = (),
    ) -> RemoteSession:
        row = self.ensure_loaded(target)
        runtime_target = CloudTarget(
            target.host_id,
            target.deployment_id,
            str(row.get("gpu_uuid") or ""),
        )
        if not runtime_target.gpu_uuid:
            raise CloudRequestError("cloud deployment does not report its loaded GPU")
        allowed_policy = {
            "policy.n_action_steps",
            "policy.num_steps",
            "policy.num_inference_steps",
            "policy.temporal_ensemble_coeff",
            "policy.use_amp",
        }
        safe_overrides = {
            str(key): str(value)
            for key, value in overrides.items()
            if key in allowed_policy or (mode == "rtc_chunk" and str(key).startswith("inference.rtc."))
        }
        return RemoteSession(self, runtime_target, mode, task, state_keys, safe_overrides, camera_names)

    def load_policy(
        self,
        uri: str,
        *,
        task: str,
        state_keys: list[str],
    ) -> LoadedPolicy:
        target = parse_cloud_uri(uri)
        if target is None:
            raise ValueError("not a cloud model address")
        row = self.ensure_loaded(target)
        action_keys = [str(key) for key in (row.get("metadata") or {}).get("action_keys") or state_keys]
        return LoadedPolicy(
            path=uri,
            device="remote",
            task=task,
            policy=_RemotePolicyDescriptor(),
            preprocessor=lambda value: value,
            postprocessor=lambda value: value,
            dataset_features={},
            ordered_action_keys=action_keys,
        )

    def debug_infer(
        self,
        uri: str,
        *,
        task: str,
        state_keys: list[str],
        extra: Mapping[str, str],
        joints: Mapping[str, float],
        images_rgb: Mapping[str, Any],
        chunk_size: int,
    ) -> ActionChunk:
        target = parse_cloud_uri(uri)
        if target is None:
            raise ValueError("not a cloud model address")
        started = time.perf_counter()
        session = self.open_session(
            target,
            mode="debug_chunk",
            task=task,
            state_keys=state_keys,
            overrides=extra,
            camera_names=sorted(str(name) for name in images_rgb),
        )
        wait_ms = (time.perf_counter() - started) * 1000
        try:
            compute_started = time.perf_counter()
            result = session.infer({**joints, **images_rgb}, chunk_size=chunk_size)
            actions = _poses(result, joints)
            generated = result.get("generated_steps")
            return ActionChunk(
                actions=actions,
                strategy="cloud_policy_chunk",
                degraded=False,
                warnings=[],
                cache_hit=True,
                model_wait_ms=wait_ms,
                model_load_ms=wait_ms,
                compute_ms=(time.perf_counter() - compute_started) * 1000,
                generated_steps=(
                    int(generated) if isinstance(generated, int) and generated > 0 else None
                ),
            )
        finally:
            session.close()

    def close(self) -> None:
        self.manager.close()


def _poses(result: Mapping[str, Any], fallback: Mapping[str, float]) -> list[dict[str, float]]:
    keys = [str(key) for key in result.get("action_keys") or []]
    poses: list[dict[str, float]] = []
    for row in result.get("actions") or []:
        pose = {str(key): float(value) for key, value in fallback.items()}
        pose.update(
            {
                (key[:-4] if key.endswith(".pos") else key): float(row[index])
                for index, key in enumerate(keys)
                if index < len(row)
            }
        )
        poses.append(pose)
    return poses


class RemoteSyncInferenceEngine:
    def __init__(self, session: RemoteSession) -> None:
        self.session = session
        self.failed = False
        self.failure_traceback: str | None = None
        self.dispatched_chunk_id: int | None = None
        self.dispatched_action_index: int | None = None
        # Mirrors the native profiler's observer contract so the rollout worker can
        # record cloud transfer and compute phases on the same chart.
        self.stage_observer: Any | None = None
        self._sequence = 0
        # Actions the cloud policy still queues after the last request; the chart
        # draws them as the dashed prediction a local sync rollout shows.
        self._queued: list[dict[str, float]] = []

    def leftover_poses(self, fallback: Mapping[str, float]) -> list[dict[str, float]]:
        return [{**fallback, **pose} for pose in self._queued]

    def start(self) -> None:
        pass

    def resume(self) -> None:
        pass

    def pause(self) -> None:
        pass

    def reset(self) -> None:
        self.session.reset()

    def notify_observation(self, observation: dict[str, Any]) -> None:
        pass

    def _publish_stages(self) -> None:
        observer = self.stage_observer
        if observer is None:
            return
        for stage in self.session.stages:
            try:
                observer(stage)
            except Exception:  # noqa: BLE001 - telemetry must not affect control
                logger.debug("cloud stage observer failed", exc_info=True)

    def get_action(self, observation: dict[str, Any] | None) -> dict[str, float] | None:
        if observation is None:
            return None
        try:
            result = self.session.infer(observation, chunk_size=1)
        finally:
            self._publish_stages()
        poses = _poses(result, {})
        # Older cloud services do not report the queue; the preview is then empty.
        self._queued = _poses({**result, "actions": result.get("queued_actions") or []}, {})
        self._sequence += 1
        self.dispatched_chunk_id = self._sequence
        self.dispatched_action_index = 0
        return poses[0] if poses else None

    def stop(self) -> bool:
        self.session.close()
        return True

    def wait_stopped(self, timeout: float | None = None) -> bool:
        return True


@dataclass(frozen=True)
class RemoteChunkEvent:
    """Cloud producer lifecycle; mirrors LeRobot's RTCChunkEvent for the chart adapter.

    ``actions`` holds joint-space poses (the cloud reports named action columns), not
    tensors, and ``stage`` carries one completed transfer/compute phase for ``chunk_id``.
    """

    kind: str
    chunk_id: int
    steps: int = 0
    actions: tuple[dict[str, float], ...] = ()
    merge: _RemoteMergeReceipt | None = None
    action_index: int | None = None
    stage: Any = None


@dataclass(frozen=True)
class _RemoteMergeReceipt:
    """The LeRobot ``QueueMergeReceipt`` subset the chart adapter reads."""

    prefix_trimmed: int
    accepted_steps: int
    replaced: tuple[tuple[int, int], ...]
    timestamp: float


class RemoteRTCInferenceEngine:
    def __init__(
        self,
        session: RemoteSession,
        *,
        fps: float,
        queue_threshold: int,
    ) -> None:
        self.session = session
        self.fps = max(1.0, float(fps))
        self.queue_threshold = max(0, int(queue_threshold))
        self.observation_provider: Any | None = None
        self.chunk_observer: Any | None = None
        self.failed = False
        self.failure_traceback: str | None = None
        self.dispatched_chunk_id: int | None = None
        self.dispatched_action_index: int | None = None
        self._sequence = 0
        self._last_delay = 0
        self._queue: deque[tuple[list[float], dict[str, float], int, int]] = deque()
        self._lock = threading.Lock()
        self._observation: dict[str, Any] | None = None
        self._active = threading.Event()
        self._stop = threading.Event()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self.action_queue = self

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("remote RTC engine is already running")
        self._stop.clear()
        self._stopped.clear()
        self._thread = threading.Thread(target=self._run, name="cloud-rtc-inference", daemon=True)
        self._thread.start()

    def resume(self) -> None:
        self._active.set()

    def pause(self) -> None:
        self._active.clear()

    def reset(self) -> None:
        with self._lock:
            self._queue.clear()
            self._observation = None
        self.session.reset()

    def notify_observation(self, observation: dict[str, Any]) -> None:
        with self._lock:
            self._observation = dict(observation)

    def get_action(self, observation: dict[str, Any] | None) -> dict[str, float] | None:
        with self._lock:
            if not self._queue:
                return None
            _raw, action, chunk_id, index = self._queue.popleft()
        self.dispatched_chunk_id = chunk_id
        self.dispatched_action_index = index
        self._emit(RemoteChunkEvent("consumed", chunk_id, action_index=index))
        return dict(action)

    def qsize(self) -> int:
        with self._lock:
            return len(self._queue)

    def get_action_index(self) -> int:
        return int(self.dispatched_action_index or 0)

    def get_processed_left_over(self) -> list[dict[str, float]]:
        with self._lock:
            return [dict(item[1]) for item in self._queue]

    def leftover_poses(self, fallback: Mapping[str, float]) -> list[dict[str, float]]:
        return [{**fallback, **pose} for pose in self.get_processed_left_over()]

    def _emit(self, event: RemoteChunkEvent) -> None:
        observer = self.chunk_observer
        if observer is None:
            return
        try:
            observer(event)
        except Exception:
            # Chart telemetry must never affect control.
            logger.debug("remote RTC chunk observer failed", exc_info=True)

    def _publish_stages(self, chunk_id: int) -> None:
        """Forward this request's transfer/compute phases to the chunk they produced."""
        if self.chunk_observer is None:
            return
        for stage in self.session.stages:
            self._emit(RemoteChunkEvent("stage", chunk_id, stage=stage))

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if not self._active.wait(0.05):
                    continue
                with self._lock:
                    observation = None if self._observation is None else dict(self._observation)
                    prefix = list(self._queue)
                if observation is None or len(prefix) > self.queue_threshold:
                    self._stop.wait(0.01)
                    continue
                self._sequence += 1
                chunk_id = self._sequence
                if self.observation_provider is not None:
                    observation = self.observation_provider(observation)
                raw_prefix = [list(item[0]) for item in prefix] or None
                absolute_prefix = [
                    [float(item[1].get(key[:-4] if key.endswith(".pos") else key, 0.0)) for key in self.session.action_keys]
                    for item in prefix
                ] or None
                started = time.perf_counter()
                conditioned_delay = min(self._last_delay, len(prefix))
                if self.session.overrides.get("inference.rtc.mode") == "trained":
                    conditioned_delay = min(
                        conditioned_delay,
                        int(self.session.metadata.get("rtc_training_max_delay") or 0),
                    )
                self._emit(RemoteChunkEvent("started", chunk_id))
                try:
                    result = self.session.infer(
                        observation,
                        prefix_raw=raw_prefix,
                        prefix_absolute=absolute_prefix,
                        inference_delay=conditioned_delay,
                    )
                except BaseException:
                    self._emit(RemoteChunkEvent("failed", chunk_id))
                    raise
                finally:
                    self._publish_stages(chunk_id)
                delay = math.ceil((time.perf_counter() - started) * self.fps) if prefix else 0
                self._last_delay = delay
                raw_rows = result.get("raw_actions") or []
                action_rows = result.get("actions") or []
                keys = [str(key) for key in result.get("action_keys") or self.session.action_keys]
                trimmed = min(delay, len(raw_rows), len(action_rows))
                # The server reported the whole horizon; the executed part follows as "accepted".
                self._emit(RemoteChunkEvent("ready", chunk_id, len(action_rows)))
                replacement: deque[tuple[list[float], dict[str, float], int, int]] = deque()
                accepted_poses: list[dict[str, float]] = []
                for index, (raw, action) in enumerate(zip(raw_rows[trimmed:], action_rows[trimmed:])):
                    pose = {
                        (key[:-4] if key.endswith(".pos") else key): float(action[pos])
                        for pos, key in enumerate(keys)
                        if pos < len(action)
                    }
                    accepted_poses.append(pose)
                    replacement.append(([float(value) for value in raw], pose, chunk_id, index + trimmed))
                # Steps the consumer could not drain during inference stay in the old
                # chunk when the queue is replaced, exactly as LeRobot's merge reports
                # them: counted under the lock that swaps the queue.
                accepted_at = time.perf_counter()
                with self._lock:
                    replaced = tuple(sorted(Counter(item[2] for item in self._queue).items()))
                    self._queue = replacement
                self._emit(
                    RemoteChunkEvent(
                        "accepted", chunk_id, len(replacement), tuple(accepted_poses),
                        _RemoteMergeReceipt(trimmed, len(replacement), replaced, accepted_at),
                    )
                )
        except BaseException:  # the control loop surfaces this traceback
            self.failed = True
            self.failure_traceback = traceback.format_exc()
        finally:
            try:
                self.session.close()
            finally:
                self._stopped.set()

    def stop(self) -> bool:
        self._stop.set()
        self._active.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3)
        return thread is None or not thread.is_alive()

    def wait_stopped(self, timeout: float | None = None) -> bool:
        return self._stopped.wait(timeout)
