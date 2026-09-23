import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

from lerobot_monitor import dataset_hub
from lerobot_monitor.dataset_hub import (
    DatasetRegistry,
    create_empty_dataset,
    download_hf_dataset,
    search_hf_datasets,
)
from lerobot_monitor.library import (
    DatasetRecorder,
    VideoLibrary,
    list_hf_datasets,
    list_local_models,
)
from lerobot_monitor.session import mosaic_bgr
from lerobot_monitor.store import JsonStore
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
    reordered = lib.reorder(created["id"], [1, 0])
    assert reordered["episode_index_map"] == {"1": 0, "0": 1}
    again = lib.get(created["id"])
    assert [e["index"] for e in again["episodes"]] == [0, 1]
    deleted = lib.delete_episode(created["id"], 0)
    assert deleted["episode_index_map"] == {"1": 0}
    leftover = lib.get(created["id"])
    assert len(leftover["episodes"]) == 1
    lib.delete(created["id"])
    assert lib.list() == []


def test_recorder_resume_skips_uncommitted_episode_directories(tmp_path: Path) -> None:
    root = tmp_path / "videos" / "session"
    residue = root / "episodes" / "000007"
    residue.mkdir(parents=True)
    marker = residue / "crash-residue.txt"
    marker.write_text("preserve", encoding="utf-8")
    (root / "meta.json").write_text(
        '{"episodes": [{"index": 2}], "frames": 3}\n',
        encoding="utf-8",
    )

    recorder = DatasetRecorder(root, fps=5, kind="record", resume=True, merge=False)
    assert recorder.episode_index == 8
    recorder.add_frame({"gripper": 1.0}, {"gripper": 2.0}, {})
    recorder.close()

    assert marker.read_text(encoding="utf-8") == "preserve"
    assert (root / "episodes" / "000008" / "joints.csv").is_file()


def test_recorder_video_false_writes_only_csv_and_meta(tmp_path: Path, monkeypatch) -> None:
    from lerobot_monitor import session as session_module

    def fail_open(*args, **kwargs):
        raise AssertionError("video writer must not be opened")

    monkeypatch.setattr(session_module, "open_video_writer", fail_open)
    root = tmp_path / "videos" / "no_media"
    recorder = DatasetRecorder(
        root,
        fps=5,
        kind="record",
        extra_meta={"video": False},
        merge=True,
    )
    frame = np.zeros((8, 10, 3), dtype=np.uint8)
    recorder.add_frame({}, None, {"front": frame})
    recorder.add_frame({}, None, {"front": frame})
    recorder.close()

    episode = root / "episodes" / "000000"
    assert (episode / "joints.csv").read_text(encoding="utf-8").count("\n") == 3
    assert not (episode / "videos").exists()
    assert not (episode / "preview.jpg").exists()
    saved = VideoLibrary(root.parent).get(root.name)
    assert saved["video"] is False
    assert saved["frames"] == 2
    assert saved["episodes"][0]["video"] is False
    assert saved["episodes"][0]["videos"] == []
    assert saved["episodes"][0]["preview"] is None


def test_recorder_dual_rate_metadata_and_explicit_episode_finish(tmp_path: Path, monkeypatch) -> None:
    from lerobot_monitor import session as session_module

    class FakeVideoWriter:
        def write(self, frame: np.ndarray) -> None:
            pass

        def release(self) -> None:
            pass

    monkeypatch.setattr(session_module, "open_video_writer", lambda *args, **kwargs: FakeVideoWriter())
    root = tmp_path / "videos" / "dual"
    recorder = DatasetRecorder(root, action_fps=2, video_fps=4, kind="record", merge=False)
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    recorder.add_action({}, None, episode_index=0, elapsed_s=0.0)
    recorder.add_video({"front": frame}, episode_index=0, elapsed_s=0.25)
    first = recorder.finish_episode(0)
    assert first is not None
    recorder.add_action({}, None, episode_index=1, elapsed_s=0.0)
    recorder.add_video({"front": frame}, episode_index=1, elapsed_s=0.25)
    recorder.close()

    saved = VideoLibrary(root.parent).get(root.name)
    assert saved["fps"] == saved["action_fps"] == 2
    assert saved["video_fps"] == 4
    assert saved["frames"] == saved["action_frames"] == 2
    assert saved["video_frames"] == 2
    assert saved["per_camera_video_frames"] == {"front": 2}
    assert len(saved["episodes"]) == 2
    assert all(episode["action_frames"] == 1 for episode in saved["episodes"])


