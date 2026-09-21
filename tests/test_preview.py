from pathlib import Path

import pytest

from lerobot_monitor.preview import (
    MAX_SERIES_POINTS,
    lerobot_episode_payload,
    local_episode_payload,
    sample_index,
    series_from_joints_csv,
)
from lerobot_monitor.types import JOINT_ORDER


def test_series_from_joints_csv(tmp_path: Path) -> None:
    path = tmp_path / "joints.csv"
    header = ["t", "frame"] + [f"obs.{n}" for n in JOINT_ORDER] + [f"act.{n}" for n in JOINT_ORDER]
    rows = [",".join(header)]
    for i in range(5):
        obs = [str(i)] * len(JOINT_ORDER)
        act = [str(i + 0.5)] * len(JOINT_ORDER)
        rows.append(",".join([str(i * 0.1), str(i), *obs, *act]))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    payload = series_from_joints_csv(path)
    assert payload["frames"] == 5
    assert "obs.shoulder_pan" in payload["series"]
    assert payload["series"]["obs.shoulder_pan"][0] == 0.0


def test_series_times_are_episode_relative(tmp_path: Path) -> None:
    path = tmp_path / "joints.csv"
    path.write_text(
        "t,obs.gripper,act.gripper\n12.5,1,2\n12.6,2,3\n12.7,3,4\n",
        encoding="utf-8",
    )
    payload = series_from_joints_csv(path)
    assert payload["t"] == pytest.approx([0.0, 0.1, 0.2])


def test_local_episode_payload(tmp_path: Path) -> None:
    ep = tmp_path / "episodes" / "000000"
    (ep / "videos").mkdir(parents=True)
    (ep / "videos" / "front.mp4").write_bytes(b"not-a-real-video")
    (ep / "videos" / "merged.mp4").write_bytes(b"not-a-real-video")
    (ep / "joints.csv").write_text("t,frame,obs.gripper,act.gripper\n0,0,1,2\n0.1,1,2,3\n", encoding="utf-8")
    (tmp_path / "meta.json").write_text('{"fps": 15, "action_fps": 10, "video_fps": 30}\n', encoding="utf-8")
    payload = local_episode_payload(tmp_path, 0)
    assert [cam["name"] for cam in payload["cameras"]] == ["front"]
    assert "obs.gripper" in payload["series"]
    assert payload["action_fps"] == 10
    assert payload["video_fps"] == 30


def test_local_episode_payload_exposes_camera_timeline_offset_and_duration(tmp_path: Path) -> None:
    ep = tmp_path / "episodes" / "000000"
    (ep / "videos").mkdir(parents=True)
    (ep / "videos" / "side.mp4").write_bytes(b"not-a-real-video")
    (ep / "joints.csv").write_text(
        "t,frame,obs.gripper,act.gripper\n0,0,1,2\n10,1,2,3\n",
        encoding="utf-8",
    )
    (ep / "meta.json").write_text(
        '{"duration_s": 10.0, "video_fps": 20, "requested_video_fps": 20, '
        '"encoded_video_fps": 20, "video_start_frames": {"side": 60}, '
        '"per_camera_video_frames": {"side": 140}}\n',
        encoding="utf-8",
    )

    payload = local_episode_payload(tmp_path, 0)

    assert payload["duration_s"] == pytest.approx(10.0)
    assert payload["requested_video_fps"] == 20
    assert payload["encoded_video_fps"] == 20
    assert payload["cameras"] == [
        {"name": "side", "timeline_offset_s": 3.0, "encoded_frames": 140}
    ]


