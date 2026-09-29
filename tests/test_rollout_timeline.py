"""Rollout timeline telemetry stays bounded and failure-safe."""

from __future__ import annotations

import threading

import pytest

from lerobot_monitor.rollout_timeline import RolloutTimeline


def test_explicit_rtc_events_do_not_activate_unpublished_or_discarded_chunks() -> None:
    timeline = RolloutTimeline()
    timeline.set_enabled(True)
    token = timeline.note_inference_start(kind="rtc", step_s=0.05, chunk_id=17)
    timeline.note_inference_end(token, ok=True, steps=50)
    timeline.note_dispatched(token)
    assert timeline.snapshot()["blocks"][0]["active"] is None
    timeline.note_chunk_accepted(token, steps=42)
    timeline.note_dispatched(token)
    assert timeline.snapshot()["blocks"][0]["active"] is not None
    discarded = timeline.note_inference_start(kind="rtc", step_s=0.05, chunk_id=18)
    timeline.note_inference_end(discarded, ok=False, steps=50)
    timeline.note_chunk_accepted(discarded, steps=42)
    timeline.note_dispatched(discarded)
    assert timeline.snapshot()["blocks"][1]["active"] is None


def test_predicted_steps_are_plumbed_into_the_snapshot() -> None:
    timeline = RolloutTimeline()
    timeline.set_enabled(True)
    token = timeline.note_inference_start(kind="sync", step_s=1 / 15)
    timeline.note_inference_end(token, ok=True, steps=1, predicted_steps=50)
    timeline.note_chunk_accepted(token, steps=1, predicted_steps=50)
    block = timeline.snapshot()["blocks"][0]
    assert block["steps"] == 1
    assert block["accepted_steps"] == 1
    assert block["original_steps"] == 1
    assert block["predicted_steps"] == 50

    plain = timeline.note_inference_start(kind="sync", step_s=1 / 15)
    timeline.note_inference_end(plain, ok=True, steps=3)
    timeline.note_chunk_accepted(plain, steps=3)
    block = timeline.snapshot()["blocks"][1]
    assert block["steps"] == 3
    assert block["predicted_steps"] is None


def test_disabled_timeline_has_no_snapshot_and_clear_resets_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(
        "lerobot_monitor.rollout_timeline.time.perf_counter", lambda: clock[0]
    )
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


def test_inference_lifecycle_and_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(
        "lerobot_monitor.rollout_timeline.time.perf_counter", lambda: clock[0]
    )
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
    expected = {
        "id": token,
        "kind": "rtc",
        "start": 1.2,
        "end": 1.45,
        "active": 1.5,
        "steps": 16,
        "step_s": 0.033333,
        "failed": False,
    }
    assert {key: snapshot["blocks"][0][key] for key in expected} == expected


def test_index_reset_marks_rtc_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [50.0]
    monkeypatch.setattr(
        "lerobot_monitor.rollout_timeline.time.perf_counter", lambda: clock[0]
    )
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


def test_empty_queue_is_not_a_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [20.0]
    monkeypatch.setattr(
        "lerobot_monitor.rollout_timeline.time.perf_counter", lambda: clock[0]
    )
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


def test_sync_worker_does_not_double_count_nested_rtc_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [30.0]
    monkeypatch.setattr(
        "lerobot_monitor.rollout_timeline.time.perf_counter", lambda: clock[0]
    )
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


def test_block_limit_drops_old_unactivated_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(
        "lerobot_monitor.rollout_timeline.time.perf_counter", lambda: clock[0]
    )
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


