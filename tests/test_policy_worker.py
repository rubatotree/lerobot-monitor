"""Slow synchronous inference stays outside the control loop."""

from __future__ import annotations

import threading
import time

import pytest

from lerobot_monitor.policy_worker import PolicyWorker


def test_result_notification_is_set_and_consumption_clears_it() -> None:
    class Engine:
        def get_action(self, _observation: dict) -> dict[str, float]:
            return {"gripper": 1.0}

        def stop(self) -> None:
            pass

    worker = PolicyWorker(Engine(), threading.Lock())
    try:
        assert worker.submit({}, {})
        assert worker.result_ready.wait(2)
        assert worker.latest() is not None
        assert not worker.result_ready.is_set()
    finally:
        assert worker.stop_async().wait(2)


def test_result_published_around_empty_read_keeps_wakeup(monkeypatch: pytest.MonkeyPatch) -> None:
    import queue
    from types import SimpleNamespace

    worker = PolicyWorker(SimpleNamespace(stop=lambda: None), threading.Lock())
    original_get = worker._results.get_nowait
    result = (1.0, {"gripper": 1.0}, None, [])

    def race() -> None:
        # A producer publishes immediately after an empty observation. Its
        # notification must survive the consumer's empty-result path.
        worker._results.put_nowait(result)
        worker.result_ready.set()
        raise queue.Empty

    try:
        monkeypatch.setattr(worker._results, "get_nowait", race)
        assert worker.latest() is None
        assert worker.result_ready.is_set()
        monkeypatch.setattr(worker._results, "get_nowait", original_get)
        assert worker.latest() == result
        assert not worker.result_ready.is_set()
    finally:
        assert worker.stop_async().wait(2)


def test_camera_preparation_and_conversion_run_off_the_submitter_thread() -> None:
    from typing import Any

    entered, release = threading.Event(), threading.Event()
    threads: list[int] = []

    def prepare(observation: dict[str, Any]) -> dict[str, Any]:
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        return observation

    def convert(action: Any, joints: dict[str, float]) -> dict[str, float]:
        threads.append(threading.get_ident())
        return {"gripper": float(action)}

    class Engine:
        def get_action(self, observation: dict[str, Any]) -> float:
            return 1.0

        def stop(self) -> None:
            pass

    worker = PolicyWorker(Engine(), threading.Lock(), prepare=prepare, convert=convert)
    try:
        assert worker.submit({}, {})
        assert entered.wait(2)
        assert worker.latest() is None
        # The blocked camera does not block the caller or create an unbounded backlog.
        assert worker.submit({}, {})
        assert not worker.submit({}, {})
        release.set()
        deadline = time.perf_counter() + 2
        result = None
        while result is None and time.perf_counter() < deadline:
            result = worker.latest()
            time.sleep(0.005)
        assert result is not None and result[1] == {"gripper": 1.0}
        assert all(identifier != threading.get_ident() for identifier in threads)
    finally:
        release.set()
        assert worker.stop_async().wait(2)


def test_slow_inference_does_not_block_submit_or_stop() -> None:
    started = threading.Event()
    release = threading.Event()
    stopped = threading.Event()

    class Engine:
        def get_action(self, _observation: dict) -> dict[str, float]:
            started.set()
            release.wait(5)
            return {"gripper": 1.0}

        def stop(self) -> None:
            stopped.set()

    worker = PolicyWorker(Engine(), threading.Lock())
    assert worker.submit({"state": 0}, {"gripper": 0.0})
    assert started.wait(2)
    before = time.perf_counter()
    assert worker.latest() is None
    fully_stopped = worker.stop_async()
    assert time.perf_counter() - before < 0.2
    assert not fully_stopped.is_set()
    release.set()
    assert stopped.wait(2)
    assert fully_stopped.wait(2)
    assert worker.latest() is None


def test_policy_worker_records_successful_inference() -> None:
    class Timeline:
        def __init__(self) -> None:
            self.starts: list[tuple[str, float]] = []
            self.ends: list[tuple[int | None, bool, int | None]] = []
            self.ended = threading.Event()

        def note_inference_start(self, *, kind: str, step_s: float) -> int:
            self.starts.append((kind, step_s))
            return 11

        def note_inference_end(
            self,
            token: int | None,
            *,
            ok: bool,
            steps: int | None,
        ) -> None:
            self.ends.append((token, ok, steps))
            self.ended.set()

    class Engine:
        def get_action(self, _observation: dict) -> dict[str, float]:
            return {"gripper": 1.0}

        def stop(self) -> None:
            pass

    timeline = Timeline()
    worker = PolicyWorker(
        Engine(),
        threading.Lock(),
        timeline=timeline,
        step_s=0.05,
    )
    assert worker.submit({"state": 0}, {"gripper": 0.0})
    assert timeline.ended.wait(2)

    result = None
    deadline = time.perf_counter() + 1.0
    while result is None and time.perf_counter() < deadline:
        result = worker.latest()
        time.sleep(0.01)

    assert result is not None
    assert len(result) == 4
    assert result[2] is None
    assert timeline.starts == [("sync", 0.05)]
    assert timeline.ends == [(11, True, 1)]
    worker.stop_async().wait(2)


