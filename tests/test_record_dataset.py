"""Exercise the real dataset writer used by Record, including video readback."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from lerobot_monitor import dataset_hub
from lerobot_monitor.pathutil import ensure_lerobot_on_path
from lerobot_monitor.record_dataset import RecordDatasetSession, RecordSample
from lerobot_monitor.types import JOINT_ORDER


def test_confirmed_episodes_append_to_selected_dataset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setenv("HF_DATASETS_CACHE", str(tmp_path / "hf" / "datasets"))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(dataset_hub, "lerobot_home", lambda: tmp_path / "lerobot")
    ensure_lerobot_on_path()
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    created = dataset_hub.create_empty_dataset(
        name="operator-record", fps=5, robot_type="so101_follower",
        cameras=[{"key": "front", "width": 64, "height": 48}],
    )
    root = Path(created["path"])
    done = threading.Event()
    camera_keys = {"front": "observation.images.front"}
    assert RecordDatasetSession.validate(root, camera_keys, 5)["total_episodes"] == 0
    session = RecordDatasetSession(root, created["id"], created["repo_id"], "pick blocks", camera_keys, 5, 1, False, done.set)
    try:
        for episode in range(2):
            attempt = session.new_attempt()
            for frame in range(4):
                color = np.full((48, 64, 3), 50 + episode * 100, dtype=np.uint8)
                ok, jpeg = cv2.imencode(".jpg", color)
                assert ok
                pose = {joint: float(episode * 10 + frame) for joint in JOINT_ORDER}
                session.add_sample(RecordSample(attempt, pose, pose, {"front": jpeg.tobytes()}))
            session.seal(attempt)
            session.accept(attempt)
        session.stop()
        assert done.wait(timeout=90), session.progress()
        assert session.progress()["error"] is None
        dataset = LeRobotDataset(repo_id=created["repo_id"], root=root, video_backend="pyav")
        assert dataset.num_episodes == 2
        assert dataset.num_frames == 8
        assert dataset[7]["action"].tolist() == [13.0] * len(JOINT_ORDER)
        assert tuple(dataset[7]["observation.images.front"].shape) == (3, 48, 64)
        assert abs(float(dataset[7]["observation.images.front"].mean()) * 255 - 150) < 15
        assert float(dataset[0]["timestamp"]) == 0.0
        assert abs(float(dataset[3]["timestamp"]) - 0.6) < 0.01
        assert float(dataset[4]["timestamp"]) == 0.0
        info = json.loads((root / "meta" / "info.json").read_text(encoding="utf-8"))
        assert info["total_episodes"] == 2
        existing = {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for parent in (root / "data", root / "videos") if parent.exists()
            for path in parent.rglob("*") if path.is_file()
        }
        continued = threading.Event()
        append = RecordDatasetSession(root, created["id"], created["repo_id"], "pick blocks", camera_keys, 5, 1, False, continued.set)
        attempt = append.new_attempt()
        color = np.full((48, 64, 3), 220, dtype=np.uint8)
        ok, jpeg = cv2.imencode(".jpg", color)
        assert ok
        pose = {joint: 22.0 for joint in JOINT_ORDER}
        append.add_sample(RecordSample(attempt, pose, pose, {"front": jpeg.tobytes()}))
        append.seal(attempt)
        append.accept(attempt)
        append.stop()
        assert continued.wait(timeout=90), append.progress()
        assert append.progress()["error"] is None
        appended = LeRobotDataset(repo_id=created["repo_id"], root=root, video_backend="pyav")
        assert appended.num_episodes == 3
        assert appended.num_frames == 9
        assert appended[8]["action"].tolist() == [22.0] * len(JOINT_ORDER)
        assert all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest for name, digest in existing.items())
    finally:
        session.stop()


def test_discarded_attempt_does_not_enter_dataset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setenv("HF_DATASETS_CACHE", str(tmp_path / "hf" / "datasets"))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(dataset_hub, "lerobot_home", lambda: tmp_path / "lerobot")
    row = dataset_hub.create_empty_dataset(name="discard-plan", fps=5, cameras=[])
    root = Path(row["path"])
    done = threading.Event()
    session = RecordDatasetSession(root, row["id"], row["repo_id"], "pick", {}, 5, 1, False, done.set)
    attempt = session.new_attempt()
    pose = {joint: 0.0 for joint in JOINT_ORDER}
    session.add_sample(RecordSample(attempt, pose, pose, {}))
    session.seal(attempt, discard=True)
    session.stop()
    assert done.wait(timeout=10)
    info = json.loads((root / "meta" / "info.json").read_text(encoding="utf-8"))
    assert info["total_episodes"] == 0


def test_failed_publish_can_retry_without_duplicate_episode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dataset_hub, "lerobot_home", lambda: tmp_path / "lerobot")
    row = dataset_hub.create_empty_dataset(name="retry-plan", fps=5, cameras=[])
    root = Path(row["path"])
    done = threading.Event()
    session = RecordDatasetSession(root, row["id"], row["repo_id"], "pick", {}, 5, 1, False, done.set)
    real_commit = session._commit
    calls = 0

    def fail_once(stage: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected disk failure")
        real_commit(stage)

    monkeypatch.setattr(session, "_commit", fail_once)
    attempt = session.new_attempt()
    pose = {joint: 4.0 for joint in JOINT_ORDER}
    session.add_sample(RecordSample(attempt, pose, pose, {}))
    session.seal(attempt)
    session.accept(attempt)
    session.stop()
    deadline = time.monotonic() + 30
    while session.progress()["status"] != "error" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert session.progress()["status"] == "error"
    assert session.progress()["saved"] == 0
    assert not done.is_set()
    session.retry()
    assert done.wait(timeout=60), session.progress()
    assert session.progress()["saved"] == 1
    info = json.loads((root / "meta" / "info.json").read_text(encoding="utf-8"))
    assert info["total_episodes"] == 1
