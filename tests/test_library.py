from pathlib import Path

import numpy as np

from lerobot_monitor.library import VideoLibrary, DatasetRecorder, list_hf_datasets
from lerobot_monitor.session import mosaic_bgr
from lerobot_monitor.types import JOINT_ORDER


def test_mosaic_grid() -> None:
    a = np.zeros((20, 30, 3), dtype=np.uint8)
    b = np.ones((20, 40, 3), dtype=np.uint8) * 9
    out = mosaic_bgr({"a": a, "b": b})
    assert out is not None
    assert out.shape[0] == 20
    assert out.shape[1] >= 70


def test_dataset_create_record_reorder_delete(tmp_path: Path) -> None:
    lib = VideoLibrary(tmp_path / "videos")
    created = lib.create("blocks", fps=5, task="sort")
    pose = {name: float(i) for i, name in enumerate(JOINT_ORDER)}
    frame = np.zeros((24, 32, 3), dtype=np.uint8)
    rec = DatasetRecorder(Path(created["path"]), fps=5, kind="record", merge=True)
    rec.add_frame(pose, pose, {"front": frame}, episode_index=0)
    rec.add_frame(pose, pose, {"front": frame}, episode_index=1)
    rec.close()
    data = lib.get(created["id"])
    assert len(data["episodes"]) == 2
    preview = lib.episode_preview(created["id"], 0)
    assert preview.is_file()
    video = lib.episode_video(created["id"], 0, "merged")
    assert video.is_file()
    lib.reorder(created["id"], [1, 0])
    again = lib.get(created["id"])
    assert [e["index"] for e in again["episodes"]] == [0, 1]
    lib.delete_episode(created["id"], 0)
    leftover = lib.get(created["id"])
    assert len(leftover["episodes"]) == 1
    lib.delete(created["id"])
    assert lib.list() == []


def test_list_hf_datasets_hub_and_lerobot(tmp_path: Path, monkeypatch) -> None:
    hf = tmp_path / "hf"
    hub = hf / "hub" / "datasets--user--blocks"
    snap = hub / "snapshots" / "abc"
    (snap / "meta").mkdir(parents=True)
    (snap / "meta" / "info.json").write_text('{"fps": 15, "total_episodes": 3}\n', encoding="utf-8")
    home = hf / "lerobot" / "user" / "toy"
    (home / "meta").mkdir(parents=True)
    (home / "meta" / "info.json").write_text('{"fps": 10, "total_episodes": 2}\n', encoding="utf-8")
    extra = tmp_path / "extra" / "local_ds"
    (extra / "meta").mkdir(parents=True)
    (extra / "meta" / "info.json").write_text('{"fps": 12, "total_episodes": 1}\n', encoding="utf-8")
    monkeypatch.setenv("HF_HOME", str(hf))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_LEROBOT_HOME", raising=False)
    rows = list_hf_datasets([tmp_path / "extra"])
    ids = [row["repo_id"] for row in rows]
    assert ids.count("user/blocks") == 1
    assert "user/toy" in ids
    assert "local_ds" in ids


def test_list_hf_datasets_dedupes_same_repo(tmp_path: Path, monkeypatch) -> None:
    hf = tmp_path / "hf"
    hub = hf / "hub" / "datasets--user--blocks"
    snap = hub / "snapshots" / "abc"
    (snap / "meta").mkdir(parents=True)
    (snap / "meta" / "info.json").write_text('{"fps": 15, "total_episodes": 3}\n', encoding="utf-8")
    home = hf / "lerobot" / "user" / "blocks"
    (home / "meta").mkdir(parents=True)
    (home / "videos").mkdir()
    (home / "videos" / "front.mp4").write_bytes(b"x")
    (home / "meta" / "info.json").write_text('{"fps": 15, "total_episodes": 3}\n', encoding="utf-8")
    extra = tmp_path / "extra" / "user" / "blocks"
    (extra / "meta").mkdir(parents=True)
    (extra / "meta" / "info.json").write_text('{"fps": 15, "total_episodes": 3}\n', encoding="utf-8")
    monkeypatch.setenv("HF_HOME", str(hf))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_LEROBOT_HOME", raising=False)
    rows = list_hf_datasets([tmp_path / "extra"])
    matches = [row for row in rows if row["repo_id"] == "user/blocks"]
    assert len(matches) == 1
    assert matches[0]["playable"] is True
    assert matches[0]["source"] == "lerobot"