def test_recorder_resume_rejects_frame_rate_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "videos" / "resume"
    recorder = DatasetRecorder(root, action_fps=10, video_fps=20, kind="record")
    recorder.close()

    with pytest.raises(ValueError, match="frame-rate mismatch"):
        DatasetRecorder(root, action_fps=10, video_fps=15, kind="record", resume=True)


def test_recorder_root_duration_uses_sparse_action_timestamps(tmp_path: Path) -> None:
    root = tmp_path / "videos" / "sparse"
    recorder = DatasetRecorder(root, action_fps=15, video_fps=30, kind="record", video=False)
    recorder.add_action({}, None, elapsed_s=0.0)
    recorder.add_action({}, None, elapsed_s=10.0)
    recorder.close()

    saved = VideoLibrary(root.parent).get(root.name)
    assert saved["duration_s"] == pytest.approx(10.0)
    assert saved["episodes"][0]["duration_s"] == pytest.approx(10.0)


def test_video_library_create_is_atomic_across_instances(tmp_path: Path, monkeypatch) -> None:
    from lerobot_monitor import library as library_module

    monkeypatch.setattr(library_module, "_utc_stamp", lambda: "20260921_120000")
    root = tmp_path / "videos"

    def create_one(index: int) -> dict[str, object]:
        return VideoLibrary(root).create("blocks", extra={"worker": index})

    with ThreadPoolExecutor(max_workers=12) as pool:
        rows = list(pool.map(create_one, range(24)))

    ids = [str(row["id"]) for row in rows]
    assert len(set(ids)) == len(ids)
    for row in rows:
        saved = VideoLibrary(root).get(str(row["id"]))
        assert saved["worker"] == row["worker"]


def test_video_library_duplicate_copies_episode_data(tmp_path: Path) -> None:
    lib = VideoLibrary(tmp_path / "videos")
    created = lib.create("blocks", fps=5)
    episode = Path(created["path"]) / "episodes" / "000000"
    episode.mkdir(parents=True)
    (episode / "joints.csv").write_text("t,obs.gripper\n0,1\n", encoding="utf-8")

    copied = lib.duplicate(created["id"], name="blocks copy")
    assert copied["id"] != created["id"]
    assert copied["name"] == "blocks copy"
    assert (Path(copied["path"]) / "episodes" / "000000" / "joints.csv").is_file()


def test_video_library_trim_keeps_first_episode(tmp_path: Path) -> None:
    lib = VideoLibrary(tmp_path / "videos")
    created = lib.create("blocks", fps=5)
    pose = {name: float(i) for i, name in enumerate(JOINT_ORDER)}
    frame = np.zeros((8, 10, 3), dtype=np.uint8)
    rec = DatasetRecorder(Path(created["path"]), fps=5, kind="record", merge=True)
    rec.add_frame(pose, pose, {"front": frame}, episode_index=0)
    rec.add_frame(pose, pose, {"front": frame}, episode_index=1)
    rec.close()
    assert len(lib.get(created["id"])["episodes"]) == 2

    trimmed = lib.trim_to_first_episode(created["id"])
    assert len(trimmed["episodes"]) == 1
    assert trimmed["episodes"][0]["index"] == 0
    assert (Path(created["path"]) / "episodes" / "000000").is_dir()


