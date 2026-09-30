"""Native queue provenance and optional timing never alter action delivery."""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

# The native engine needs torch; the monitor's slim venv skips this file instead of
# failing collection (same convention as test_native_rtc.py).
torch = pytest.importorskip("torch")

from lerobot.policies.rtc.action_queue import ActionQueue
from lerobot.policies.rtc.configuration_rtc import RTCConfig
from lerobot.rollout.inference.profiling import PipelineProfiler
from lerobot.rollout.inference.sync import SyncInferenceEngine

from lerobot_monitor.policy_worker import PolicyWorker
from lerobot_monitor.rollout_timeline import RolloutTimeline


class _ChunkPolicy:
    def __init__(self, *, kind: str = "smolvla", ensemble: bool = False) -> None:
        self.config = SimpleNamespace(
            type=kind, temporal_ensemble_coeff=0.1 if ensemble else None, use_amp=False
        )
        self._action_queue_attrs = ("_queues",)
        self._queues: dict[str, deque[torch.Tensor]] = {"action": deque()}
        self.calls = 0

    def select_action(self, _observation: dict[str, Any]) -> torch.Tensor:
        self.calls += 1
        cache = self._queues["action"]
        if not cache:
            cache.extend(
                torch.ones(1, 2) * index for index in range(3)
            )  # Three [1,A] actions.
        return cache.popleft()

    def drop_queued_actions(self) -> None:
        self._queues["action"].clear()

    def reset(self) -> None:
        self.drop_queued_actions()


class _IdentityProcessor:
    def __call__(self, value: Any) -> Any:
        return value

    def reset(self) -> None:
        pass


def _sync_engine(
    *, kind: str = "smolvla", ensemble: bool = False
) -> SyncInferenceEngine:
    return SyncInferenceEngine(
        policy=_ChunkPolicy(kind=kind, ensemble=ensemble),
        preprocessor=_IdentityProcessor(),
        postprocessor=_IdentityProcessor(),
        dataset_features={
            "action": {"dtype": "float32", "shape": (2,), "names": ["j1.pos", "j2.pos"]}
        },
        ordered_action_keys=["j1.pos", "j2.pos"],
        task="a",
        device="cpu",
        robot_type="mock",
    )


def test_sync_chunk_provenance_and_cached_profiling_with_worker() -> None:
    engine = _sync_engine()
    timeline = RolloutTimeline()
    timeline.set_enabled(True)
    worker = PolicyWorker(engine, threading.Lock(), timeline=timeline)
    provenance: list[tuple[int, int]] = []
    first_inference: dict[str, Any] | None = None
    try:
        for iteration in range(4):
            assert worker.submit(
                {"observation.state": np.zeros(2, dtype=np.float32)}, {}
            )
            deadline = time.perf_counter() + 2.0
            result = None
            while result is None and time.perf_counter() < deadline:
                result = worker.latest()
                time.sleep(0.001)
            assert result is not None and result[2] is None
            provenance.append((worker.latest_token, worker.latest_index))
            timeline.note_dispatched(worker.latest_token, index=worker.latest_index)
            block = timeline.snapshot()["blocks"][0]
            if iteration == 0:
                first_inference = block
            elif iteration < 3:
                assert block["end"] == first_inference["end"]
                assert block["stages"] == first_inference["stages"]
        assert provenance == [(1, 0), (1, 1), (1, 2), (2, 0)]
        blocks = timeline.snapshot()["blocks"]
        assert len(blocks) == 2
        assert (
            blocks[0]["accepted_steps"]
            == blocks[0]["consumed_steps"]
            == blocks[0]["dispatched_steps"]
            == 3
        )
        assert engine._policy.calls == 4
    finally:
        assert worker.stop_async().wait(2)


def test_sync_task_change_and_reset_keep_monotonic_chunk_ids() -> None:
    engine = _sync_engine()
    events = []
    engine.chunk_observer = events.append
    observation = {"observation.state": np.zeros(2, dtype=np.float32)}
    engine.get_action(observation)
    engine.set_task("b")
    engine.get_action(observation)
    ready = [event for event in events if event.kind == "ready"]
    assert ready[-1].chunk_id == 2 and ready[-1].replaced == ((1, 2),)
    assert engine.dispatched_action_index == 0
    engine.reset()
    engine.get_action(observation)
    assert engine.dispatched_chunk_id == 3 and engine.dispatched_action_index == 0


