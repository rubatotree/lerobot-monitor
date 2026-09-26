"""Rollout timeline telemetry stays bounded and failure-safe."""

from __future__ import annotations

import threading

from lerobot_monitor.rollout_timeline import RolloutTimeline


def test_disabled_timeline_has_no_snapshot_and_clear_resets_state(monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr("lerobot_monitor.rollout_timeline.time.perf_counter", lambda: clock[0])
    timeline = RolloutTimeline()

    assert timeline.note_inference_start(kind="rtc", step_s=0.05) is None
    assert timeline.snapshot() is None

    timeline.set_enabled(True)
    token = timeline.note_inference_start(kind="rtc", step_s=0.05)
    clock[0] = 100.1
    timeline.note_inference_end(token, ok=True, steps=8)
    assert timeline.snapshot() is not None

    timeline.clear()
    assert timeline.snapshot() is None
    timeline.set_enabled(False)


def test_inference_lifecycle_and_latency(monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr("lerobot_monitor.rollout_timeline.time.perf_counter", lambda: clock[0])
    timeline = RolloutTimeline()
    timeline.set_enabled(True)

    clock[0] = 101.2
    token = timeline.note_inference_start(kind="rtc", step_s=1 / 30)
    clock[0] = 101.45
    timeline.note_inference_end(token, ok=True, steps=16)
    assert timeline.latest_latency_ms() == 250.0

    clock[0] = 101.5
    timeline.observe(qsize=10, index=0)
    timeline.observe(qsize=9, index=1)
    snapshot = timeline.snapshot()

    assert snapshot is not None
    assert snapshot["t_s"] == 1.5
    assert snapshot["step_s"] == 0.033333
    assert snapshot["blocks"] == [
        {
            "id": token,
            "kind": "rtc",
            "start": 1.2,
            "end": 1.45,
            "active": 1.5,
            "steps": 16,
            "step_s": 0.033333,
            "failed": False,
        }
    ]


def test_index_reset_marks_rtc_handoff(monkeypatch) -> None:
    clock = [50.0]
    monkeypatch.setattr("lerobot_monitor.rollout_timeline.time.perf_counter", lambda: clock[0])
    timeline = RolloutTimeline()
    timeline.set_enabled(True)
    clock[0] = 50.2
    token = timeline.note_inference_start(kind="rtc", step_s=0.05)
    clock[0] = 50.3
    timeline.note_inference_end(token, ok=True, steps=12)

    clock[0] = 50.4
    timeline.observe(qsize=2, index=5)
    clock[0] = 50.5
    timeline.observe(qsize=9, index=0)

    snapshot = timeline.snapshot()
    assert snapshot is not None
    assert snapshot["blocks"][0]["active"] == 0.5


def test_empty_queue_is_not_a_handoff(monkeypatch) -> None:
    clock = [20.0]
    monkeypatch.setattr("lerobot_monitor.rollout_timeline.time.perf_counter", lambda: clock[0])
    timeline = RolloutTimeline()
    timeline.set_enabled(True)
    token = timeline.note_inference_start(kind="rtc", step_s=0.05)
    clock[0] = 20.1
    timeline.note_inference_end(token, ok=True, steps=4)

    timeline.observe(qsize=0, index=0)
    timeline.observe(qsize=0, index=0)
    snapshot = timeline.snapshot()

    assert snapshot is not None
    assert snapshot["blocks"][0]["active"] is None


def test_sync_worker_does_not_double_count_nested_rtc_chunk(monkeypatch) -> None:
    clock = [30.0]
    monkeypatch.setattr("lerobot_monitor.rollout_timeline.time.perf_counter", lambda: clock[0])
    timeline = RolloutTimeline()
    timeline.set_enabled(True)
    sync_token = timeline.note_inference_start(kind="sync", step_s=0.05)

    clock[0] = 30.01
    nested_token = timeline.note_inference_start(kind="rtc", step_s=0.05)
    assert nested_token is None

    clock[0] = 30.02
    timeline.note_inference_end(sync_token, ok=True, steps=1)
    clock[0] = 30.03
    timeline.note_chunk_ready(steps=1, step_s=0.05)
    snapshot = timeline.snapshot()

    assert snapshot is not None
    assert len(snapshot["blocks"]) == 1
    assert snapshot["blocks"][0]["kind"] == "sync"
    assert snapshot["blocks"][0]["active"] == 0.03


def test_block_limit_drops_old_unactivated_blocks(monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr("lerobot_monitor.rollout_timeline.time.perf_counter", lambda: clock[0])
    timeline = RolloutTimeline(max_blocks=2)
    timeline.set_enabled(True)

    first = timeline.note_inference_start(kind="rtc", step_s=0.05)
    clock[0] = 0.1
    timeline.note_inference_end(first, ok=True, steps=2)
    clock[0] = 0.2
    timeline.note_chunk_ready(steps=2, step_s=0.05)
    clock[0] = 0.3
    second = timeline.note_inference_start(kind="rtc", step_s=0.05)
    clock[0] = 0.4
    timeline.note_inference_end(second, ok=True, steps=3)
    clock[0] = 0.5
    third = timeline.note_inference_start(kind="rtc", step_s=0.05)
    clock[0] = 0.6
    timeline.note_inference_end(third, ok=True, steps=4)

    snapshot = timeline.snapshot()
    assert snapshot is not None
    assert [block["id"] for block in snapshot["blocks"]] == [first, third]


def test_window_keeps_recent_and_active_blocks(monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr("lerobot_monitor.rollout_timeline.time.perf_counter", lambda: clock[0])
    timeline = RolloutTimeline(window_s=1.0)
    timeline.set_enabled(True)

    old = timeline.note_inference_start(kind="rtc", step_s=0.05)
    clock[0] = 0.1
    timeline.note_inference_end(old, ok=True, steps=2)
    clock[0] = 2.0
    recent = timeline.note_inference_start(kind="rtc", step_s=0.05)
    clock[0] = 2.1
    timeline.note_inference_end(recent, ok=True, steps=3)
    clock[0] = 2.2
    timeline.note_chunk_ready(steps=3, step_s=0.05)

    snapshot = timeline.snapshot()
    assert snapshot is not None
    assert [block["id"] for block in snapshot["blocks"]] == [recent]


def test_concurrent_inference_events_are_failure_safe() -> None:
    timeline = RolloutTimeline(max_blocks=40)
    timeline.set_enabled(True)
    start = threading.Barrier(3)

    def worker(kind: str) -> None:
        start.wait()
        for _ in range(30):
            token = timeline.note_inference_start(kind=kind, step_s=0.05)
            timeline.note_inference_end(token, ok=True, steps=4)
            timeline.latest_latency_ms()

    threads = [threading.Thread(target=worker, args=(kind,)) for kind in ("rtc", "sync")]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join()

    snapshot = timeline.snapshot()
    assert snapshot is not None
    assert len(snapshot["blocks"]) <= 40
