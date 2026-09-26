"""Single-owner worker for synchronous policy inference."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from .rollout_timeline import RolloutTimeline


class PolicyWorker:
    def __init__(
        self,
        engine: Any,
        inference_lock: threading.Lock,
        preview: Callable[[dict[str, float]], list[dict[str, float]]] | None = None,
        timeline: RolloutTimeline | None = None,
        step_s: float = 1.0 / 30.0,
        kind: str = "sync",
        prepare: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        convert: Callable[[Any, dict[str, float]], dict[str, float]] | None = None,
    ) -> None:
        self.engine = engine
        self.inference_lock = inference_lock
        self.preview = preview
        self.timeline = timeline
        self.step_s = float(step_s)
        self.kind = str(kind)
        self.prepare = prepare
        self.convert = convert
        self._requests: queue.Queue[tuple[dict[str, Any], dict[str, float]]] = queue.Queue(maxsize=1)
        self._results: queue.Queue[tuple[float, Any, str | None, list[dict[str, float]]]] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self.stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="monitor-policy-inference", daemon=True)
        self._thread.start()

    def _note_start(self) -> int | None:
        if self.timeline is None:
            return None
        try:
            return self.timeline.note_inference_start(kind=self.kind, step_s=self.step_s)
        except Exception:  # noqa: BLE001 - telemetry must not affect control
            return None

    def _note_end(self, token: int | None, *, ok: bool) -> None:
        if self.timeline is None:
            return
        try:
            self.timeline.note_inference_end(token, ok=ok, steps=1)
        except Exception:  # noqa: BLE001, S110 - telemetry must not affect control
            pass

    def submit(self, observation: dict[str, Any], fallback_joints: dict[str, float]) -> bool:
        if self._stop.is_set():
            return False
        try:
            self._requests.put_nowait((observation, fallback_joints))
            return True
        except queue.Full:
            return False

    def latest(self) -> tuple[float, Any, str | None, list[dict[str, float]]] | None:
        try:
            return self._results.get_nowait()
        except queue.Empty:
            return None

    def stop_async(self) -> threading.Event:
        self._stop.set()
        threading.Thread(target=self._finish, name="stop-monitor-policy", daemon=True).start()
        return self.stopped

    def _finish(self) -> None:
        self._thread.join()
        try:
            self.engine.stop()
        except Exception:
            pass
        finally:
            self.stopped.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                observation, fallback = self._requests.get(timeout=0.05)
            except queue.Empty:
                continue
            started = time.perf_counter()
            token = self._note_start()
            try:
                with self.inference_lock:
                    if self.prepare is not None:
                        observation = self.prepare(observation)
                    if self._stop.is_set():
                        break
                    action = self.engine.get_action(observation)
                    if action is not None and self.convert is not None:
                        action = self.convert(action, fallback)
                    preview = self.preview(fallback) if self.preview is not None else []
                error = None
            except Exception as exc:  # noqa: BLE001 - report on the bus thread
                action = None
                preview = []
                error = f"{type(exc).__name__}: {exc}"
            self._note_end(token, ok=error is None)
            if self._stop.is_set():
                break
            result = ((time.perf_counter() - started) * 1000.0, action, error, preview)
            try:
                self._results.put_nowait(result)
            except queue.Full:
                try:
                    self._results.get_nowait()
                except queue.Empty:
                    pass
                self._results.put_nowait(result)
