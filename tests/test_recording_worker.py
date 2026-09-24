import threading
from pathlib import Path
from unittest.mock import MagicMock

from lerobot_monitor.recording_worker import RecordingWorker


def test_slow_video_processing_does_not_block_action_submission(tmp_path: Path) -> None:
    video_started = threading.Event()
    release_video = threading.Event()
    events: list[tuple[str, int]] = []

    class FakeRecorder:
        root = tmp_path
        session_id = "test"
        dataset_id = "test"
        episode_index = 0
        action_fps = 30
        video_fps = 30
        video = True

        def add_action(
            self,
            observation: dict[str, float],
            action: dict[str, float] | None,
            **kwargs: object,
        ) -> None:
            events.append(("action", int(observation["joint"])))

        def add_video(self, images: dict[str, object], **kwargs: object) -> None:
            video_started.set()
            assert release_video.wait(timeout=5)
            events.append(("video", int(float(kwargs["elapsed_s"]))))

        def finish_episode(self, index: int) -> None:
            events.append(("finish", index))

        def close(self) -> Path:
            events.append(("close", 0))
            return self.root

    cameras = MagicMock()
    cameras.latest_main_bgr_map.return_value = {"front": object()}
    worker = RecordingWorker(FakeRecorder(), cameras)  # type: ignore[arg-type]
    try:
        worker.add_action(
            {"joint": 0.0}, None, episode_index=0, kind="record", elapsed_s=0
        )
        worker.request_video(episode_index=0, elapsed_s=0)
        assert video_started.wait(timeout=2)
        for index in range(1, 11):
            worker.add_action(
                {"joint": float(index)},
                None,
                episode_index=0,
                kind="record",
                elapsed_s=index,
            )
            worker.request_video(episode_index=0, elapsed_s=index)
        assert sum(type(job).__name__ == "_VideoJob" for job in worker._jobs) == 1
        worker.finish_episode(0)
    finally:
        release_video.set()
        worker.close()
    assert [value for name, value in events if name == "action"] == list(range(11))
    assert [value for name, value in events if name == "video"] == [0, 10]
    assert events[-2:] == [("finish", 0), ("close", 0)]