@pytest.mark.parametrize("kind,ensemble", [("act", True), ("custom", False)])
def test_sync_unverified_queue_and_temporal_ensemble_use_single_steps(
    kind: str, ensemble: bool
) -> None:
    engine = _sync_engine(kind=kind, ensemble=ensemble)
    events = []
    engine.chunk_observer = events.append
    for _ in range(3):
        engine.get_action({"observation.state": np.zeros(2, dtype=np.float32)})
    ready = [event for event in events if event.kind == "ready"]
    assert [event.chunk_id for event in ready] == [1, 2, 3]
    assert all(event.steps == 1 for event in ready)


def test_receipt_reports_applied_trim_and_exact_removed_tails() -> None:
    queue = ActionQueue(RTCConfig(enabled=True))
    actions = torch.ones(5, 2)  # [H, A].
    first = queue.merge(actions, actions, 0, task="task", chunk_id=1)
    assert first.original_steps == first.accepted_steps == 5
    assert first.prefix_trimmed == 0 and first.replaced == ()
    assert queue.get_with_provenance()[3] == 0
    assert queue.get_with_provenance()[3] == 1
    replacement = queue.merge(actions, actions, 2, task="task", chunk_id=2)
    assert replacement.replaced == ((1, 3),)
    assert replacement.original_steps == 5 and replacement.prefix_trimmed == 2
    assert replacement.accepted_steps == 3
    assert (
        replacement.lock_started <= replacement.lock_acquired <= replacement.timestamp
    )
    assert queue.get_with_provenance()[2:] == (2, 0)
    empty = queue.merge(actions, actions, 100, task="task", chunk_id=3)
    assert empty.prefix_trimmed == 5 and empty.accepted_steps == 0
    assert empty.replaced == ((2, 2),)
    assert queue.get_with_provenance() is None
    queue.clear()
    restarted = queue.merge(actions, actions, 0, task="task", chunk_id=4)
    assert restarted.replaced == ()


def test_append_preserves_source_indices_and_legacy_triple_api() -> None:
    queue = ActionQueue(RTCConfig(enabled=False))
    actions = torch.ones(3, 2)  # [H, A].
    queue.merge(actions, actions, 100, task="a", chunk_id=1)
    assert len(queue.get_with_metadata()) == 3
    receipt = queue.merge(actions, actions, 100, task="b", chunk_id=2)
    assert receipt.prefix_trimmed == 0 and receipt.accepted_steps == 3
    assert receipt.replaced == ()
    assert queue.get_with_provenance()[2:] == (1, 1)
    assert queue.get_with_provenance()[2:] == (1, 2)
    assert queue.get_with_provenance()[2:] == (2, 0)


def test_profiler_failure_isolated_and_stage_recorded_on_failure() -> None:
    profiler = PipelineProfiler()
    records = []
    profiler.observer = records.append
    profiler.chunk_id = 7
    with pytest.raises(ValueError), profiler.stage("model", "cpu"):
        raise ValueError("model error")
    assert records[0].chunk_id == 7 and records[0].end >= records[0].start

    def broken_observer(_stage: object) -> None:
        raise RuntimeError("chart error")

    profiler.observer = broken_observer
    with profiler.stage("model", "cpu"):
        pass


def test_cuda_timing_queries_without_waiting_and_bounds_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = [False]

    class Event:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def record(self) -> None:
            pass

        def query(self) -> bool:
            return ready[0]

        def elapsed_time(self, _other: Event) -> float:
            assert ready[0]
            return 4.0

        def synchronize(self) -> None:
            pytest.fail("profiling must never synchronize")

    monkeypatch.setattr(torch.cuda, "Event", Event)
    monkeypatch.setattr(torch.cuda, "device", lambda _device: nullcontext())
    profiler = PipelineProfiler()
    records = []
    profiler.observer = records.append
    for _ in range(150):
        with profiler.stage("model", "cuda"):
            pass
    assert len(profiler._pending) == 128
    assert all(stage.gpu_ms is None for stage in records)
    ready[0] = True
    profiler.poll()
    assert len(profiler._pending) == 0
    assert records[-1].gpu_ms == 4.0
