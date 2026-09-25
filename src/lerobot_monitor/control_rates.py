"""Control cadence settings and measurements shared by all robot modes."""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

RATE_MODES = ("joints", "teleop", "record", "playback", "rollout")
RATE_PRESETS = (1.0, 2.0, 4.0, 6.0, 8.0)
RATE_HZ_MIN = 1.0
RATE_HZ_MAX = 240.0


@dataclass(frozen=True)
class RateSetting:
    kind: str = "inherit"
    value: float | None = None

    @classmethod
    def parse(cls, raw: Any) -> RateSetting:
        if raw is None:
            return cls()
        if isinstance(raw, (int, float)):
            raw = {"kind": "hz", "value": raw}
        if not isinstance(raw, dict):
            raise ValueError("control rate must be an object")
        kind = str(raw.get("kind") or "inherit")
        if kind == "inherit":
            return cls()
        if kind not in {"hz", "multiplier"}:
            raise ValueError("control rate kind must be inherit, hz, or multiplier")
        try:
            value = float(raw["value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("control rate needs a numeric value") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError("control rate value must be positive and finite")
        if kind == "multiplier" and value not in RATE_PRESETS:
            raise ValueError("control multiplier must be 1, 2, 4, 6, or 8")
        return cls(kind, value)

    def resolve(self, default_hz: float, source_hz: float | None = None) -> float:
        if self.kind == "inherit":
            result = default_hz
        elif self.kind == "hz":
            result = float(self.value)
        else:
            if source_hz is None or not math.isfinite(source_hz) or source_hz <= 0:
                raise ValueError("control multiplier needs a known action source FPS")
            result = source_hz * float(self.value)
        if not math.isfinite(result) or result < RATE_HZ_MIN or result > RATE_HZ_MAX:
            # Name the resolved value: a high multiplier over a fast source is
            # the usual way to land outside the supported band.
            raise ValueError(
                f"control frequency must be between {RATE_HZ_MIN:g} and {RATE_HZ_MAX:g} Hz "
                f"(this setting resolves to {result:g} Hz)"
            )
        return result

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "value": self.value}


class CadenceStats:
    """Only completed sends count toward the measured output frequency."""

    def __init__(self) -> None:
        self._times: deque[float] = deque(maxlen=2400)
        self.sent = 0
        self.failed = 0
        self.missed_slots = 0
        self.source_updates = 0
        self.holds = 0
        self.reads = 0
        self._read_times: deque[float] = deque(maxlen=2400)

    def sent_at(self, when: float | None = None, *, hold: bool = False) -> None:
        self._times.append(time.perf_counter() if when is None else when)
        self.sent += 1
        self.holds += int(hold)

    def read_at(self, when: float | None = None) -> None:
        self._read_times.append(time.perf_counter() if when is None else when)
        self.reads += 1

    @staticmethod
    def _hz(times: deque[float], now: float) -> float:
        window = [t for t in times if now - t <= 1.0]
        return round((len(window) - 1) / (window[-1] - window[0]), 2) if len(window) > 1 and window[-1] > window[0] else 0.0

    def snapshot(self, target_hz: float, now: float | None = None) -> dict[str, Any]:
        now = time.perf_counter() if now is None else now
        recent = [t for t in self._times if now - t <= 1.0]
        intervals = sorted((right - left) * 1000 for left, right in zip(recent, recent[1:], strict=False))
        p95 = intervals[math.ceil(0.95 * len(intervals)) - 1] if intervals else None
        return {
            "target_hz": target_hz,
            "actual_hz": self._hz(self._times, now),
            "read_hz": self._hz(self._read_times, now),
            "interval_p95_ms": round(p95, 2) if p95 is not None else None,
            "interval_max_ms": round(intervals[-1], 2) if intervals else None,
            "sent": self.sent,
            "hold_sends": self.holds,
            "failed": self.failed,
            "missed_slots": self.missed_slots,
            "source_updates": self.source_updates,
        }
