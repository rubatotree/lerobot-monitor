"""Single-owner worker for synchronous policy inference."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from .rollout_timeline import RolloutTimeline

logger = logging.getLogger(__name__)


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
        self._requests: queue.Queue[tuple[dict[str, Any], dict[str, float]]] = (
            queue.Queue(maxsize=1)
        )
        self._results: queue.Queue[
            tuple[float, Any, str | None, list[dict[str, float]]]
        ] = queue.Queue(maxsize=1)
        self.result_ready = threading.Event()
        self.latest_token: int | None = None
        self.latest_index: int | None = None
        self._result_tokens: dict[int, tuple[int | None, int]] = {}
        self._native_events = hasattr(engine, "chunk_observer")
        self._generated_this_request = False
        self._request_started = self._lock_acquired = self._prepared_at = 0.0
        self._native_token: int | None = None
        if self._native_events:
            engine.chunk_observer = self._observe_native_chunk
        self._stop = threading.Event()
        self.stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="monitor-policy-inference", daemon=True
        )
        self._thread.start()

    def _note_start(self) -> int | None:
        if self.timeline is None or self._native_events:
            return None
        try:
            return self.timeline.note_inference_start(
                kind=self.kind, step_s=self.step_s
            )
        except Exception:  # noqa: BLE001 - telemetry must not affect control
            return None

    def _note_end(self, token: int | None, *, ok: bool) -> None:
        if self.timeline is None:
            return
        try:
            self.timeline.note_inference_end(
                token, ok=ok, steps=None if self._native_events else 1
            )
        except Exception:  # noqa: BLE001, S110 - telemetry must not affect control
            pass

    def submit(
        self, observation: dict[str, Any], fallback_joints: dict[str, float]
    ) -> bool:
        if self._stop.is_set():
            return False
        try:
            self._requests.put_nowait((observation, fallback_joints))
            return True
        except queue.Full:
            return False

    def latest(self) -> tuple[float, Any, str | None, list[dict[str, float]]] | None:
        # Clear before checking the queue: a later publication keeps its wakeup.
        self.result_ready.clear()
        try:
            result = self._results.get_nowait()
        except queue.Empty:
            return None
        self.latest_token, self.latest_index = self._result_tokens.pop(
            id(result), (None, 0)
        )
        if (
            self.timeline is not None
            and result[1] is not None
            and not self._native_events
        ):
            try:
                self.timeline.note_consumed(self.latest_token, index=0)
            except Exception:
                logger.debug("Could not record worker telemetry", exc_info=True)
        return result

    def stop_async(self) -> threading.Event:
        self._stop.set()
        threading.Thread(
            target=self._finish, name="stop-monitor-policy", daemon=True
        ).start()
        return self.stopped

    def _finish(self) -> None:
        self._thread.join()
        try:
            self.engine.stop()
        except Exception:
            logger.debug("Could not stop inference engine", exc_info=True)
        finally:
            self.stopped.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                observation, fallback = self._requests.get(timeout=0.05)
            except queue.Empty:
                continue
            started = time.perf_counter()
            self._request_started = started
            self._generated_this_request = not self._native_events
            self._native_token = None
            token = self._note_start()
            try:
                with self.inference_lock:
                    acquired = time.perf_counter()
                    self._lock_acquired = acquired
                    self._stage(token, "lock_wait", started, acquired)
                    profiler = getattr(self.engine, "profiler", None)
                    if profiler is not None:
                        profiler.chunk_id = token
                        profiler.observer = (
                            (
                                lambda stage: self.timeline.note_stage(
                                    stage.chunk_id, stage
                                )
                            )
                            if self.timeline
                            else None
                        )
                    if self.prepare is not None:
                        prepared = time.perf_counter()
                        observation = self.prepare(observation)
                        self._stage(
                            token, "observation_capture", prepared, time.perf_counter()
                        )
                    self._prepared_at = time.perf_counter()
                    if self._stop.is_set():
                        break
                    action = self.engine.get_action(observation)
                    if self._native_events:
                        token = self.engine.dispatched_chunk_id
                    converted = time.perf_counter()
                    if action is not None and self.convert is not None:
                        action = self.convert(action, fallback)
                    preview = self.preview(fallback) if self.preview is not None else []
                    if self._generated_this_request:
                        self._stage(
                            token, "action_prepare", converted, time.perf_counter()
                        )
                error = None
            except Exception as exc:  # noqa: BLE001 - report on the bus thread
                action = None
                preview = []
                error = f"{type(exc).__name__}: {exc}"
            if not self._native_events or error:
                self._note_end(
                    token if token is not None else self._native_token,
                    ok=error is None and action is not None,
                )
            published = time.perf_counter()
            if (
                self.timeline is not None
                and action is not None
                and error is None
                and not self._native_events
            ):
                try:
                    self.timeline.note_chunk_accepted(token, steps=1)
                except Exception:
                    logger.debug("Could not record worker telemetry", exc_info=True)
            if self._stop.is_set():
                break
            result = ((time.perf_counter() - started) * 1000.0, action, error, preview)
            source_index = (
                (self.engine.dispatched_action_index or 0) if self._native_events else 0
            )
            self._result_tokens[id(result)] = (token, source_index)
            try:
                self._results.put_nowait(result)
            except queue.Full:
                try:
                    dropped = self._results.get_nowait()
                    self._result_tokens.pop(id(dropped), None)
                except queue.Empty:
                    pass
                self._results.put_nowait(result)
            self.result_ready.set()
            if self._generated_this_request:
                self._stage(token, "publish", published, time.perf_counter())

    def _stage(self, token: int | None, name: str, start: float, end: float) -> None:
        if self.timeline is not None:
            try:
                self.timeline.note_stage(
                    token, SimpleNamespace(name=name, start=start, end=end, gpu_ms=None)
                )
            except Exception:
                logger.debug("Could not record worker telemetry", exc_info=True)

    def _observe_native_chunk(self, event: Any) -> None:
        """Called only by the owning inference thread after cache decisions."""
        if self.timeline is None:
            return
        if event.kind == "started":
            self._generated_this_request = True
            self._native_token = event.chunk_id
            self.timeline.note_inference_start(
                kind=self.kind,
                step_s=self.step_s,
                chunk_id=event.chunk_id,
                started_at=self._request_started,
            )
            self._stage(
                event.chunk_id, "lock_wait", self._request_started, self._lock_acquired
            )
            self._stage(
                event.chunk_id,
                "observation_capture",
                self._lock_acquired,
                self._prepared_at,
            )
        elif event.kind == "ready":
            self.timeline.note_inference_end(event.chunk_id, ok=True, steps=event.steps)
            self.timeline.note_chunk_accepted(
                event.chunk_id, steps=event.steps, replaced=event.replaced
            )
        elif event.kind == "consumed":
            self.timeline.note_consumed(event.chunk_id, index=event.action_index)
        elif event.kind == "failed":
            self.timeline.note_inference_end(event.chunk_id, ok=False, steps=None)
