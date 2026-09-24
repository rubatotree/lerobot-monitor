import shutil
import threading
from pathlib import Path

import cv2
import numpy as np
import pytest

from lerobot_monitor.session import (
    EpisodeWriter,
    SessionWriter,
    StreamingVideoWriter,
    list_sessions,
    video_copies,
)
from lerobot_monitor.types import JOINT_ORDER


def test_streaming_encoder_keeps_control_writes_nonblocking_under_backpressure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lerobot_monitor import session as session_module

    entered = threading.Event()
    allow_write = threading.Event()
    encoded: list[int] = []

    class FakeInput:
        closed = False

        def write(self, raw: memoryview) -> int:
            entered.set()
            assert allow_write.wait(timeout=5)
            encoded.append(raw[0])
            return len(raw)

        def close(self) -> None:
            self.closed = True

    class FakeError:
        def read(self) -> bytes:
            return b""

        def close(self) -> None:
            pass

    class FakeProcess:
        def __init__(self, command: list[str], **_: object) -> None:
            self.args = command
            self.stdin = FakeInput()
            self.stderr = FakeError()
            self.returncode = 0

        def wait(self, timeout: int | None = None) -> int:
            return 0

    monkeypatch.setattr(session_module.shutil, "which", lambda _: "ffmpeg")
    monkeypatch.setattr(session_module.subprocess, "Popen", FakeProcess)
    writer = StreamingVideoWriter(tmp_path / "front.mp4", 30, (4, 4), 2)
    try:
        writer.write(np.zeros((4, 4, 3), dtype=np.uint8))
        assert entered.wait(timeout=2)
        for value in range(1, 10):
            writer.write(np.full((4, 4, 3), value, dtype=np.uint8))
        assert len(writer._pending) <= 2
    finally:
        allow_write.set()
        writer.release()
    assert len(encoded) == 10
    assert encoded[0] == 0
    assert encoded[-1] == 9


def test_streaming_encoder_uses_configured_threads_and_writes_h264(tmp_path: Path) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg is required for streaming encoding")
    writer = EpisodeWriter(
        tmp_path / "episode", index=0, action_fps=10, video_fps=10,
        streaming_encoding=True, encoder_threads=3, merge=False,
    )
    writer.add_video({"front": np.zeros((48, 64, 3), dtype=np.uint8)}, elapsed_s=0.1)
    process_args = writer._videos["front"]._process.args
    assert process_args[process_args.index("-threads") + 1] == "3"
    info = writer.close()
    assert info["video_frames"] == 1
    capture = cv2.VideoCapture(str(tmp_path / "episode" / "videos" / "front.mp4"))
    try:
        assert capture.isOpened()
        fourcc = int(capture.get(cv2.CAP_PROP_FOURCC))
        codec = "".join(chr((fourcc >> (8 * offset)) & 0xFF) for offset in range(4))
        assert codec.lower() in {"avc1", "h264", "x264"}
    finally:
        capture.release()


def test_streaming_encoder_closes_two_cameras_and_mosaic(tmp_path: Path) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg is required for streaming encoding")
    writer = EpisodeWriter(
        tmp_path / "episode", index=0, action_fps=10, video_fps=10,
        streaming_encoding=True, encoder_threads=2, merge=True,
    )
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    for index in range(3):
        writer.add_video({"front": frame, "side": frame}, elapsed_s=(index + 1) / 10)
    info = writer.close()
    assert info["video_frames"] == 3
    assert info["videos"] == ["front.mp4", "merged.mp4", "side.mp4"]
    for name in ("front", "merged", "side"):
        capture = cv2.VideoCapture(str(tmp_path / "episode" / "videos" / f"{name}.mp4"))
        try:
            assert capture.isOpened()
            assert int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) == 3
        finally:
            capture.release()


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
    assert (out / "videos" / "merged.mp4").is_file()
    rows = (out / "joints.csv").read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 3  # header + 2 frames
    sessions = list_sessions(tmp_path)
    assert sessions[0]["kind"] == "rollout"
    assert sessions[0]["frames"] >= 2


def test_video_copies_pads_to_wall_clock() -> None:
    assert video_copies(0.0, 15, 0) == 0
    assert video_copies(1.0 / 15.0, 15, 1) == 0
    assert video_copies(0.4, 15, 1) >= 5
    assert video_copies(20.0, 15, 0) == 300


