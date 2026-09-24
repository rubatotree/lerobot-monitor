"""Keep camera and dataset work off the robot's control thread."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .cameras import CameraHub
from .deferred_video import DeferredVideoEncoder
from .library import DatasetRecorder
from .thread_priority import set_current_thread_priority


@dataclass
class _ActionJob:
    observation: dict[str, float]
    action: dict[str, float] | None
    episode_index: int
    kind: str
    elapsed_s: float


@dataclass
class _VideoJob:
    episode_index: int
    elapsed_s: float


@dataclass
class _FinishJob:
    episode_index: int


class RecordingWorker:
    """Serialize recorder calls on a worker while coalescing pending video samples."""

    def __init__(self, recorder: DatasetRecorder, cameras: CameraHub) -> None:
        self._recorder = recorder
        self._cameras = cameras
        self.root = recorder.root
        self.session_id = recorder.session_id
        self.dataset_id = recorder.dataset_id
        self.episode_index = recorder.episode_index
        self.action_fps = recorder.action_fps
        self.video_fps = recorder.video_fps
        self.video = recorder.video
        self.deferred_encoding = bool(
            getattr(recorder, "deferred_encoding", False) and recorder.video
        )
        self._deferred = (
            DeferredVideoEncoder(
                recorder.root,
                fps=recorder.video_fps,
                threads=recorder.encoder_threads,
                merge=recorder.merge,
                video_format=recorder.video_format,
            )
            if self.deferred_encoding
            else None
        )
        self._condition = threading.Condition()
        self._jobs: deque[_ActionJob | _VideoJob | _FinishJob | None] = deque()
        self._pending_video: _VideoJob | None = None
        self._closing = False
        self._error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run, name="recording-worker", daemon=True
        )
        self._thread.start()

    def _check_error(self) -> None:
        if self._error is not None:
            raise RuntimeError("recording worker failed") from self._error

    def add_action(
        self,
        observation: dict[str, float],
        action: dict[str, float] | None,
        *,
        episode_index: int,
        kind: str,
        elapsed_s: float,
    ) -> None:
        job = _ActionJob(
            dict(observation),
            None if action is None else dict(action),
            episode_index,
            kind,
            elapsed_s,
        )
        with self._condition:
            self._check_error()
            if self._closing:
                raise RuntimeError("recording is closing")
            self._jobs.append(job)
            self._condition.notify()

    def request_video(self, *, episode_index: int, elapsed_s: float) -> None:
        if not self.video:
            return
        with self._condition:
            self._check_error()
            if self._closing:
                raise RuntimeError("recording is closing")
            if (
                self._pending_video is not None
                and self._pending_video.episode_index == episode_index
            ):
                self._pending_video.elapsed_s = max(
                    self._pending_video.elapsed_s, elapsed_s
                )
            else:
                job = _VideoJob(episode_index, elapsed_s)
                self._jobs.append(job)
                self._pending_video = job
                self._condition.notify()

    def finish_episode(self, index: int) -> None:
        with self._condition:
            self._check_error()
            if self._closing:
                raise RuntimeError("recording is closing")
            self._pending_video = None
            self._jobs.append(_FinishJob(index))
            self._condition.notify()

    def close(self) -> Path:
        with self._condition:
            if not self._closing:
                self._closing = True
                self._pending_video = None
                self._jobs.append(None)
                self._condition.notify()
        self._thread.join(timeout=600)
        if self._thread.is_alive():
            raise RuntimeError("recording worker did not finish in time")
        self._check_error()
        return self.root

    def _run(self) -> None:
        set_current_thread_priority(-1)
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: bool(self._jobs))
                    job = self._jobs.popleft()
                    if job is self._pending_video:
                        self._pending_video = None
                if job is None:
                    break
                if isinstance(job, _ActionJob):
                    self._recorder.add_action(
                        job.observation,
                        job.action,
                        episode_index=job.episode_index,
                        kind=job.kind,
                        elapsed_s=job.elapsed_s,
                    )
                elif isinstance(job, _VideoJob):
                    if self._deferred is not None:
                        images_jpeg = self._cameras.latest_main_jpeg_map()
                        if not images_jpeg:
                            images_jpeg = self._cameras.latest_jpeg_map()
                        self._deferred.add_video(
                            images_jpeg,
                            episode_index=job.episode_index,
                            elapsed_s=job.elapsed_s,
                        )
                    else:
                        images = self._cameras.latest_main_bgr_map()
                        if not images:
                            images = self._cameras.latest_bgr_map()
                        self._recorder.add_video(
                            images,
                            episode_index=job.episode_index,
                            elapsed_s=job.elapsed_s,
                        )
                elif isinstance(job, _FinishJob):
                    self._recorder.finish_episode(job.episode_index)
        except Exception as exc:  # noqa: BLE001 - forward worker failures to the control loop
            self._error = exc
        finally:
            try:
                self._recorder.close()
            except Exception as exc:  # noqa: BLE001 - preserve the first failure
                if self._error is None:
                    self._error = exc
            if self._deferred is not None:
                if self._error is None:
                    self._deferred.start_encoding()
                else:
                    self._deferred.fail(self._error)