def test_policy_worker_records_failed_inference() -> None:
    class Timeline:
        def __init__(self) -> None:
            self.ends: list[tuple[int | None, bool, int | None]] = []
            self.ended = threading.Event()

        def note_inference_start(self, *, kind: str, step_s: float) -> int:
            return 12

        def note_inference_end(
            self,
            token: int | None,
            *,
            ok: bool,
            steps: int | None,
        ) -> None:
            self.ends.append((token, ok, steps))
            self.ended.set()

    class Engine:
        def get_action(self, _observation: dict) -> dict[str, float]:
            raise RuntimeError("inference failed")

        def stop(self) -> None:
            pass

    timeline = Timeline()
    worker = PolicyWorker(Engine(), threading.Lock(), timeline=timeline)
    assert worker.submit({"state": 0}, {"gripper": 0.0})
    assert timeline.ended.wait(2)

    result = None
    deadline = time.perf_counter() + 1.0
    while result is None and time.perf_counter() < deadline:
        result = worker.latest()
        time.sleep(0.01)

    assert result is not None
    assert len(result) == 4
    assert result[2] == "RuntimeError: inference failed"
    assert timeline.ends == [(12, False, 1)]
    worker.stop_async().wait(2)

def test_native_ready_event_records_predicted_plan_steps() -> None:
    from types import SimpleNamespace

    from lerobot_monitor.rollout_timeline import RolloutTimeline

    class NativeEngine:
        def __init__(self) -> None:
            self.chunk_observer = None
            self.dispatched_chunk_id = None
            self.dispatched_action_index = None
            self._sequence = 0

        def get_action(self, _observation: dict) -> dict[str, float]:
            self._sequence += 1
            chunk_id = self._sequence
            self.chunk_observer(SimpleNamespace(kind="started", chunk_id=chunk_id))
            self.chunk_observer(
                SimpleNamespace(kind="ready", chunk_id=chunk_id, steps=1, replaced=())
            )
            self.chunk_observer(
                SimpleNamespace(kind="consumed", chunk_id=chunk_id, action_index=0)
            )
            self.dispatched_chunk_id = chunk_id
            self.dispatched_action_index = 0
            return {"gripper": float(chunk_id)}

        def stop(self) -> None:
            pass

    timeline = RolloutTimeline()
    timeline.set_enabled(True)
    worker = PolicyWorker(
        NativeEngine(),
        threading.Lock(),
        timeline=timeline,
        step_s=1 / 15,
        predicted_steps=lambda: 50,
    )
    try:
        assert worker.submit({}, {"gripper": 0.0})
        result = None
        deadline = time.perf_counter() + 2
        while result is None and time.perf_counter() < deadline:
            result = worker.latest()
            time.sleep(0.005)
        assert result is not None and result[1] == {"gripper": 1.0}
    finally:
        assert worker.stop_async().wait(2)

    blocks = timeline.snapshot()["blocks"]
    assert len(blocks) == 1
    assert blocks[0]["steps"] == 1
    assert blocks[0]["accepted_steps"] == 1
    assert blocks[0]["predicted_steps"] == 50


def test_manual_accept_records_plan_steps_from_preview() -> None:
    from lerobot_monitor.rollout_timeline import RolloutTimeline

    class Engine:
        def get_action(self, _observation: dict) -> dict[str, float]:
            return {"gripper": 1.0}

        def stop(self) -> None:
            pass

    timeline = RolloutTimeline()
    timeline.set_enabled(True)
    worker = PolicyWorker(
        Engine(),
        threading.Lock(),
        timeline=timeline,
        step_s=1 / 15,
        preview=lambda _joints: [{"gripper": 2.0}, {"gripper": 3.0}],
    )
    try:
        assert worker.submit({}, {"gripper": 0.0})
        result = None
        deadline = time.perf_counter() + 2
        while result is None and time.perf_counter() < deadline:
            result = worker.latest()
            time.sleep(0.005)
        assert result is not None
    finally:
        assert worker.stop_async().wait(2)

    block = timeline.snapshot()["blocks"][0]
    assert block["steps"] == 1
    assert block["predicted_steps"] == 3