def test_episode_writer_keeps_camera_timelines_aligned(tmp_path: Path, monkeypatch) -> None:
    from lerobot_monitor import session as session_module

    class FakeVideoWriter:
        def __init__(self) -> None:
            self.frames: list[np.ndarray] = []
            self.released = False

        def write(self, frame: np.ndarray) -> None:
            self.frames.append(frame.copy())

        def release(self) -> None:
            self.released = True

    writers: dict[str, FakeVideoWriter] = {}

    def fake_open(path: Path, fps: float, size: tuple[int, int], fmt: str = "mp4") -> FakeVideoWriter:
        writer = FakeVideoWriter()
        writers[path.stem] = writer
        return writer

    monkeypatch.setattr(session_module, "open_video_writer", fake_open)
    writer = EpisodeWriter(tmp_path / "episode", index=0, action_fps=5, video_fps=1, merge=True)
    front_first = np.full((8, 10, 3), 10, dtype=np.uint8)
    front_last = np.full((8, 10, 3), 20, dtype=np.uint8)
    side_first = np.full((8, 10, 3), 30, dtype=np.uint8)
    side_last = np.full((8, 10, 3), 40, dtype=np.uint8)
    for elapsed, images in (
        (1.0, {"front": front_first}),
        (2.0, {}),
        (3.0, {"side": side_first}),
        (4.0, {"front": front_last, "side": side_last}),
    ):
        writer.add_action({}, None, elapsed_s=elapsed)
        writer.add_video(images, elapsed_s=elapsed)
    info = writer.close()

    assert info["frames"] == 4
    assert set(writers) == {"front", "side", "merged"}
    assert len(writers["front"].frames) == 4
    assert len(writers["merged"].frames) == 4
    assert len(writers["side"].frames) == 2
    assert all(video.released for video in writers.values())
    assert [int(frame[0, 0, 0]) for frame in writers["front"].frames] == [10, 10, 10, 20]
    assert [int(frame[0, 0, 0]) for frame in writers["side"].frames] == [30, 40]
    assert info["video_start_frames"]["side"] == 2
    assert info["per_camera_video_frames"]["side"] == 2


def test_episode_writer_separates_action_and_video_rates(tmp_path: Path, monkeypatch) -> None:
    from lerobot_monitor import session as session_module

    class FakeVideoWriter:
        def __init__(self) -> None:
            self.frames: list[np.ndarray] = []

        def write(self, frame: np.ndarray) -> None:
            self.frames.append(frame.copy())

        def release(self) -> None:
            pass

    videos: dict[str, FakeVideoWriter] = {}

    def fake_open(path: Path, fps: float, size: tuple[int, int], fmt: str = "mp4") -> FakeVideoWriter:
        videos[path.stem] = FakeVideoWriter()
        return videos[path.stem]

    monkeypatch.setattr(session_module, "open_video_writer", fake_open)
    writer = EpisodeWriter(
        tmp_path / "episode",
        index=0,
        action_fps=2,
        video_fps=4,
        merge=False,
    )
    frame = np.zeros((4, 6, 3), dtype=np.uint8)
    writer.add_action({}, None, elapsed_s=0.0)
    writer.add_action({}, None, elapsed_s=0.5)
    for elapsed in (0.25, 0.5, 0.75, 1.0):
        writer.add_video({"front": frame}, elapsed_s=elapsed)
    info = writer.close()

    assert info["frames"] == info["action_frames"] == 2
    assert info["video_frames"] == 4
    assert info["action_fps"] == 2
    assert info["video_fps"] == 4
    assert info["duration_s"] == 1.0
    assert len(videos["front"].frames) == 4


def test_video_stall_uses_last_frame_for_history_and_current_for_latest(tmp_path: Path, monkeypatch) -> None:
    from lerobot_monitor import session as session_module

    class FakeVideoWriter:
        def __init__(self) -> None:
            self.frames: list[np.ndarray] = []

        def write(self, frame: np.ndarray) -> None:
            self.frames.append(frame.copy())

        def release(self) -> None:
            pass

    fake = FakeVideoWriter()
    monkeypatch.setattr(session_module, "open_video_writer", lambda *args, **kwargs: fake)
    writer = EpisodeWriter(tmp_path / "episode", index=0, action_fps=2, video_fps=10, merge=False)
    writer.add_video({"front": np.full((2, 2, 3), 10, dtype=np.uint8)}, elapsed_s=0.1)
    writer.add_video({"front": np.full((2, 2, 3), 20, dtype=np.uint8)}, elapsed_s=0.5)
    writer.close()

    assert [int(frame[0, 0, 0]) for frame in fake.frames] == [10, 10, 10, 10, 20]


def test_video_disabled_or_camera_absent_reports_no_actual_frames(tmp_path: Path) -> None:
    disabled = EpisodeWriter(tmp_path / "disabled", index=0, action_fps=5, video_fps=30, video=False)
    disabled.add_video({"front": np.zeros((2, 2, 3), dtype=np.uint8)}, elapsed_s=2.0)
    assert disabled.close()["video_frames"] == 0

    absent = EpisodeWriter(tmp_path / "absent", index=0, action_fps=5, video_fps=30, video=True)
    absent.add_video({}, elapsed_s=2.0)
    info = absent.close()
    assert info["requested_video_frames"] == 60
    assert info["video_frames"] == info["actual_video_frames"] == 0
    assert info["per_camera_video_frames"] == {}


def test_sparse_actions_preserve_real_episode_duration(tmp_path: Path) -> None:
    writer = EpisodeWriter(
        tmp_path / "sparse",
        index=0,
        action_fps=15,
        video_fps=30,
        video=False,
    )
    writer.add_action({}, None, elapsed_s=0.0)
    writer.add_action({}, None, elapsed_s=10.0)

    info = writer.close()

    assert info["duration_s"] == pytest.approx(10.0)
    assert info["wall_s"] == pytest.approx(10.0)