def test_video_library_recovers_missing_duration_from_joints_csv(tmp_path: Path) -> None:
    lib = VideoLibrary(tmp_path / "videos")
    created = lib.create("blocks", fps=15)
    episode = Path(created["path"]) / "episodes" / "000000"
    episode.mkdir(parents=True)
    (episode / "joints.csv").write_text(
        "t,obs.gripper\n0.0,1\n51.1124,2\n",
        encoding="utf-8",
    )

    recovered = lib.get(created["id"])
    assert recovered["duration_s"] == pytest.approx(51.1124)
    assert recovered["episodes"][0]["duration_s"] == pytest.approx(51.1124)
    assert json.loads((episode / "meta.json").read_text(encoding="utf-8"))["duration_s"] == pytest.approx(51.1124)


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
    (extra / "meta" / "info.json").write_text(
        '{"fps": 12, "total_episodes": 1, "title": "Local Robot", "description": "Demo set"}\n',
        encoding="utf-8",
    )
    (extra / "meta" / "tasks.jsonl").write_text(
        'not-json\n{"task_index": 0, "task": "Pick cube"}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HF_HOME", str(hf))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_LEROBOT_HOME", raising=False)
    rows = list_hf_datasets([tmp_path / "extra"])
    ids = [row["repo_id"] for row in rows]
    assert ids.count("user/blocks") == 1
    assert "user/toy" in ids
    assert "local_ds" in ids
    local = next(row for row in rows if row["repo_id"] == "local_ds")
    assert local["title"] == "Local Robot"
    assert local["subtitle"] == "Pick cube"
    assert local["description"] == "Demo set"


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


def _isolate_hf_home(tmp_path: Path, monkeypatch) -> Path:
    hf = tmp_path / "hf"
    hf.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HF_HOME", str(hf))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_LEROBOT_HOME", raising=False)
    monkeypatch.delenv("LEROBOT_HOME", raising=False)
    return hf


def _write_policy(path: Path, weight: str = "model.safetensors") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text(
        '{"type": "act", "input_features": {"observation.state": {}}, "output_features": {"action": {}}}\n',
        encoding="utf-8",
    )
    (path / weight).write_bytes(b"x")


def test_list_local_models_scans_hub_and_roots(tmp_path: Path, monkeypatch) -> None:
    hf = _isolate_hf_home(tmp_path, monkeypatch)
    _write_policy(hf / "hub" / "models--user--act" / "snapshots" / "rev1")
    local = tmp_path / "models" / "my_act"
    _write_policy(local)
    checkpoint = tmp_path / "models" / "run" / "checkpoints" / "100000" / "pretrained_model"
    _write_policy(checkpoint)
    rows = list_local_models([tmp_path / "models"])
    by_name = {row["name"]: row for row in rows}
    assert set(by_name) == {"user/act", "my_act", "100000"}
    assert by_name["user/act"]["source"] == "hub"
    assert by_name["user/act"]["repo_id"] == "user/act"
    assert by_name["user/act"]["policy_type"] == "act"
    assert by_name["user/act"]["path"].endswith(str(Path("snapshots") / "rev1"))
    assert by_name["my_act"]["source"] == "local"
    assert by_name["my_act"]["path"] == str(local)
    assert by_name["100000"]["path"] == str(checkpoint)


def test_list_local_models_uses_newest_snapshot(tmp_path: Path, monkeypatch) -> None:
    hf = _isolate_hf_home(tmp_path, monkeypatch)
    repo = hf / "hub" / "models--user--act"
    old = repo / "snapshots" / "old"
    new = repo / "snapshots" / "new"
    _write_policy(old)
    _write_policy(new)
    os.utime(old, (1_600_000_000, 1_600_000_000))
    os.utime(new, (1_700_000_000, 1_700_000_000))
    rows = list_local_models([])
    assert len(rows) == 1
    assert rows[0]["path"].endswith(str(Path("snapshots") / "new"))


def test_list_local_models_skips_dir_without_weights(tmp_path: Path, monkeypatch) -> None:
    _isolate_hf_home(tmp_path, monkeypatch)
    bare = tmp_path / "models" / "config_only"
    bare.mkdir(parents=True)
    (bare / "config.json").write_text("{}", encoding="utf-8")
    assert list_local_models([tmp_path / "models"]) == []


