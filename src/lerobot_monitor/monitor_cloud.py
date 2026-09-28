"""Monitor-side SSH transport and leased remote inference engines."""

from __future__ import annotations

import base64
import math
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

from .cloud_manager.manager import CloudManager
from .policy import ActionChunk, LoadedPolicy


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


def _encode_images(observation: Mapping[str, Any]) -> dict[str, str]:
    import cv2
    import numpy as np

    images: dict[str, str] = {}
    for key, value in observation.items():
        array = np.asarray(value)
        if array.ndim != 3 or array.shape[2] != 3:
            continue
        # CameraHub exposes RGB. OpenCV writes BGR input into a standards-compliant PNG.
        ok, encoded = cv2.imencode(".png", np.ascontiguousarray(array[:, :, ::-1]))
        if not ok:
            raise RuntimeError(f"could not encode camera {key}")
        images[str(key)] = base64.b64encode(encoded.tobytes()).decode("ascii")
    return images


class CloudRequestError(RuntimeError):
    pass


class RemoteSession:
    def __init__(
        self,
        owner: MonitorCloudClient,
        target: CloudTarget,
        mode: str,
        task: str,
        state_keys: list[str],
        overrides: Mapping[str, str],
    ) -> None:
        self.owner = owner
        self.target = target
        self.mode = mode
        self.task = task
        self.overrides = dict(overrides)
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
        state = {
            str(key): float(value)
            for key, value in observation.items()
            if not str(key).startswith("_") and not hasattr(value, "shape")
        }
        with self._epoch_lock:
            epoch = self.epoch
        payload: dict[str, Any] = {
            "epoch": epoch,
            "request_id": uuid.uuid4().hex,
            "state": state,
            "images": _encode_images(observation),
            "task": self.task,
            "chunk_size": int(chunk_size),
            "inference_delay": max(0, int(inference_delay)),
        }
        if prefix_raw is not None and prefix_absolute is not None:
            payload["prefix_raw"] = prefix_raw
            payload["prefix_absolute"] = prefix_absolute
        return self.owner.request(
            self.target.host_id,
            "POST",
            f"/api/v1/sessions/{self.session_id}/infer",
            json=payload,
            timeout=180,
        )

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
            raise CloudRequestError(str(detail or f"cloud returned HTTP {response.status_code}"))
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

    def residency(self, uri: str) -> dict[str, Any]:
        target = parse_cloud_uri(uri)
        if target is None:
            raise ValueError("not a cloud model address")
        key = (target.host_id, target.deployment_id)
        with self._lock:
            row = dict(self._deployments.get(key) or {})
            active = self._active_sessions.get(key, 0)
        loaded = row.get("status") == "loaded" and bool(row.get("gpu_uuid"))
        state = "in_use" if active else "ready" if loaded else "unloaded"
        instances = []
        if loaded:
            instances.append(
                {
                    "id": f"cloud:{target.host_id}:{target.deployment_id}",
                    "device": str(row.get("gpu_uuid")),
                    "state": state,
                    "gpu_bytes": 0,
                    "can_unload": not active,
                    "can_release": active > 0,
                    "can_cancel_load": False,
                    "overrides": {},
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

    def ensure_loaded(
        self,
        target: CloudTarget,
        *,
        gpu_uuid: str | None = None,
        timeout: float = 900,
    ) -> dict[str, Any]:
        row = self.deployment(target)
        if row.get("status") == "loaded":
            if gpu_uuid and row.get("gpu_uuid") != gpu_uuid:
                raise CloudRequestError("cloud deployment is loaded on a different GPU")
            return row
        if row.get("status") not in {"ready", "error"}:
            raise CloudRequestError(f"cloud deployment is not ready ({row.get('status')})")
        selected_gpu = str(gpu_uuid or "").strip()
        if not selected_gpu:
            raise CloudRequestError("choose a cloud GPU before loading this model")
        job = self.request(
            target.host_id,
            "POST",
            f"/api/v1/deployments/{quote(target.deployment_id, safe='')}/load",
            json={"gpu_uuid": selected_gpu, "device": "cuda"},
            timeout=30,
        )
        job_id = str(job["job_id"])
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
        return RemoteSession(self, runtime_target, mode, task, state_keys, safe_overrides)

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
        )
        wait_ms = (time.perf_counter() - started) * 1000
        try:
            compute_started = time.perf_counter()
            result = session.infer({**joints, **images_rgb}, chunk_size=chunk_size)
            actions = _poses(result, joints)
            return ActionChunk(
                actions=actions,
                strategy="cloud_policy_chunk",
                degraded=False,
                warnings=[],
                cache_hit=True,
                model_wait_ms=wait_ms,
                model_load_ms=wait_ms,
                compute_ms=(time.perf_counter() - compute_started) * 1000,
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
        self._sequence = 0

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

    def get_action(self, observation: dict[str, Any] | None) -> dict[str, float] | None:
        if observation is None:
            return None
        result = self.session.infer(observation, chunk_size=1)
        poses = _poses(result, {})
        self._sequence += 1
        self.dispatched_chunk_id = self._sequence
        self.dispatched_action_index = 0
        return poses[0] if poses else None

    def stop(self) -> bool:
        self.session.close()
        return True

    def wait_stopped(self, timeout: float | None = None) -> bool:
        return True


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
        return dict(action)

    def qsize(self) -> int:
        with self._lock:
            return len(self._queue)

    def get_action_index(self) -> int:
        return int(self.dispatched_action_index or 0)

    def get_processed_left_over(self) -> list[dict[str, float]]:
        with self._lock:
            return [dict(item[1]) for item in self._queue]

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
                result = self.session.infer(
                    observation,
                    prefix_raw=raw_prefix,
                    prefix_absolute=absolute_prefix,
                    inference_delay=conditioned_delay,
                )
                delay = math.ceil((time.perf_counter() - started) * self.fps) if prefix else 0
                self._last_delay = delay
                raw_rows = result.get("raw_actions") or []
                action_rows = result.get("actions") or []
                keys = [str(key) for key in result.get("action_keys") or self.session.action_keys]
                trimmed = min(delay, len(raw_rows), len(action_rows))
                self._sequence += 1
                replacement: deque[tuple[list[float], dict[str, float], int, int]] = deque()
                for index, (raw, action) in enumerate(zip(raw_rows[trimmed:], action_rows[trimmed:])):
                    replacement.append(
                        (
                            [float(value) for value in raw],
                            {
                                (key[:-4] if key.endswith(".pos") else key): float(action[pos])
                                for pos, key in enumerate(keys)
                                if pos < len(action)
                            },
                            self._sequence,
                            index + trimmed,
                        )
                    )
                with self._lock:
                    self._queue = replacement
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
