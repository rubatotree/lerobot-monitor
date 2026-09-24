import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
import pytest

from lerobot_monitor import dataset_hub
from lerobot_monitor.dataset_hub import (
    DatasetHubError,
    DatasetRegistry,
    DatasetTransferManager,
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


def test_legacy_mp4v_video_gets_cached_browser_copy(tmp_path: Path) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg is required for legacy video previews")
    library = VideoLibrary(tmp_path)
    folder = tmp_path / "recording" / "episodes" / "000000" / "videos"
    folder.mkdir(parents=True)
    source = folder / "front.mp4"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
    assert writer.isOpened()
    for _ in range(3):
        writer.write(np.zeros((48, 64, 3), dtype=np.uint8))
    writer.release()

    preview = library.episode_browser_video("recording", 0, "front")
    assert preview != source
    assert source.is_file()
    assert preview.is_file()
    capture = cv2.VideoCapture(str(preview))
    try:
        fourcc = int(capture.get(cv2.CAP_PROP_FOURCC))
        codec = "".join(chr((fourcc >> (8 * offset)) & 0xFF) for offset in range(4))
        assert codec.lower() in {"avc1", "h264", "x264"}
    finally:
        capture.release()
    assert library.episode_browser_video("recording", 0, "front") == preview
    (tmp_path / "recording" / "meta.json").write_text('{"episodes": []}', encoding="utf-8")
    assert library.get("recording")["episodes"][0]["videos"] == ["front.mp4"]


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


def test_recorder_persists_task_into_each_episode(tmp_path: Path) -> None:
    root = tmp_path / "videos" / "tasked"
    rec = DatasetRecorder(
        root,
        fps=5,
        kind="record",
        extra_meta={"task": "sort blocks"},
        video=False,
    )
    rec.add_action({}, None, episode_index=0, elapsed_s=0.0)
    rec.finish_episode(0)
    rec.add_action({}, None, episode_index=1, elapsed_s=0.0)
    rec.close()

    saved = VideoLibrary(root.parent).get(root.name)
    assert [episode["task"] for episode in saved["episodes"]] == ["sort blocks", "sort blocks"]


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
        def list_datasets(self, *, search, filter, limit, sort):
            # Mirrors huggingface_hub 1.x, which has no `direction` argument:
            # passing one raises TypeError exactly like the real client did.
            assert (search, filter, limit, sort) == ("blocks", "lerobot", 20, "downloads")
            return [Row()]

    class Hub:
        HfApi = Api

        @staticmethod
        def snapshot_download(**kwargs):
            assert kwargs["repo_type"] == "dataset"
            assert kwargs["force_download"] is True
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
    assert download_hf_dataset("https://huggingface.co/datasets/user/blocks/tree/main") == str(dataset_root)


def test_dataset_registry_edits_source_and_starts_upload(tmp_path: Path, monkeypatch) -> None:
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

    started: list[tuple[str, str, str]] = []

    class StubTransfers:
        def start_upload(self, repo_id, folder, *, revision="", name="", private=False):
            started.append((repo_id, str(folder), revision, private))
            return {"repo_id": repo_id, "direction": "upload", "status": "pending", "active": True}

    registry.transfers = StubTransfers()
    state = registry.start_upload(row["id"])
    assert state["direction"] == "upload"
    assert started == [("user/demo", str(created["path"]), "", False)]

    with pytest.raises(FileNotFoundError):
        registry.start_upload("user/unknown")


def test_upload_dataset_folder_reports_batched_progress(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "dataset"
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text('{"fps": 15}\n', encoding="utf-8")
    (root / "data").mkdir()
    for index in range(4):
        (root / "data" / f"file-{index:03d}.parquet").write_bytes(b"x" * (index + 1))
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("ignored\n", encoding="utf-8")
    (root / "obsolete-local.txt").write_text("local", encoding="utf-8")

    committed: list[list[str]] = []
    steps: list[tuple[int, int]] = []

    class Hub:
        class CommitOperationAdd:
            def __init__(self, path_in_repo, path_or_fileobj):
                self.path_in_repo = path_in_repo
                self.path_or_fileobj = path_or_fileobj

        class CommitOperationDelete:
            def __init__(self, path_in_repo):
                self.path_in_repo = path_in_repo

        @staticmethod
        def list_repo_files(repo_id, *, repo_type="dataset", revision=None):
            assert (repo_id, repo_type, revision) == ("user/demo", "dataset", None)
            return ["old.parquet", ".gitattributes"]

        @staticmethod
        def create_repo(repo_id, repo_type="dataset", private=False, exist_ok=True):
            assert private is False
            committed.append(["create_repo"])

        @staticmethod
        def create_commit(*, operations, commit_message, **kwargs):
            committed.append([op.path_in_repo for op in operations])

    monkeypatch.setattr(dataset_hub, "_hub_module", lambda: Hub)
    dataset_hub.upload_dataset_folder(
        "user/demo",
        root,
        on_progress=lambda done_bytes, done_files: steps.append((done_bytes, done_files)),
    )

    uploaded = [path for batch in committed[1:] for path in batch]
    assert ".git/config" not in uploaded
    assert sorted(uploaded) == ["data/file-000.parquet", "data/file-001.parquet", "data/file-002.parquet", "data/file-003.parquet", "meta/info.json", "obsolete-local.txt", "old.parquet"]
    assert committed[-1] == ["old.parquet"]
    assert ".gitattributes" not in uploaded
    expected_bytes = sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    )
    assert steps and steps[-1] == (expected_bytes, 6)
    assert steps == sorted(steps)


def test_upload_dataset_folder_creates_private_repo(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "dataset"
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text('{"fps": 15}\n', encoding="utf-8")
    created: list[dict] = []

    class Hub:
        class CommitOperationAdd:
            def __init__(self, path_in_repo, path_or_fileobj):
                self.path_in_repo = path_in_repo

        class CommitOperationDelete:
            def __init__(self, path_in_repo):
                self.path_in_repo = path_in_repo

        @staticmethod
        def create_repo(repo_id, *, repo_type, private, exist_ok):
            created.append({"repo_id": repo_id, "repo_type": repo_type, "private": private, "exist_ok": exist_ok})

        @staticmethod
        def list_repo_files(repo_id, *, repo_type, revision=None):
            return []

        @staticmethod
        def create_commit(**kwargs):
            return None

    monkeypatch.setattr(dataset_hub, "_hub_module", lambda: Hub)
    dataset_hub.upload_dataset_folder("user/new-private", root, private=True)
    assert created == [{
        "repo_id": "user/new-private", "repo_type": "dataset", "private": True, "exist_ok": True,
    }]


def test_upload_accepts_hub_blob_links_but_skips_external_links(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    repo = tmp_path / "hf" / "hub" / "datasets--user--demo"
    snapshot = repo / "snapshots" / "revision"
    (snapshot / "meta").mkdir(parents=True)
    (snapshot / "meta" / "info.json").write_text('{"fps": 15}\n', encoding="utf-8")
    (snapshot / "data").mkdir()
    (repo / "blobs").mkdir()
    blob = repo / "blobs" / "payload"
    blob.write_bytes(b"parquet")
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    try:
        (snapshot / "data" / "file.parquet").symlink_to(blob)
        (snapshot / "external.txt").symlink_to(secret)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    paths = {name for name, _size in dataset_hub.upload_folder_files(snapshot)}
    assert "data/file.parquet" in paths
    assert "external.txt" not in paths


def test_dataset_registry_orders_by_arrival_not_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_LEROBOT_HOME", str(tmp_path / "lerobot"))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    store = JsonStore(tmp_path / "store.json")
    # A hub dataset added later must not jump ahead of an earlier local one, even
    # when its own timestamps would sort it first.
    store.put_dataset(
        {
            "id": "user/local",
            "name": "local",
            "repo_id": "user/local",
            "source": "hub",
            "path": "",
            "created_utc": "2026-09-23T11:00:00+00:00",
        }
    )
    store.put_dataset(
        {
            "id": "user/hub",
            "name": "hub",
            "repo_id": "user/hub",
            "source": "hub",
            "path": "",
            "created_utc": "2026-09-23T12:00:00+00:00",
        }
    )
    scanned = tmp_path / "scanned"
    dataset = scanned / "user" / "folder"
    (dataset / "meta").mkdir(parents=True)
    (dataset / "meta" / "info.json").write_text('{"fps": 15}\n', encoding="utf-8")
    os.utime(dataset, (1_800_000_000, 1_800_000_000))  # newer than both entries

    registry = DatasetRegistry(store, [scanned])
    # Registered datasets keep the order the user added them in; scanned folders
    # trail them instead of leading the list.
    assert [row["id"] for row in registry.list()] == ["user/local", "user/hub", "user/folder"]


def test_dataset_registry_keeps_store_order_after_hub_download(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_LEROBOT_HOME", str(tmp_path / "lerobot"))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    store = JsonStore(tmp_path / "store.json")
    store.put_dataset({"id": "user/first", "name": "first", "source": "hub", "path": ""})
    store.put_dataset({"id": "user/second", "name": "second", "source": "hub", "path": ""})
    registry = DatasetRegistry(store, [])

    assert [row["id"] for row in registry.list()] == ["user/first", "user/second"]
    # A dataset added afterwards is appended, never inserted ahead of older ones.
    registry.stage(remote="user/third")
    assert [row["id"] for row in registry.list()] == ["user/first", "user/second", "user/third"]


def test_list_hf_datasets_reads_v3_tasks_parquet(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("pandas")
    import pandas as pd

    hf = tmp_path / "hf"
    monkeypatch.setenv("HF_HOME", str(hf))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_LEROBOT_HOME", raising=False)
    extra = tmp_path / "extra" / "picked"
    (extra / "meta").mkdir(parents=True)
    (extra / "meta" / "info.json").write_text(
        '{"fps": 15, "total_episodes": 2, "total_tasks": 2}\n',
        encoding="utf-8",
    )
    # LeRobot v3 layout: the task text is the index, `task_index` is the column
    # that the data files reference. Rows are intentionally out of index order.
    pd.DataFrame(
        {"task_index": [1, 0]},
        index=pd.Index(["Sort blocks", "Pick cube"], name="task"),
    ).to_parquet(extra / "meta" / "tasks.parquet")

    rows = list_hf_datasets([tmp_path / "extra"])
    row = next(item for item in rows if item["repo_id"] == "picked")
    assert row["task"] == "Pick cube"
    assert row["tasks"] == ["Pick cube", "Sort blocks"]
    assert row["subtitle"] == "Pick cube"


def test_list_hf_datasets_keeps_legacy_tasks_jsonl(tmp_path: Path, monkeypatch) -> None:
    hf = tmp_path / "hf"
    monkeypatch.setenv("HF_HOME", str(hf))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_LEROBOT_HOME", raising=False)
    extra = tmp_path / "extra" / "legacy"
    (extra / "meta").mkdir(parents=True)
    (extra / "meta" / "info.json").write_text('{"fps": 15, "total_episodes": 1}\n', encoding="utf-8")
    (extra / "meta" / "tasks.jsonl").write_text(
        'not-json\n{"task_index": 0, "task": "Legacy pick"}\n',
        encoding="utf-8",
    )

    rows = list_hf_datasets([tmp_path / "extra"])
    row = next(item for item in rows if item["repo_id"] == "legacy")
    assert row["task"] == "Legacy pick"
    assert row["tasks"] == ["Legacy pick"]


def test_dataset_registry_resolves_stored_id_and_repo_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_LEROBOT_HOME", str(tmp_path / "lerobot"))
    snapshot = tmp_path / "hub" / "datasets--user--demo" / "snapshots" / "rev"
    (snapshot / "meta").mkdir(parents=True)
    (snapshot / "meta" / "info.json").write_text('{"fps": 15}\n', encoding="utf-8")
    store = JsonStore(tmp_path / "store.json")
    # Legacy entries can carry a local id while pointing at an upstream repo.
    store.put_dataset(
        {
            "id": "demo_plus80",
            "repo_id": "user/demo",
            "remote": "user/demo",
            "source": "hub",
            "path": str(snapshot),
        }
    )
    registry = DatasetRegistry(store, [])

    row = registry.get("user/demo")
    assert row["repo_id"] == "user/demo"
    assert row["id"] == "user/demo"
    assert registry.get("demo_plus80")["repo_id"] == "user/demo"
    assert registry.delete("user/demo")["id"] == "demo_plus80"
    assert store.dataset("demo_plus80") is None


def test_dataset_registry_save_never_downloads(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_LEROBOT_HOME", str(tmp_path / "lerobot"))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    snapshot = tmp_path / "hf" / "hub" / "datasets--user--demo" / "snapshots" / "rev"
    (snapshot / "meta").mkdir(parents=True)
    (snapshot / "meta" / "info.json").write_text('{"fps": 15, "total_episodes": 3}\n', encoding="utf-8")
    store = JsonStore(tmp_path / "store.json")
    store.put_dataset(
        {
            "id": "user/demo",
            "name": "Demo",
            "source": "hub",
            "remote": "user/demo",
            "repo_id": "user/demo",
            "revision": "",
            "path": str(snapshot),
        }
    )
    registry = DatasetRegistry(store, [])

    def explode(*args, **kwargs):
        raise AssertionError("saving dataset details must not sync")

    monkeypatch.setattr(dataset_hub, "download_hf_dataset", explode)

    # Same address: the downloaded snapshot stays on the entry.
    saved = registry.save("user/demo", {"name": "Renamed", "remote": "user/demo", "revision": "v1"})
    assert saved["repo_id"] == "user/demo"
    assert saved["name"] == "Renamed"
    assert saved["revision"] == "v1"
    assert saved["path"] == str(snapshot)

    # New address: the stale snapshot is dropped until an explicit download.
    moved = registry.save("user/demo", {"remote": "user/other"})
    assert moved["repo_id"] == "user/other"
    assert moved["path"] == ""

    store.put_dataset({"id": "user/taken", "repo_id": "user/taken", "path": ""})
    with pytest.raises(DatasetHubError, match="already in the Library"):
        registry.save("user/other", {"remote": "user/taken"})


def test_dataset_registry_keeps_local_path_separate_from_upstream(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HF_LEROBOT_HOME", raising=False)
    dataset = tmp_path / "datasets" / "user" / "local"
    (dataset / "meta").mkdir(parents=True)
    (dataset / "meta" / "info.json").write_text('{"fps": 15}\n', encoding="utf-8")
    store = JsonStore(tmp_path / "store.json")
    registry = DatasetRegistry(store, [tmp_path / "datasets"])
    scanned = registry.get("user/local")
    assert scanned["upstream"] is False
    with pytest.raises(DatasetHubError, match="no upstream"):
        registry.start_upload("user/local")

    saved = registry.save("user/local", {"repo_id": "user/remote", "path": str(dataset), "private": True})
    assert saved["id"] == "user/remote"
    assert saved["path"] == str(dataset)
    assert saved["upstream"] is True
    assert saved["private"] is True

    moved = registry.save("user/remote", {"repo_id": "user/new"})
    assert moved["repo_id"] == "user/new"
    assert moved["path"] == str(dataset)
    assert moved["private"] is True

    unbound = registry.save("user/new", {"repo_id": ""})
    assert unbound["repo_id"] == ""
    assert unbound["path"] == str(dataset)
    assert unbound["upstream"] is False
    with pytest.raises(DatasetHubError, match="missing meta/info.json"):
        registry.save(unbound["id"], {"path": str(tmp_path / "missing")})
    assert registry.get(unbound["id"])["path"] == str(dataset)


def test_dataset_registry_start_download_stages_a_card(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_LEROBOT_HOME", str(tmp_path / "lerobot"))
    store = JsonStore(tmp_path / "store.json")
    registry = DatasetRegistry(store, [])
    started: list[tuple[str, str, str]] = []

    class StubTransfers:
        def start(self, remote: str, *, revision: str = "", name: str = "") -> dict:
            started.append((remote, revision, name))
            return {"repo_id": remote, "revision": revision, "status": "pending", "active": True}

    registry.transfers = StubTransfers()
    state = registry.start_download(remote="user/blocks", revision="main")

    assert state["active"] is True
    assert started == [("user/blocks", "main", "user/blocks")]
    row = next(item for item in registry.list() if item["id"] == "user/blocks")
    assert row["repo_id"] == "user/blocks"
    assert row["revision"] == "main"
    assert row["path"] == ""


def test_dataset_upload_rejects_empty_local_path(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "store.json")
    store.put_dataset({"id": "user/empty", "repo_id": "user/empty", "source": "hub", "path": ""})
    registry = DatasetRegistry(store, [])
    with pytest.raises(DatasetHubError, match="no local path"):
        registry.start_upload("user/empty")


def test_finished_download_does_not_restore_edited_or_deleted_source(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "store.json")
    store.put_dataset(
        {"id": "user/old", "repo_id": "user/old", "source": "hub", "revision": "main", "path": ""}
    )
    registry = DatasetRegistry(store, [])
    finished = {
        "repo_id": "user/old", "revision": "main", "direction": "download",
        "remote": "user/old", "status": "done", "path": str(tmp_path / "snapshot"),
    }
    registry.save("user/old", {"remote": "user/new"})
    registry._finish_transfer(finished)
    assert store.dataset("user/old")["repo_id"] == "user/new"
    assert store.dataset("user/old")["path"] == ""

    store.delete_dataset("user/old")
    registry._finish_transfer(finished)
    assert store.datasets() == []


def test_active_transfer_prevents_dataset_delete(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "store.json")
    store.put_dataset({"id": "user/demo", "repo_id": "user/demo", "path": str(tmp_path / "dataset")})
    registry = DatasetRegistry(store, [])

    class BusyTransfers:
        def get(self, repo_id: str) -> dict:
            return {"active": True, "direction": "download"}

    registry.transfers = BusyTransfers()
    with pytest.raises(DatasetHubError, match="still download"):
        registry.delete("user/demo")
    assert store.dataset("user/demo") is not None


def test_dataset_transfer_manager_reports_progress(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_LEROBOT_HOME", str(tmp_path / "lerobot"))
    root = tmp_path / "snapshot"
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text('{"fps": 15}\n', encoding="utf-8")
    written = {"bytes": 0}
    release = threading.Event()
    events: list[dict] = []

    def downloader(repo_id: str, *, revision: str = "") -> str:
        release.wait(timeout=5)
        return str(root)

    manager = DatasetTransferManager(
        poll_seconds=0.05,
        on_finish=events.append,
        progress_probe=lambda repo_id: written["bytes"],
        total_probe=lambda repo_id, revision: 100,
        downloader=downloader,
    )
    started = manager.start("user/blocks")
    assert started["direction"] == "download"
    assert started["status"] in {"pending", "transferring"}

    written["bytes"] = 40
    deadline = time.time() + 5
    mid: dict = {}
    while time.time() < deadline:
        current = manager.get("user/blocks") or {}
        if current.get("transferred_bytes") == 40:
            mid = current
            break
        time.sleep(0.05)
    assert mid, "the watcher never published download progress"
    assert mid["percent"] == 40.0
    assert mid["active"] is True

    release.set()
    deadline = time.time() + 5
    while not events and time.time() < deadline:
        time.sleep(0.05)
    assert events and events[0]["status"] == "done"
    assert events[0]["percent"] == 100.0
    assert events[0]["path"] == str(root)

    with pytest.raises(DatasetHubError):
        manager.start("")


def test_dataset_transfer_manager_uploads_with_file_progress(tmp_path: Path) -> None:
    root = tmp_path / "dataset"
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text('{"fps": 15}\n', encoding="utf-8")
    (root / "data").mkdir()
    for index in range(4):
        (root / "data" / f"file-{index:03d}.parquet").write_bytes(b"x" * 100)
    release = threading.Event()
    events: list[dict] = []

    def uploader(repo_id, folder, *, revision="", private=False, on_progress=None):
        assert private is True
        if on_progress is not None:
            on_progress(200, 2)
        release.wait(timeout=5)
        return str(folder)

    manager = DatasetTransferManager(poll_seconds=0.05, on_finish=events.append, uploader=uploader)
    started = manager.start_upload("user/demo", root, private=True)
    assert started["direction"] == "upload"
    assert started["active"] is True
    assert started["private"] is True

    deadline = time.time() + 5
    mid: dict = {}
    while time.time() < deadline:
        current = manager.get("user/demo") or {}
        if current.get("files_done") == 2:
            mid = current
            break
        time.sleep(0.05)
    assert mid.get("files_done") == 2, "upload progress was never published"
    assert mid["percent"] == pytest.approx(200 / mid["total_bytes"] * 100, abs=0.1)

    release.set()
    deadline = time.time() + 5
    while not events and time.time() < deadline:
        time.sleep(0.05)
    assert events and events[0]["status"] == "done"
    assert events[0]["files_total"] == 5

    with pytest.raises(DatasetHubError):
        manager.start_upload("", root)


def test_download_replaces_local_dataset_and_removes_stale_files(tmp_path: Path) -> None:
    destination = tmp_path / "local"
    (destination / "meta").mkdir(parents=True)
    (destination / "meta" / "info.json").write_text('{"fps": 10}\n', encoding="utf-8")
    (destination / "stale.parquet").write_bytes(b"old")
    snapshot = tmp_path / "snapshot"
    (snapshot / "meta").mkdir(parents=True)
    (snapshot / "meta" / "info.json").write_text('{"fps": 20}\n', encoding="utf-8")
    (snapshot / "new.parquet").write_bytes(b"new")
    manager = DatasetTransferManager(
        progress_probe=lambda repo_id: 0,
        total_probe=lambda repo_id, revision: 0,
        downloader=lambda repo_id, revision="": str(snapshot),
    )
    manager.start("user/demo", target_path=str(destination))
    deadline = time.time() + 5
    while time.time() < deadline and (manager.get("user/demo") or {}).get("status") != "done":
        time.sleep(0.02)
    state = manager.get("user/demo")
    assert state and state["status"] == "done"
    assert state["path"] == str(destination)
    assert (destination / "new.parquet").read_bytes() == b"new"
    assert not (destination / "stale.parquet").exists()


def test_download_copy_failure_preserves_local_dataset(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "local"
    (destination / "meta").mkdir(parents=True)
    (destination / "meta" / "info.json").write_text('{"fps": 10}\n', encoding="utf-8")
    original = destination / "data.parquet"
    original.write_bytes(b"original")
    snapshot = tmp_path / "snapshot"
    (snapshot / "meta").mkdir(parents=True)
    (snapshot / "meta" / "info.json").write_text('{"fps": 20}\n', encoding="utf-8")

    def broken_copy(source: Path, target: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(dataset_hub.shutil, "copytree", broken_copy)
    manager = DatasetTransferManager(
        progress_probe=lambda repo_id: 0,
        total_probe=lambda repo_id, revision: 0,
        downloader=lambda repo_id, revision="": str(snapshot),
    )
    manager.start("user/demo", target_path=str(destination))
    deadline = time.time() + 5
    while time.time() < deadline and (manager.get("user/demo") or {}).get("status") != "error":
        time.sleep(0.02)
    state = manager.get("user/demo")
    assert state and state["status"] == "error"
    assert "disk full" in state["error"]
    assert original.read_bytes() == b"original"


def test_dataset_transfer_manager_blocks_duplicates_and_surfaces_errors(tmp_path: Path) -> None:
    release = threading.Event()
    events: list[dict] = []

    def downloader(repo_id: str, *, revision: str = "") -> str:
        release.wait(timeout=5)
        raise DatasetHubError("could not download user/blocks: 404")

    manager = DatasetTransferManager(
        poll_seconds=0.05,
        on_finish=events.append,
        progress_probe=lambda repo_id: 0,
        total_probe=lambda repo_id, revision: 0,
        downloader=downloader,
    )
    first = manager.start("user/blocks")
    assert first["indeterminate"] is True
    with pytest.raises(DatasetHubError):
        manager.start("user/blocks")

    release.set()
    deadline = time.time() + 5
    while not events and time.time() < deadline:
        time.sleep(0.05)

    assert events and events[0]["status"] == "error"
    assert "404" in events[0]["error"]
    assert events[0]["active"] is False


def test_quiet_progress_bar_renders_nothing(capsys) -> None:
    quiet = dataset_hub._quiet_progress()
    with quiet(total=8) as bar:
        bar.update(3)
        bar.set_description("downloading")
    assert bar.disable is True
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
