"""Single-owner worker for synchronous policy inference."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from typing import Any


class PolicyWorker:
    def __init__(
        self,
        engine: Any,
        inference_lock: threading.Lock,
        preview: Callable[[dict[str, float]], list[dict[str, float]]] | None = None,
    ) -> None:
        self.engine = engine
        self.inference_lock = inference_lock
        self.preview = preview
        self._requests: queue.Queue[tuple[dict[str, Any], dict[str, float]]] = queue.Queue(maxsize=1)
        self._results: queue.Queue[tuple[float, Any, str | None, list[dict[str, float]]]] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="monitor-policy-inference", daemon=True)
        self._thread.start()

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

    def stop_async(self) -> None:
        self._stop.set()
        threading.Thread(target=self._finish, name="stop-monitor-policy", daemon=True).start()

    def _finish(self) -> None:
        self._thread.join()
        try:
            self.engine.stop()
        except Exception:
            pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                observation, fallback = self._requests.get(timeout=0.05)
            except queue.Empty:
                continue
            started = time.perf_counter()
            try:
                with self.inference_lock:
                    action = self.engine.get_action(observation)
                    preview = self.preview(fallback) if self.preview is not None else []
                error = None
            except Exception as exc:  # noqa: BLE001 - report on the bus thread
                action = None
                preview = []
                error = f"{type(exc).__name__}: {exc}"
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