def test_list_local_models_ignores_processor_safetensors(tmp_path: Path, monkeypatch) -> None:
    _isolate_hf_home(tmp_path, monkeypatch)
    partial = tmp_path / "models" / "partial"
    partial.mkdir(parents=True)
    (partial / "config.json").write_text(
        '{"type": "smolvla", "input_features": {"observation.state": {}}, '
        '"output_features": {"action": {}}}\n',
        encoding="utf-8",
    )
    (partial / "policy_preprocessor_step_5_normalizer_processor.safetensors").write_bytes(b"x")
    (partial / "policy_postprocessor_step_0_unnormalizer_processor.safetensors").write_bytes(b"x")
    assert list_local_models([tmp_path / "models"]) == []


def test_list_local_models_skips_generic_hf_model(tmp_path: Path, monkeypatch) -> None:
    hf = _isolate_hf_home(tmp_path, monkeypatch)
    generic = hf / "hub" / "models--openai--gpt2" / "snapshots" / "rev1"
    generic.mkdir(parents=True)
    (generic / "config.json").write_text('{"model_type": "gpt2"}\n', encoding="utf-8")
    (generic / "model.safetensors").write_bytes(b"x")
    assert list_local_models([]) == []


def test_create_empty_lerobot_dataset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_LEROBOT_HOME", str(tmp_path / "lerobot"))
    monkeypatch.delenv("LEROBOT_HOME", raising=False)
    created = create_empty_dataset(
        name="Empty demo",
        repo_id="user/empty_demo",
        fps=20,
        robot_type="so101_follower",
        cameras=[{"key": "front camera", "width": 640, "height": 480}],
    )

    root = Path(created["path"])
    info = json.loads((root / "meta" / "info.json").read_text(encoding="utf-8"))
    assert info["codebase_version"] == "v3.0"
    assert info["fps"] == 20
    assert info["total_episodes"] == 0
    assert info["features"]["action"]["shape"] == [6]
    assert info["features"]["observation.images.front_camera"]["shape"] == [480, 640, 3]
    assert (root / "data").is_dir()
    assert (root / "videos").is_dir()


def test_dataset_hub_search_and_download_validation(tmp_path: Path, monkeypatch) -> None:
    dataset_root = tmp_path / "hub" / "snapshots" / "rev"
    (dataset_root / "meta").mkdir(parents=True)
    (dataset_root / "meta" / "info.json").write_text('{"fps": 15, "total_episodes": 0}\n', encoding="utf-8")

    class Row:
        id = "user/blocks"
        downloads = 7
        likes = 2
        last_modified = "2026-09-23"
        tags = ["lerobot"]

    class Api:
        def list_datasets(self, **kwargs):
            return [Row()]

    class Hub:
        HfApi = Api

        @staticmethod
        def snapshot_download(**kwargs):
            assert kwargs["repo_type"] == "dataset"
            return str(dataset_root)

    monkeypatch.setattr(dataset_hub, "_hub_module", lambda: Hub)
    rows = search_hf_datasets("blocks")
    assert rows == [
        {
            "repo_id": "user/blocks",
            "downloads": 7,
            "likes": 2,
            "last_modified": "2026-09-23",
            "tags": ["lerobot"],
        }
    ]
    assert download_hf_dataset("user/blocks") == str(dataset_root)


def test_dataset_registry_edits_source_and_uploads(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_LEROBOT_HOME", str(tmp_path / "lerobot"))
    created = create_empty_dataset(name="Demo", repo_id="user/demo", fps=15)
    store = JsonStore(tmp_path / "store.json")
    registry = DatasetRegistry(store, [])
    row = registry.register(
        remote=str(created["path"]),
        name="Demo",
        repo_id="user/demo",
    )
    assert row["repo_id"] == "user/demo"
    assert row["episodes"] == 0

    calls: list[tuple[str, str]] = []

    class Hub:
        @staticmethod
        def create_repo(repo_id, repo_type="dataset", exist_ok=True):
            calls.append(("create", repo_id))

        @staticmethod
        def upload_folder(**kwargs):
            calls.append(("upload", kwargs["repo_id"]))

    monkeypatch.setattr(dataset_hub, "_hub_module", lambda: Hub)
    uploaded = registry.upload(row["id"])
    assert uploaded["repo_id"] == "user/demo"
    assert calls == [("create", "user/demo"), ("upload", "user/demo")]
