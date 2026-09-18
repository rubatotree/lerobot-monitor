from pathlib import Path

from lerobot_monitor.preview import local_episode_payload, series_from_joints_csv
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


def test_local_episode_payload(tmp_path: Path) -> None:
    ep = tmp_path / "episodes" / "000000"
    (ep / "videos").mkdir(parents=True)
    (ep / "videos" / "front.mp4").write_bytes(b"not-a-real-video")
    (ep / "videos" / "merged.mp4").write_bytes(b"not-a-real-video")
    (ep / "joints.csv").write_text("t,frame,obs.gripper,act.gripper\n0,0,1,2\n0.1,1,2,3\n", encoding="utf-8")
    payload = local_episode_payload(tmp_path, 0)
    assert payload["cameras"][0]["name"] == "merged"
    assert "obs.gripper" in payload["series"]


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
