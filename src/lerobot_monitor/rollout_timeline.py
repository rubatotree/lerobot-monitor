"""Thread-safe inference and action-chunk telemetry for rollout charts."""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _RolloutBlock:
    id: int
    kind: str
    start: float
    end: float | None = None
    active: float | None = None
    steps: int | None = None
    step_s: float | None = None
    failed: bool = False
    accepted: bool = False
    accepted_at: float | None = None
    action_end: float | None = None
    last_dispatched: float | None = None
    prefix_trimmed: int = 0
    accepted_steps: int = 0
    consumed_steps: int = 0
    dispatched_steps: int = 0
    last_dispatch_index: int = -1
    replaced_steps: int = 0
    replaced_by: int | None = None
    replaced_at: float | None = None
    status: str = "inferring"
    stages: list[dict[str, Any]] = field(default_factory=list)
    pending_dispatches: dict[int, float] = field(default_factory=dict)
    pending_last_dispatch: float | None = None


class RolloutTimeline:
    """Record rollout inference intervals and their handoff to execution.

    Timestamps are monotonic but are exposed relative to the rollout start. All
    public methods are failure-safe: chart telemetry must never disturb control.
    """

    def __init__(self, *, window_s: float = 20.0, max_blocks: int = 256) -> None:
        self._window_s = max(0.0, float(window_s))
        self._max_blocks = max(1, int(max_blocks))
        self._lock = threading.Lock()
        self._enabled = False
        self._epoch: float | None = None
        self._run_id: str | None = None
        self._epoch_ts: float | None = None
        self._stopped_at: float | None = None
        self._next_id = 1
        self._blocks: list[_RolloutBlock] = []
        self._last_qsize: int | None = None
        self._last_index: int | None = None

    def set_enabled(
        self, enabled: bool, *, epoch_monotonic: float | None = None
    ) -> None:
        """Start or stop recording without publishing partial rollout state."""
        try:
            with self._lock:
                if enabled:
                    if not self._enabled:
                        self._enabled = True
                        self._epoch = (
                            time.perf_counter()
                            if epoch_monotonic is None
                            else epoch_monotonic
                        )
                        self._epoch_ts = time.time() - (
                            time.perf_counter() - self._epoch
                        )
                        self._run_id = uuid.uuid4().hex
                        self._stopped_at = None
                        self._blocks.clear()
                        self._next_id = 1
                        self._last_qsize = None
                        self._last_index = None
                else:
                    self._enabled = False
                    self._stopped_at = time.perf_counter()
                    for block in self._blocks:
                        if block.end is None:
                            block.end = self._stopped_at
                        if block.action_end is None and block.active is not None:
                            block.action_end = self._stopped_at
                        if block.status in {"inferring", "waiting", "active"}:
                            block.status = "stopped"
        except Exception as exc:  # noqa: BLE001 - telemetry must not affect control
            logger.debug("could not change rollout timeline state: %s", exc)

    def clear(self) -> None:
        """Drop every recorded block."""
        try:
            with self._lock:
                self._blocks.clear()
                self._next_id = 1
                self._last_qsize = None
                self._last_index = None
                self._epoch = time.perf_counter() if self._enabled else None
                self._epoch_ts = time.time() if self._enabled else None
                self._run_id = uuid.uuid4().hex if self._enabled else None
                self._stopped_at = None
        except Exception as exc:  # noqa: BLE001 - telemetry must not affect control
            logger.debug("could not clear rollout timeline: %s", exc)

    def note_inference_start(
        self,
        *,
        kind: str,
        step_s: float,
        chunk_id: int | None = None,
        started_at: float | None = None,
    ) -> int | None:
        """Open an inference interval and return its block token."""
        try:
            step_s = self._positive_float(step_s)
            with self._lock:
                if not self._enabled or self._epoch is None:
                    return None
                if kind == "rtc" and any(
                    block.kind == "sync" and block.end is None for block in self._blocks
                ):
                    return None
                block = _RolloutBlock(
                    id=self._next_id if chunk_id is None else chunk_id,
                    kind=str(kind or "unknown"),
                    start=time.perf_counter() if started_at is None else started_at,
                    step_s=step_s,
                )
                self._next_id = max(self._next_id, block.id) + 1
                self._blocks.append(block)
                self._trim_locked()
                return block.id
        except Exception as exc:  # noqa: BLE001 - telemetry must not affect control
            logger.debug("could not start rollout inference timing: %s", exc)
            return None

    def note_inference_end(
        self, token: int | None, *, ok: bool, steps: int | None, discarded: bool = False
    ) -> None:
        """Close a previously opened inference interval."""
        if token is None:
            return
        try:
            with self._lock:
                if not self._enabled:
                    return
                block = self._find_locked(token)
                if block is None:
                    return
                if block.end is None:
                    block.end = time.perf_counter()
                block.failed = not bool(ok) and not discarded
                block.status = (
                    "discarded" if discarded else ("waiting" if ok else "failed")
                )
                normalized_steps = self._positive_int(steps)
                if normalized_steps is not None:
                    block.steps = normalized_steps
        except Exception as exc:  # noqa: BLE001 - telemetry must not affect control
            logger.debug("could not end rollout inference timing: %s", exc)

    def note_chunk_ready(self, *, steps: int, step_s: float) -> None:
        """Mark the synchronous worker's chunk handoff with a green-line time."""
        try:
            with self._lock:
                if not self._enabled:
                    return
                now = time.perf_counter()
                block = self._latest_pending_locked()
                if block is None:
                    block = _RolloutBlock(
                        id=self._next_id,
                        kind="sync",
                        start=now,
                        end=now,
                    )
                    self._next_id += 1
                    self._blocks.append(block)
                elif block.end is None:
                    block.end = now
                normalized_steps = self._positive_int(steps)
                if normalized_steps is not None:
                    block.steps = normalized_steps
                normalized_step_s = self._positive_float(step_s)
                if normalized_step_s is not None:
                    block.step_s = normalized_step_s
                block.active = max(now, block.end)
                self._trim_locked()
        except Exception as exc:  # noqa: BLE001 - telemetry must not affect control
            logger.debug("could not mark rollout chunk ready: %s", exc)

    def note_stage(self, token: int | None, stage: Any) -> None:
        """Copy native monotonic stage records; updates may later add GPU duration."""
        if token is None:
            return
        try:
            with self._lock:
                block = self._find_locked(token)
                if block is None or not self._enabled:
                    return
                record = {"name": stage.name, "start": stage.start, "end": stage.end}
                if stage.name == "publish" and block.end is not None:
                    block.end = max(block.end, stage.end)
                if stage.gpu_ms is not None:
                    record["gpu_ms"] = stage.gpu_ms
                for index, previous in enumerate(block.stages):
                    if (
                        previous["name"] == stage.name
                        and previous["start"] == stage.start
                    ):
                        block.stages[index] = record
                        break
                else:
                    if len(block.stages) < 64:
                        block.stages.append(record)
        except Exception:
            logger.debug("could not record inference stage", exc_info=True)

    def note_chunk_accepted(
        self,
        token: int,
        *,
        steps: int,
        prefix_trimmed: int = 0,
        replaced: tuple[tuple[int, int], ...] = (),
        timestamp: float | None = None,
    ) -> None:
        """Apply the actual queue merge receipt, without changing generated steps."""
        try:
            with self._lock:
                if not self._enabled:
                    return
                now = time.perf_counter() if timestamp is None else timestamp
                block = self._find_locked(token)
                if block is None or block.failed:
                    return
                block.accepted = steps > 0
                block.accepted_at = now
                block.accepted_steps = max(0, steps)
                block.prefix_trimmed = max(0, prefix_trimmed)
                if block.pending_dispatches and steps > 0:
                    block.active = min(block.pending_dispatches.values())
                    block.last_dispatched = block.pending_last_dispatch
                    block.last_dispatch_index = max(block.pending_dispatches)
                    block.dispatched_steps = len(block.pending_dispatches)
                    for old in self._blocks:
                        if (
                            old is not block
                            and old.active is not None
                            and old.action_end is None
                        ):
                            old.action_end = block.active
                            if old.status != "replaced":
                                old.status = "completed"
                block.pending_dispatches.clear()
                block.status = (
                    "active"
                    if block.active is not None
                    else ("waiting" if steps else "discarded")
                )
                for old_id, count in replaced:
                    old = self._find_locked(old_id)
                    if old is not None:
                        old.replaced_steps += count
                        old.replaced_by = token
                        old.replaced_at = now
                        old.status = "replaced"
        except Exception:
            logger.debug("could not record queue merge", exc_info=True)

    def note_consumed(self, token: int | None, *, index: int | None = None) -> None:
        """Count queue pops independently of successful hardware sends."""
        try:
            with self._lock:
                block = self._find_locked(token)
                if self._enabled and block is not None:
                    block.consumed_steps = (
                        max(block.consumed_steps, index + 1)
                        if index is not None
                        else block.consumed_steps + 1
                    )
        except Exception:
            logger.debug("could not record action consumption", exc_info=True)

    def note_dispatched(self, token: int | None, *, index: int | None = None) -> None:
        """Record successful sends once per source action, even with interpolation."""
        try:
            with self._lock:
                block = self._find_locked(token)
                if not self._enabled or block is None or block.failed:
                    return
                now = time.perf_counter()
                if not block.accepted:
                    # A consumer can win the race after queue publication but before
                    # the producer emits its receipt. Reconcile only once accepted.
                    if index is not None and len(block.pending_dispatches) < 4096:
                        block.pending_dispatches.setdefault(index, now)
                        block.pending_last_dispatch = now
                    return
                for old in self._blocks:
                    if (
                        old is not block
                        and old.active is not None
                        and old.action_end is None
                    ):
                        old.action_end = now
                        if old.status != "replaced":
                            old.status = "completed"
                if block.active is None:
                    block.active = now
                if block.status != "replaced":
                    block.status = "active"
                source_index = (
                    max(0, block.consumed_steps - 1) if index is None else index
                )
                block.last_dispatched = now
                if source_index > block.last_dispatch_index:
                    block.dispatched_steps += 1
                    block.last_dispatch_index = source_index
        except Exception:
            logger.debug("could not record successful action send", exc_info=True)

    def observe(self, *, qsize: int | None, index: int | None) -> None:
        """Detect an RTC queue handoff from a consumption or merge transition."""
        try:
            normalized_qsize = self._nonnegative_int(qsize)
            normalized_index = self._nonnegative_int(index)
            with self._lock:
                if not self._enabled:
                    return
                qsize_drop = (
                    normalized_qsize is not None
                    and self._last_qsize is not None
                    and normalized_qsize < self._last_qsize
                )
                index_reset = (
                    normalized_index == 0
                    and self._last_index is not None
                    and self._last_index > 0
                )
                if qsize_drop or index_reset:
                    self._activate_latest_locked(time.perf_counter())
                self._last_qsize = normalized_qsize
                self._last_index = normalized_index
        except Exception as exc:  # noqa: BLE001 - telemetry must not affect control
            logger.debug("could not observe rollout queue state: %s", exc)

    def latest_latency_ms(self) -> float | None:
        """Return the latest completed inference duration in milliseconds."""
        try:
            with self._lock:
                for block in reversed(self._blocks):
                    if block.end is not None:
                        return round(max(0.0, block.end - block.start) * 1000.0, 3)
        except Exception as exc:  # noqa: BLE001 - telemetry must not affect control
            logger.debug("could not read rollout inference latency: %s", exc)
        return None

    def snapshot(self) -> dict[str, Any] | None:
        """Return a JSON-safe, windowed view of the current rollout telemetry."""
        try:
            with self._lock:
                if not self._enabled or self._epoch is None or not self._blocks:
                    return None
                now = (
                    self._stopped_at
                    if self._stopped_at is not None
                    else time.perf_counter()
                )
                cutoff = now - self._window_s
                blocks = [
                    block
                    for block in self._blocks
                    if block.end is None
                    or block.active is not None
                    or block.end >= cutoff
                ]
                if not blocks:
                    return None
                latest_step_s = next(
                    (
                        block.step_s
                        for block in reversed(self._blocks)
                        if block.step_s is not None
                    ),
                    None,
                )
                return {
                    "run_id": self._run_id,
                    "epoch_ts": self._epoch_ts,
                    "t_s": round(max(0.0, now - self._epoch), 3),
                    "step_s": None
                    if latest_step_s is None
                    else round(latest_step_s, 6),
                    "blocks": [
                        self._block_payload(block, self._epoch) for block in blocks
                    ],
                }
        except Exception as exc:  # noqa: BLE001 - telemetry must not affect control
            logger.debug("could not snapshot rollout timeline: %s", exc)
        return None

    def _block_payload(self, block: _RolloutBlock, epoch: float) -> dict[str, Any]:
        return {
            "id": block.id,
            "kind": block.kind,
            "start": round(block.start - epoch, 3),
            "end": None if block.end is None else round(block.end - epoch, 3),
            "active": None if block.active is None else round(block.active - epoch, 3),
            "steps": block.accepted_steps
            if block.accepted_at is not None
            else block.steps,
            "step_s": None if block.step_s is None else round(block.step_s, 6),
            "failed": bool(block.failed),
            "status": block.status,
            "original_steps": block.steps,
            "prefix_trimmed": block.prefix_trimmed,
            "accepted_steps": block.accepted_steps,
            "consumed_steps": block.consumed_steps,
            "dispatched_steps": block.dispatched_steps,
            "remaining_steps": max(
                0, block.accepted_steps - block.consumed_steps - block.replaced_steps
            ),
            "replaced_steps": block.replaced_steps,
            "replaced_by": block.replaced_by,
            **{
                name: None
                if (value := getattr(block, name)) is None
                else round(value - epoch, 6)
                for name in (
                    "accepted_at",
                    "action_end",
                    "last_dispatched",
                    "replaced_at",
                )
            },
            "stages": [
                {
                    **stage,
                    "start": round(stage["start"] - epoch, 6),
                    "end": round(stage["end"] - epoch, 6),
                }
                for stage in block.stages
            ],
        }

    def _find_locked(self, token: int) -> _RolloutBlock | None:
        for block in self._blocks:
            if block.id == token:
                return block
        return None

    def _latest_pending_locked(self) -> _RolloutBlock | None:
        for block in reversed(self._blocks):
            if block.end is not None and block.active is None:
                return block
        return None

    def _activate_latest_locked(self, now: float) -> None:
        for block in reversed(self._blocks):
            if block.end is not None and block.active is None:
                block.active = max(now, block.end)
                return

    def _trim_locked(self) -> None:
        while len(self._blocks) > self._max_blocks:
            # The newest inference must survive to receive its completion events.
            previous = self._blocks[:-1]
            removable = next(
                (
                    index
                    for index, block in enumerate(previous)
                    if block.action_end is not None
                    or block.status in {"failed", "discarded", "stopped"}
                ),
                None,
            )
            if removable is None:
                removable = next(
                    (
                        index
                        for index, block in enumerate(previous)
                        if block.end is not None and block.active is None
                    ),
                    0,
                )
            self._blocks.pop(removable)

    @staticmethod
    def _positive_float(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number > 0.0 else None

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @staticmethod
    def _nonnegative_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None
