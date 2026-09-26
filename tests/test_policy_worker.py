"""Slow synchronous inference stays outside the control loop."""

from __future__ import annotations

import threading
import time

from lerobot_monitor.policy_worker import PolicyWorker


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
