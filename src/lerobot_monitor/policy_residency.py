"""Process-local ownership of loaded policy bundles and their GPU memory."""

from __future__ import annotations

import gc
import hashlib
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .policy import (
    LoadedPolicy,
    resolve_cached_policy_path,
)

_COLD_LOAD_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


class PolicyBusyError(RuntimeError):
    """The requested policy still has a live inference owner."""


def _canonical_source(path: str, revision: str = "") -> str:
    if not path.strip():
        return ""
    source = resolve_cached_policy_path(path, revision) or path
    local = Path(source).expanduser()
    return str(local.resolve()) if local.is_dir() else source.strip()


def _checkpoint_fingerprint(root: Path) -> tuple[tuple[str, int, int], ...]:
    if not root.is_dir():
        return ()
    files = [root / "config.json"]
    for pattern in (
        "*.safetensors", "*.bin", "*.pt", "*.index.json",
        "policy_preprocessor*.json", "policy_postprocessor*.json",
    ):
        files.extend(root.glob(pattern))
    result: list[tuple[str, int, int]] = []
    for path in files:
        try:
            stat = path.stat()
            result.append((path.name, stat.st_size, stat.st_mtime_ns))
        except OSError:
            continue
    return tuple(sorted(result))


def _extra_value(extra: Mapping[str, str] | None, key: str) -> str:
    """Read one extra, tolerating the spellings the UI and CLI may send for it."""
    values = {
        str(k).removeprefix("--").removeprefix("policy."): str(v)
        for k, v in (extra or {}).items()
    }
    return values.get(key, "")


def _canonical_device(device: str | None) -> str:
    """Canonical CUDA spelling, so ``cuda`` and ``cuda:0`` name the same instance."""
    text = str(device or "cuda").strip().lower() or "cuda"
    return "cuda:0" if text == "cuda" else text


def policy_identity(
    path: str,
    device: str,
    extra: dict[str, str],
    *,
    robot_type: str,
    rename_map: dict[str, str],
) -> tuple[Any, ...]:
    """Identify a loaded instance by the weights it holds, not by the command line.

    ``policy.*`` and the other runtime extras cannot change the tensors a checkpoint
    holds, so they must never force a second copy of the same weights into VRAM; they
    are applied to the resident instance at use time (``policy.apply_requested_overrides``).
    ``robot_type`` and ``rename_map`` come from the process configuration, so they are
    constant across requests and never split entries in practice — but they do change the
    preprocessors built alongside the weights, so they stay in the key.
    """
    revision = _extra_value(extra, "pretrained_revision").strip()
    source = _canonical_source(path, revision)
    return (
        source,
        _canonical_device(device),
        _checkpoint_fingerprint(Path(source).expanduser()),
        robot_type,
        tuple(sorted((str(k), str(v)) for k, v in rename_map.items())),
    )


def _instance_id(key: tuple[Any, ...]) -> str:
    return hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:20]


