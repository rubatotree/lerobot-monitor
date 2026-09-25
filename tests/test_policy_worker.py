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
