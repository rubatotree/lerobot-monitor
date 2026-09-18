from pathlib import Path

import numpy as np

from lerobot_monitor.session import SessionWriter, list_sessions
from lerobot_monitor.types import JOINT_ORDER


def test_session_writes_csv_and_video(tmp_path: Path) -> None:
    writer = SessionWriter(tmp_path, kind="rollout", fps=5, extra_meta={"task": "demo"})
    pose = {name: float(i) for i, name in enumerate(JOINT_ORDER)}
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    frame[:] = (20, 40, 80)
    writer.add_frame(pose, pose, {"front": frame}, episode_index=0)
    writer.add_frame(pose, pose, {"front": frame}, episode_index=0)
    out = writer.close()
    assert (out / "joints.csv").is_file()
    assert (out / "meta.json").is_file()
    assert (out / "videos" / "front.mp4").is_file()
    rows = (out / "joints.csv").read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 3  # header + 2 frames
    sessions = list_sessions(tmp_path)
    assert sessions[0]["kind"] == "rollout"
    assert sessions[0]["frames"] == 2
