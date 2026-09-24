import shutil
from pathlib import Path

import cv2
import numpy as np
import pytest

from lerobot_monitor.deferred_video import DeferredVideoEncoder
from lerobot_monitor.library import DatasetRecorder, VideoLibrary


def _jpeg(value: int) -> bytes:
    image = np.full((48, 64, 3), value, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return encoded.tobytes()


def test_deferred_images_encode_after_recording_with_progress(tmp_path: Path) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg is required for deferred encoding")
    root = tmp_path / "recording"
    recorder = DatasetRecorder(
        root,
        kind="record",
        action_fps=10,
        video_fps=10,
        extra_meta={"video": True, "deferred_encoding": True},
    )
    encoder = DeferredVideoEncoder(
        root, fps=10, threads=2, merge=True, video_format="mp4"
    )
    recorder.add_action({}, {}, episode_index=0, elapsed_s=0.1)
    encoder.add_video(
        {"front": _jpeg(20), "side": _jpeg(80)}, episode_index=0, elapsed_s=0.1
    )
    encoder.add_video(
        {"front": _jpeg(40), "side": _jpeg(100)}, episode_index=0, elapsed_s=0.3
    )
    recorder.close()
    assert VideoLibrary(tmp_path).get("recording")["encoding"]["state"] == "recording"
    encoder._encode()
    row = VideoLibrary(tmp_path).get("recording")
    assert row["encoding"]["state"] == "done"
    assert row["encoding"]["percent"] == 100
    assert sorted(row["episodes"][0]["videos"]) == [
        "front.mp4",
        "merged.mp4",
        "side.mp4",
    ]
    assert row["episodes"][0]["video_frames"] == 3
    for name in ("front", "side", "merged"):
        path = root / "episodes" / "000000" / "videos" / f"{name}.mp4"
        capture = cv2.VideoCapture(str(path))
        try:
            assert capture.isOpened()
            assert int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) == 3
        finally:
            capture.release()
