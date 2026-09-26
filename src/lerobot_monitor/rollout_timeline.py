"""Thread-safe inference and action-chunk telemetry for rollout charts."""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
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
        self._next_id = 1
        self._blocks: list[_RolloutBlock] = []
        self._last_qsize: int | None = None
        self._last_index: int | None = None

    def set_enabled(self, enabled: bool) -> None:
        """Start or stop recording without publishing partial rollout state."""
        try:
            with self._lock:
                if enabled:
                    if not self._enabled:
                        self._enabled = True
                        self._epoch = time.perf_counter()
                        self._blocks.clear()
                        self._next_id = 1
                        self._last_qsize = None
                        self._last_index = None
                else:
                    self._enabled = False
                    self._epoch = None
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
        except Exception as exc:  # noqa: BLE001 - telemetry must not affect control
            logger.debug("could not clear rollout timeline: %s", exc)

    def note_inference_start(self, *, kind: str, step_s: float) -> int | None:
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
                    id=self._next_id,
                    kind=str(kind or "unknown"),
                    start=time.perf_counter(),
                    step_s=step_s,
                )
                self._next_id += 1
                self._blocks.append(block)
                self._trim_locked()
                return block.id
        except Exception as exc:  # noqa: BLE001 - telemetry must not affect control
            logger.debug("could not start rollout inference timing: %s", exc)
            return None

    def note_inference_end(self, token: int | None, *, ok: bool, steps: int | None) -> None:
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
                block.end = time.perf_counter()
                block.failed = not bool(ok)
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
                if self._epoch is None or not self._blocks:
                    return None
                now = time.perf_counter()
                cutoff = now - self._window_s
                blocks = [
                    block
                    for block in self._blocks
                    if block.end is None or block.active is not None or block.end >= cutoff
                ]
                if not blocks:
                    return None
                latest_step_s = next(
                    (block.step_s for block in reversed(self._blocks) if block.step_s is not None),
                    None,
                )
                return {
                    "t_s": round(max(0.0, now - self._epoch), 3),
                    "step_s": None if latest_step_s is None else round(latest_step_s, 6),
                    "blocks": [self._block_payload(block, self._epoch) for block in blocks],
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
            "steps": block.steps,
            "step_s": None if block.step_s is None else round(block.step_s, 6),
            "failed": bool(block.failed),
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
            removable = next(
                (index for index, block in enumerate(self._blocks) if block.active is None),
                None,
            )
            if removable is None:
                removable = 0
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