def test_series_share_one_time_base(tmp_path: Path) -> None:
    path = tmp_path / "joints.csv"
    header = "t,obs.gripper,act.gripper,obs.shoulder_pan"
    rows = [header]
    for i in range(5):
        obs = "" if i == 3 else str(i)
        pan = "" if i in (0, 4) else str(-i)
        rows.append(f"{i * 0.1},{obs},{i + 0.5},{pan}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    payload = series_from_joints_csv(path)
    lengths = {len(payload["t"])}
    lengths.update(len(values) for values in payload["series"].values())
    assert lengths == {5}
    assert payload["series"]["obs.gripper"][3] != payload["series"]["obs.gripper"][3]  # NaN keeps the row


def test_series_downsample_keeps_columns_aligned(tmp_path: Path) -> None:
    path = tmp_path / "joints.csv"
    rows = ["t,obs.gripper,act.gripper"]
    for i in range(1000):
        rows.append(f"{i * 0.01},{i},{'' if i % 3 else i + 0.5}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    payload = series_from_joints_csv(path)
    assert payload["frames"] == 1000
    assert len(payload["t"]) <= MAX_SERIES_POINTS
    assert len(payload["series"]["act.gripper"]) == len(payload["t"])


def test_sample_index_honors_limit_just_above_boundary() -> None:
    sampled = sample_index(MAX_SERIES_POINTS + 1)
    assert len(sampled) <= MAX_SERIES_POINTS
    assert sampled[0] == 0


def test_lerobot_v3_shard_selects_episode_and_preserves_alignment(tmp_path: Path) -> None:
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")

    root = tmp_path / "ds"
    data_path = root / "data" / "chunk-000" / "file-000.parquet"
    episodes_path = root / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
    data_path.parent.mkdir(parents=True)
    episodes_path.parent.mkdir(parents=True)
    (root / "meta" / "info.json").write_text(
        '{"fps": 10, "total_episodes": 2, '
        '"data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet", '
        '"features": {"observation.state": {"dtype": "float32"}, '
        '"action": {"dtype": "float32"}}}\n',
        encoding="utf-8",
    )
    pd.DataFrame(
        {
            "episode_index": [0, 1],
            "data/chunk_index": [0, 0],
            "data/file_index": [0, 0],
            "dataset_from_index": [0, 2],
            "dataset_to_index": [2, 5],
        }
    ).to_parquet(episodes_path, index=False)
    states = [[float(i)] * len(JOINT_ORDER) for i in range(5)]
    actions: list[list[float] | None] = [
        [float(i + 10)] * len(JOINT_ORDER) for i in range(5)
    ]
    actions[3] = None
    pd.DataFrame(
        {
            # No episode_index: selection must use the metadata boundaries.
            "index": list(range(5)),
            "timestamp": [1.0, 1.1, 8.0, 8.1, 8.2],
            "observation.state": states,
            "action": actions,
        }
    ).to_parquet(data_path, index=False)

    payload = lerobot_episode_payload(root, 1)
    assert payload["frames"] == 3
    assert payload["t"] == pytest.approx([0.0, 0.1, 0.2])
    assert payload["series"]["obs.shoulder_pan"] == [2.0, 3.0, 4.0]
    lengths = {len(payload["t"])}
    lengths.update(len(values) for values in payload["series"].values())
    assert lengths == {3}
    missing = payload["series"]["act.shoulder_pan"][1]
    assert missing != missing


def test_lerobot_v3_chunk_video(tmp_path: Path) -> None:
    from lerobot_monitor.preview import find_lerobot_video, lerobot_episode_videos

    root = tmp_path / "ds"
    video = root / "videos" / "observation.images.front" / "chunk-000" / "file-000.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"not-a-real-video")
    (root / "meta").mkdir()
    (root / "meta" / "info.json").write_text(
        '{"fps": 15, "total_episodes": 1, "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",'
        ' "features": {"observation.images.front": {"dtype": "video"}}}\n',
        encoding="utf-8",
    )
    (root / "meta" / "episodes.jsonl").write_text(
        '{"episode_index": 0, "videos/observation.images.front/chunk_index": 0,'
        ' "videos/observation.images.front/file_index": 0,'
        ' "videos/observation.images.front/from_timestamp": 1.5,'
        ' "videos/observation.images.front/to_timestamp": 3.0}\n',
        encoding="utf-8",
    )
    rows = lerobot_episode_videos(root, 0)
    assert rows[0]["name"] == "front"
    assert rows[0]["start"] == 1.5
    assert find_lerobot_video(root, 0, "front") == video
