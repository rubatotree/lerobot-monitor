"""Missing Record slots remain visible while Dataset FPS stays fixed."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from lerobot_monitor.record_dataset import RecordDatasetSession, RecordSample
from lerobot_monitor.types import JOINT_ORDER


def test_ten_second_episode_fills_only_missing_slots(tmp_path: Path, monkeypatch) -> None:
    published: list[dict] = []

    def capture_publish(_self: RecordDatasetSession, folder: Path) -> None:
        rows = [json.loads(line) for line in (folder / "quality.jsonl").read_text().splitlines()]
        published.extend(rows)

    monkeypatch.setattr(RecordDatasetSession, "_publish", capture_publish)
    done = threading.Event()
    session = RecordDatasetSession(tmp_path / "dataset", "dataset", "local/demo", "demo", {}, 15, 1, False, done.set)
    attempt = session.new_attempt()
    pose = {joint: 1.0 for joint in JOINT_ORDER}
    session.add_sample(RecordSample(
        attempt, pose, pose, {}, slot_index=0, capture_elapsed_s=0.0,
        camera_frame_ids={"main": 7}, camera_received_elapsed_s={"main": -0.02},
    ))
    session.mark_missing(attempt, 1, "sampling_deadline_missed")
    session.add_sample(RecordSample(attempt, pose, pose, {}, slot_index=2, capture_elapsed_s=2 / 15))
    session.set_expected_slots(attempt, 150)
    session.seal(attempt)
    session.accept(attempt)
    session.stop()
    assert done.wait(20), session.progress()
    assert len(published) == 150
    assert published[0]["filled"] is False
    assert published[0]["camera_frame_ids"] == {"main": 7}
    assert published[0]["camera_received_elapsed_s"] == {"main": -0.02}
    assert published[1]["filled"] is True
    assert published[1]["missing_reason"] == "sampling_deadline_missed"
    assert published[1]["source_frame_index"] == 0
    assert published[2]["filled"] is False
    assert published[-1]["expected_t_s"] == pytest.approx(149 / 15, abs=1e-6)
    assert sum(row["filled"] for row in published) == 148