def _tensor_storages(loaded: LoadedPolicy) -> dict[tuple[str, int, int], int]:
    """Find distinct CUDA storages owned by model and processor modules."""
    try:
        import torch
    except ImportError:
        return {}
    storages: dict[tuple[str, int, int], int] = {}
    def count(value: Any) -> None:
        if isinstance(value, torch.Tensor):
            tensor = value
            if tensor.device.type != "cuda":
                return
            storage = tensor.untyped_storage()
            storage_key = (str(tensor.device), storage.data_ptr(), storage.nbytes())
            storages[storage_key] = storage.nbytes()
        elif isinstance(value, dict):
            for child in value.values():
                count(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                count(child)

    policy = getattr(loaded, "policy", None)
    if isinstance(policy, torch.nn.Module):
        for tensor in policy.parameters(recurse=True):
            count(tensor)
        for tensor in policy.buffers(recurse=True):
            count(tensor)
    for pipeline in (getattr(loaded, "preprocessor", None), getattr(loaded, "postprocessor", None)):
        if isinstance(pipeline, torch.nn.Module):
            for tensor in pipeline.parameters(recurse=True):
                count(tensor)
            for tensor in pipeline.buffers(recurse=True):
                count(tensor)
        for step in getattr(pipeline, "steps", ()):
            count(getattr(step, "_tensor_stats", None))
    return storages


@dataclass
class _Entry:
    key: tuple[Any, ...]
    source_path: str
    device: str
    extra: dict[str, str]
    instance_id: str
    state: str = "queued"
    phase: str = "queued"
    completed_steps: int = 0
    total_steps: int = 4
    error: str = ""
    loaded: LoadedPolicy | None = None
    bytes: int = 0
    storages: dict[tuple[str, int, int], int] = field(default_factory=dict)
    busy: bool = False
    retired: threading.Event | None = None
    invalidated: bool = False
    condition: threading.Condition = field(default_factory=threading.Condition)
    requested_at: float = field(default_factory=time.monotonic)
    load_ms: float | None = None
    load_started_at: float | None = None
    phase_started_at: float | None = None
    stage_durations_ms: dict[str, float] = field(default_factory=dict)


class PolicyLease:
    def __init__(self, owner: PolicyResidencyManager, entry: _Entry, *, cache_hit: bool) -> None:
        self.owner = owner
        self.entry = entry
        self.loaded = entry.loaded
        self.cache_hit = cache_hit
        self._released = False
        self._release_lock = threading.Lock()

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
            self.loaded = None
        self.owner._release(self.entry)

    def retire(self, stopped: threading.Event) -> None:
        self.owner._retire(self.entry, stopped)
        threading.Thread(target=self._finish_retirement, args=(stopped,), daemon=True).start()

    def _finish_retirement(self, stopped: threading.Event) -> None:
        stopped.wait()
        self.release()


class PolicyResidencyManager:
    """Serialized cold loads; short state locks never span model construction."""

    def __init__(
        self,
        loader: Callable[..., LoadedPolicy],
        *,
        robot_type: str,
        rename_map: dict[str, str],
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.loader = loader
        self.robot_type = robot_type
        self.rename_map = dict(rename_map)
        self.on_change = on_change
        self._lock = threading.RLock()
        self._entries: dict[tuple[Any, ...], _Entry] = {}
        self._blocked_sources: set[str] = set()
        self._jobs: queue.Queue[_Entry | None] = queue.Queue()
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="policy-residency-load", daemon=True)
        self._worker_started = False

    def identity(self, path: str, device: str, extra: dict[str, str]) -> tuple[Any, ...]:
        return policy_identity(path, device, extra, robot_type=self.robot_type, rename_map=self.rename_map)

    def request(self, path: str, device: str, extra: dict[str, str]) -> _Entry:
        key = self.identity(path, device, extra)
        with self._lock:
            if self._closed:
                raise RuntimeError("policy manager is closed")
            if key[0] in self._blocked_sources:
                raise PolicyBusyError("model weights are being updated or deleted")
            entry = self._entries.get(key)
            if entry is not None:
                if entry.state == "unloading":
                    raise PolicyBusyError("model instance is unloading; retry loading after it is released")
                if entry.state == "error":
                    entry.state, entry.phase, entry.error = "queued", "queued", ""
                    entry.completed_steps = 0
                    entry.requested_at = time.monotonic()
                    if not self._worker_started:
                        self._worker.start()
                        self._worker_started = True
                    self._jobs.put(entry)
                    self._changed()
                return entry
            entry = _Entry(key, path, device, dict(extra), _instance_id(key))
            self._entries[key] = entry
            if not self._worker_started:
                self._worker.start()
                self._worker_started = True
            self._jobs.put(entry)
            self._changed()
            return entry

    def acquire_ready(self, path: str, device: str, extra: dict[str, str]) -> PolicyLease | None:
        key = self.identity(path, device, extra)
        with self._lock:
            if key[0] in self._blocked_sources:
                return None
            entry = self._entries.get(key)
            if entry is None or entry.state != "ready" or entry.busy or entry.loaded is None:
                return None
            entry.busy = True
            self._changed()
            return PolicyLease(self, entry, cache_hit=True)

    def ready(self, path: str, device: str, extra: dict[str, str]) -> LoadedPolicy | None:
        key = self.identity(path, device, extra)
        with self._lock:
            entry = self._entries.get(key)
            return entry.loaded if entry is not None and entry.state == "ready" and not entry.busy else None

    def acquire(
        self, path: str, device: str, extra: dict[str, str], *, timeout: float | None = None
    ) -> PolicyLease:
        entry = self.request(path, device, extra)
        # Only a lease that is waiting for a previous owner to let go is bounded: a cold
        # load is a legitimate multi-minute wait, while a thread that will not exit is
        # something the caller must be told about instead of hanging on `pending` forever.
        deadline: float | None = None
        with self._lock:
            cache_hit = entry.state in {"ready", "stopping"}
        while True:
            with self._lock:
                if self._closed or entry.invalidated:
                    raise RuntimeError("policy load was invalidated")
                if entry.state == "error":
                    raise RuntimeError(entry.error)
                if entry.state == "ready" and not entry.busy and entry.loaded is not None:
                    entry.busy = True
                    self._changed()
                    return PolicyLease(self, entry, cache_hit=cache_hit)
                stopping = entry.state == "stopping"
            if timeout is not None and stopping:
                if deadline is None:
                    deadline = time.monotonic() + max(0.0, timeout)
                if time.monotonic() >= deadline:
                    raise PolicyBusyError(
                        f"model instance is not ready after {timeout:.1f}s (state={entry.state}); "
                        "wait for the previous inference thread to exit"
                    )
            else:
                deadline = None
            with entry.condition:
                entry.condition.wait(timeout=0.1)

    def _release(self, entry: _Entry) -> None:
        with self._lock:
            entry.busy = False
            entry.retired = None
            if entry.state == "stopping":
                entry.state = "ready"
            self._changed()
        with entry.condition:
            entry.condition.notify_all()

    def _retire(self, entry: _Entry, stopped: threading.Event) -> None:
        with self._lock:
            entry.state = "stopping"
            entry.retired = stopped
            self._changed()

    def _run(self) -> None:
        while True:
            entry = self._jobs.get()
            if entry is None:
                return
            with self._lock:
                if entry.invalidated or self._closed:
                    continue
                entry.state, entry.phase = "loading", "waiting"
                entry.load_started_at = time.perf_counter()
                entry.phase_started_at = entry.load_started_at
                entry.stage_durations_ms.clear()
                self._changed()
            started = time.perf_counter()

            def progress(phase: str, completed: int, target: _Entry = entry) -> None:
                with self._lock:
                    if not target.invalidated:
                        now = time.perf_counter()
                        if target.phase_started_at is not None:
                            elapsed = (now - target.phase_started_at) * 1000.0
                            target.stage_durations_ms[target.phase] = (
                                target.stage_durations_ms.get(target.phase, 0.0) + elapsed
                            )
                            logger.info("model load %s: %s took %.3fs", target.source_path, target.phase, elapsed / 1000)
                        target.phase_started_at = now
                        target.phase = phase
                        target.completed_steps = completed
                        self._changed()

            loaded: LoadedPolicy | None = None
            failed = False
            try:
                with _COLD_LOAD_LOCK:
                    if entry.invalidated or self._closed:
                        continue
                    loaded = self.loader(
                        entry.source_path,
                        device=entry.device,
                        task="",
                        robot_type=self.robot_type,
                        rename_map=self.rename_map,
                        extra=entry.extra,
                        progress=progress,
                    )
                storages = _tensor_storages(loaded)
                byte_count = sum(storages.values())
                resolved_key = self.identity(entry.source_path, entry.device, entry.extra)
                progress("finalizing", entry.total_steps)
            except Exception as exc:  # noqa: BLE001 - failures belong to one entry
                with self._lock:
                    if not entry.invalidated:
                        if entry.phase_started_at is not None:
                            elapsed = (time.perf_counter() - entry.phase_started_at) * 1000.0
                            entry.stage_durations_ms[entry.phase] = (
                                entry.stage_durations_ms.get(entry.phase, 0.0) + elapsed
                            )
                        logger.exception("model load failed during %s: %s", entry.phase, entry.source_path)
                        entry.state, entry.phase = "error", "error"
                        entry.error = f"{type(exc).__name__}: {exc}"
                        self._changed()
                with entry.condition:
                    entry.condition.notify_all()
                failed = True
            if failed:
                loaded = None
                # Leave the exception handler before collecting: its traceback can
                # otherwise keep a partially constructed CUDA model alive.
                gc.collect()
                self._empty_cuda_cache()
                continue
            stored = False
            with self._lock:
                if not entry.invalidated and not self._closed:
                    if resolved_key != entry.key and resolved_key not in self._entries:
                        self._entries.pop(entry.key, None)
                        entry.key = resolved_key
                        entry.instance_id = _instance_id(resolved_key)
                        self._entries[resolved_key] = entry
                    entry.loaded = loaded
                    entry.bytes = byte_count
                    entry.storages = storages
                    entry.state, entry.phase = "ready", "ready"
                    entry.completed_steps = entry.total_steps
                    entry.load_ms = (time.perf_counter() - started) * 1000.0
                    logger.info("model ready in %.3fs: %s", entry.load_ms / 1000, entry.source_path)
                    self._changed()
                    stored = True
            with entry.condition:
                entry.condition.notify_all()
            loaded = None
            if not stored:
                # A memory clean or shutdown may have invalidated this cold load
                # while it was building the model. Do not leave its CUDA pages
                # in the allocator after dropping the late result.
                gc.collect()
                self._empty_cuda_cache()

    def unload(self, path: str, instance_id: str | None = None) -> int:
        source = _canonical_source(path)
        with self._lock:
            candidates = [
                item for item in self._entries.values()
                if item.key[0] == source and (instance_id is None or item.instance_id == instance_id)
            ]
            if any(item.busy or item.state in {"queued", "loading", "stopping"} for item in candidates):
                raise PolicyBusyError("stop the active inference or wait for model loading to finish before unloading")
            pending = [item for item in candidates if item.state != "unloading"]
            for item in pending:
                item.invalidated = True
                item.state = "unloading"
            if pending:
                self._changed()
        if pending:
            threading.Thread(target=self._finish_unload, args=(pending,), daemon=True).start()
        return len(candidates)

    def block_source(self, path: str) -> str:
        source = _canonical_source(path)
        with self._lock:
            if source in self._blocked_sources:
                raise PolicyBusyError("model weights are already being updated or deleted")
            if any(
                item.key[0] == source and (item.busy or item.state in {"queued", "loading", "stopping", "unloading"})
                for item in self._entries.values()
            ):
                raise PolicyBusyError("stop the active inference or wait for loading before changing model weights")
            self._blocked_sources.add(source)
        return source

    def unblock_source(self, source: str) -> None:
        with self._lock:
            self._blocked_sources.discard(source)

    def _finish_unload(self, entries: list[_Entry]) -> None:
        for item in entries:
            item.loaded = None
            item.storages = {}
        gc.collect()
        self._empty_cuda_cache()
        with self._lock:
            for item in entries:
                if self._entries.get(item.key) is item:
                    self._entries.pop(item.key)
            self._changed()
        for item in entries:
            with item.condition:
                item.condition.notify_all()

    @staticmethod
    def _empty_cuda_cache() -> None:
        module = sys.modules.get("torch")
        cuda = getattr(module, "cuda", None)
        if cuda is not None and cuda.is_initialized():
            cuda.empty_cache()

    def status(self, path: str) -> dict[str, Any]:
        source = _canonical_source(path)
        with self._lock:
            entries = [item for item in self._entries.values() if item.key[0] == source]
            unique_storages: dict[tuple[str, int, int], int] = {}
            for item in entries:
                unique_storages.update(item.storages)
            instances = [
                {
                    "id": item.instance_id,
                    "device": item.device,
                    "overrides": dict(item.extra),
                    "state": "in_use" if item.busy and item.state == "ready" else item.state,
                    "phase": item.phase,
                    "completed_steps": item.completed_steps,
                    "total_steps": item.total_steps,
                    "error": item.error,
                    "gpu_bytes": item.bytes,
                    "load_ms": item.load_ms,
                    "elapsed_ms": (
                        (time.perf_counter() - item.load_started_at) * 1000.0
                        if item.state == "loading" and item.load_started_at is not None else item.load_ms
                    ),
                    "phase_elapsed_ms": (
                        (time.perf_counter() - item.phase_started_at) * 1000.0
                        if item.state == "loading" and item.phase_started_at is not None else None
                    ),
                    "stage_durations_ms": dict(item.stage_durations_ms),
                }
                for item in entries
            ]
        priority = ("error", "unloading", "stopping", "loading", "queued", "in_use", "ready")
        states = {item["state"] for item in instances}
        state = next((candidate for candidate in priority if candidate in states), "unloaded")
        return {
            "state": state,
            "instances": instances,
            "gpu_bytes": sum(unique_storages.values()),
            "can_unload": bool(instances) and states <= {"ready", "error"},
        }

    def all_statuses(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            sources = {str(key[0]) for key in self._entries}
        return {source: self.status(source) for source in sources}

    def has_active_inference(self) -> bool:
        with self._lock:
            return any(item.busy for item in self._entries.values())

    @staticmethod
    def process_gpu_memory() -> list[dict[str, int]]:
        module = sys.modules.get("torch")
        cuda = getattr(module, "cuda", None)
        if cuda is None or not cuda.is_initialized():
            return []
        return [
            {
                "device_index": index,
                "allocated_bytes": int(cuda.memory_allocated(index)),
                "reserved_bytes": int(cuda.memory_reserved(index)),
            }
            for index in range(cuda.device_count())
        ]

    def clear_all(self) -> int:
        with self._lock:
            if any(item.busy for item in self._entries.values()):
                raise PolicyBusyError("stop the active inference before cleaning memory")
            entries = list(self._entries.values())
            self._entries.clear()
            for item in entries:
                item.invalidated = True
                item.loaded = None
                item.storages = {}
            self._changed()
        for item in entries:
            with item.condition:
                item.condition.notify_all()
        gc.collect()
        self._empty_cuda_cache()
        return len(entries)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            busy = any(item.busy for item in self._entries.values())
        if busy:
            def finish() -> None:
                while True:
                    with self._lock:
                        if not any(item.busy for item in self._entries.values()):
                            break
                    time.sleep(0.05)
                self.clear_all()

            threading.Thread(target=finish, name="close-policy-residency", daemon=True).start()
        else:
            self.clear_all()
        if self._worker_started:
            self._jobs.put(None)

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()
