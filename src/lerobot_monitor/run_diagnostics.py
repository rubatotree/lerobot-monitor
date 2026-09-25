"""Bounded, asynchronous experiment records kept off the control bus thread."""

from __future__ import annotations

import csv
import json
import queue
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any


class RunDiagnostics:
    def __init__(self, root: Path, mode: str, settings: dict[str, Any], *, trace: bool = False) -> None:
        self.id = uuid.uuid4().hex
        self.root = Path(root) / self.id
        self.mode = mode
        self.settings = settings
        self.trace = trace
        self.started = time.perf_counter()
        self.events: deque[dict[str, Any]] = deque(maxlen=100)
        self.segments: list[dict[str, Any]] = [{"start_s": 0.0, "target_hz": settings["effective_hz"], "sent_start": 0}]
        self.dropped_trace = 0
        self._overflow_noted = False
        self.error: str | None = None
        self._jobs: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(maxsize=8192)
        self._closed = threading.Event()
        self._summary: dict[str, Any] | None = None
        self._thread = threading.Thread(target=self._write, name=f"run-log-{self.id[:8]}", daemon=True)
        self._thread.start()

    def event(self, name: str, details: dict[str, Any] | None = None, *, when: float | None = None) -> None:
        at = time.perf_counter() if when is None else when
        row = {"t_s": round(max(0.0, at - self.started), 6), "type": name, **(details or {})}
        self.events.append(row)
        try:
            self._jobs.put_nowait(("event", row))
        except queue.Full:
            self._note_overflow(at)

    def tick(self, row: dict[str, Any]) -> None:
        if not self.trace:
            return
        item = {"t_s": round(time.perf_counter() - self.started, 6), **row}
        try:
            self._jobs.put_nowait(("tick", item))
        except queue.Full:
            self._note_overflow(time.perf_counter())

    def _note_overflow(self, when: float) -> None:
        self.dropped_trace += 1
        if not self._overflow_noted:
            self._overflow_noted = True
            self.events.append({"t_s": round(max(0.0, when - self.started), 6), "type": "log_queue_overflow"})

    def change_rate(self, target_hz: float, sent: int) -> None:
        at = round(time.perf_counter() - self.started, 6)
        segment = self.segments[-1]
        count = sent - segment["sent_start"]
        duration = max(0.0, at - segment["start_s"])
        segment.update(end_s=at, sent=count, actual_hz=round(count / duration, 2) if duration else 0.0)
        self.segments.append({"start_s": at, "target_hz": target_hz, "sent_start": sent})
        self.event("rate_changed", {"target_hz": target_hz})

    def finish(self, cadence: dict[str, Any], sent: int) -> None:
        if self._closed.is_set():
            return
        at = round(time.perf_counter() - self.started, 6)
        segment = self.segments[-1]
        count = sent - segment["sent_start"]
        duration = max(0.0, at - segment["start_s"])
        segment.update(end_s=at, sent=count, actual_hz=round(count / duration, 2) if duration else 0.0)
        self._summary = {
            "id": self.id, "mode": self.mode, "settings": self.settings,
            "duration_s": at, "segments": self.segments,
            "cadence": cadence, "dropped_log_records": self.dropped_trace,
        }
        self._closed.set()

    def snapshot(self) -> dict[str, Any]:
        return {"id": self.id, "events": list(self.events), "dropped_log_records": self.dropped_trace, "error": self.error}

    def _write(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / "events.jsonl").open("w", encoding="utf-8") as events:
                tick_file = None
                try:
                    tick_writer: csv.DictWriter | None = None
                    while not self._closed.is_set() or not self._jobs.empty():
                        try:
                            kind, row = self._jobs.get(timeout=0.1)
                        except queue.Empty:
                            continue
                        if kind == "event":
                            events.write(json.dumps(row, ensure_ascii=False) + "\n")
                        elif kind == "tick":
                            if tick_file is None:
                                tick_file = (self.root / "ticks.csv").open("w", encoding="utf-8", newline="")
                            if tick_writer is None:
                                tick_writer = csv.DictWriter(tick_file, fieldnames=list(row))
                                tick_writer.writeheader()
                            tick_writer.writerow(row)
                finally:
                    if tick_file is not None:
                        tick_file.close()
            if self._summary is not None:
                (self.root / "summary.json").write_text(
                    json.dumps(self._summary, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except OSError as exc:
            self.error = str(exc)