def test_window_keeps_recent_and_active_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(
        "lerobot_monitor.rollout_timeline.time.perf_counter", lambda: clock[0]
    )
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

    threads = [
        threading.Thread(target=worker, args=(kind,)) for kind in ("rtc", "sync")
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join()

    snapshot = timeline.snapshot()
    assert snapshot is not None
    assert len(snapshot["blocks"]) <= 40


def test_fixed_wall_epoch_and_raw_times_survive_snapshot_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [105.0]
    monkeypatch.setattr(
        "lerobot_monitor.rollout_timeline.time.perf_counter", lambda: clock[0]
    )
    monkeypatch.setattr("lerobot_monitor.rollout_timeline.time.time", lambda: 1005.0)
    timeline = RolloutTimeline()
    timeline.set_enabled(True, epoch_monotonic=100.0)
    token = timeline.note_inference_start(kind="rtc", step_s=0.05)
    clock[0] = 105.2
    timeline.note_inference_end(token, ok=True, steps=20)
    first = timeline.snapshot()
    clock[0] = 106.0
    second = timeline.snapshot()
    assert first["epoch_ts"] == second["epoch_ts"] == 1000.0
    assert first["run_id"] == second["run_id"]
    assert first["blocks"] == second["blocks"]
    assert second["blocks"][0]["start"] == 5.0
    timeline.clear()
    timeline.note_inference_start(kind="rtc", step_s=0.05)
    assert timeline.snapshot()["run_id"] != first["run_id"]


def test_actual_trim_replacement_and_dispatch_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(
        "lerobot_monitor.rollout_timeline.time.perf_counter", lambda: clock[0]
    )
    timeline = RolloutTimeline()
    timeline.set_enabled(True)
    old = timeline.note_inference_start(kind="rtc", step_s=0.1)
    timeline.note_inference_end(old, ok=True, steps=10)
    timeline.note_chunk_accepted(old, steps=8, prefix_trimmed=2)
    timeline.note_consumed(old, index=0)
    timeline.note_dispatched(old, index=0)
    clock[0] = 100.1
    timeline.note_dispatched(old, index=0)
    timeline.note_consumed(old, index=1)  # A pop without a successful send.
    new = timeline.note_inference_start(kind="rtc", step_s=0.1)
    timeline.note_inference_end(new, ok=True, steps=10)
    clock[0] = 100.2
    timeline.note_chunk_accepted(new, steps=7, prefix_trimmed=3, replaced=((old, 6),))
    block = timeline.snapshot()["blocks"][0]
    assert block["original_steps"] == 10 and block["steps"] == 8
    assert block["prefix_trimmed"] == 2 and block["replaced_steps"] == 6
    assert block["consumed_steps"] == 2 and block["dispatched_steps"] == 1
    assert block["remaining_steps"] == 0 and block["action_end"] is None
    clock[0] = 100.3
    timeline.note_consumed(new, index=0)
    timeline.note_dispatched(new, index=0)
    assert timeline.snapshot()["blocks"][0]["action_end"] == 0.3
    assert timeline.snapshot()["blocks"][0]["status"] == "replaced"


def test_dispatch_before_merge_observer_keeps_original_send_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [10.0]
    monkeypatch.setattr(
        "lerobot_monitor.rollout_timeline.time.perf_counter", lambda: clock[0]
    )
    timeline = RolloutTimeline()
    timeline.set_enabled(True)
    token = timeline.note_inference_start(kind="rtc", step_s=0.1)
    timeline.note_inference_end(token, ok=True, steps=3)
    clock[0] = 10.2
    timeline.note_consumed(token, index=0)
    timeline.note_dispatched(token, index=0)
    clock[0] = 10.3
    timeline.note_dispatched(token, index=0)
    timeline.note_chunk_accepted(token, steps=2, prefix_trimmed=1, timestamp=10.1)
    block = timeline.snapshot()["blocks"][0]
    assert block["accepted_at"] == 0.1
    assert block["active"] == 0.2
    assert block["dispatched_steps"] == 1


def test_entirely_trimmed_chunk_has_no_execution_and_preserves_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        "lerobot_monitor.rollout_timeline.time.perf_counter", lambda: 10.0
    )
    timeline = RolloutTimeline()
    timeline.set_enabled(True, epoch_monotonic=9.0)
    token = timeline.note_inference_start(kind="rtc", step_s=0.1)
    timeline.note_stage(
        token, SimpleNamespace(name="model", start=10.0, end=10.1, gpu_ms=None)
    )
    timeline.note_stage(
        token, SimpleNamespace(name="model", start=10.0, end=10.1, gpu_ms=80.0)
    )
    timeline.note_inference_end(token, ok=True, steps=3)
    timeline.note_chunk_accepted(token, steps=0, prefix_trimmed=3)
    timeline.note_dispatched(token, index=0)
    block = timeline.snapshot()["blocks"][0]
    assert block["status"] == "discarded" and block["active"] is None
    assert block["original_steps"] == 3 and block["accepted_steps"] == 0
    assert block["stages"] == [
        {"name": "model", "start": 1.0, "end": 1.1, "gpu_ms": 80.0}
    ]


@pytest.mark.parametrize("capacity,total", [(2, 4), (256, 300)])
def test_continuous_sync_retains_newest_after_history_capacity(
    capacity: int, total: int
) -> None:
    timeline = RolloutTimeline(max_blocks=capacity)
    timeline.set_enabled(True)
    for _ in range(total):
        token = timeline.note_inference_start(kind="sync", step_s=0.05)
        timeline.note_inference_end(token, ok=True, steps=1)
        timeline.note_chunk_accepted(token, steps=1)
        timeline.note_consumed(token, index=0)
        timeline.note_dispatched(token, index=0)
    blocks = timeline.snapshot()["blocks"]
    assert len(blocks) == capacity
    assert [block["id"] for block in blocks] == list(
        range(total - capacity + 1, total + 1)
    )
    assert blocks[-1]["active"] is not None
    assert blocks[-1]["dispatched_steps"] == 1
