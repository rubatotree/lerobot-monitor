"""Named millisecond spans shared by the cloud request path.

One cloud inference crosses three processes (Monitor, cloud service, policy worker);
each measures the phases it owns and the caller merges them into one flat
``name -> ms`` dictionary. Values are display-only: nothing here influences control,
so failures to measure must never fail a request.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any


class Timing:
    """Accumulate named wall-clock spans, in milliseconds, rounded to 3 decimals."""

    __slots__ = ("_ms",)

    def __init__(self) -> None:
        self._ms: dict[str, float] = {}

    def add(self, name: str, seconds: float) -> None:
        """Add one span; repeated names accumulate (one phase measured in two places)."""
        if not (seconds > 0):
            return
        self._ms[name] = round(self._ms.get(name, 0.0) + seconds * 1000.0, 3)

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.add(name, time.perf_counter() - started)

    def merge(self, prefix: str, values: Mapping[str, Any] | None) -> None:
        """Copy another process' spans; non-numeric and non-positive entries are skipped."""
        for name, value in (values or {}).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            amount = float(value)
            if amount > 0:
                self._ms[f"{prefix}{name}"] = round(amount, 3)

    def as_dict(self) -> dict[str, float]:
        """Spans in measurement order (insertion order), so logs read as a timeline."""
        return dict(self._ms)
