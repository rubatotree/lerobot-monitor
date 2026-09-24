"""Bounded Record capture and checked publication into a LeRobot v3 dataset."""

from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .session import safe_cam_name
from .types import JOINT_ORDER


@dataclass(frozen=True)
class RecordSample:
    attempt: str
    observation: dict[str, float]
    action: dict[str, float]
    images: dict[str, bytes]


class RecordDatasetSession:
    """Keep capture off the bus thread and publish only confirmed episodes."""

    def __init__(
        self,
        root: Path,
        dataset_id: str,
        repo_id: str,
        task: str,
        camera_keys: dict[str, str],
        fps: int,
        encoder_threads: int,
        deferred: bool,
        on_done: Callable[[], None],
    ) -> None:
        self.root = Path(root).resolve()
        self.dataset_id = dataset_id
        self.repo_id = repo_id
        self.task = task
        self.camera_keys = camera_keys
        self.fps = fps
        self.encoder_threads = encoder_threads
        self.deferred = deferred
        self.session_id = uuid.uuid4().hex
        self.staging = self.root.parent / f".{self.root.name}.record-{self.session_id}"
        self.staging.mkdir(parents=True, exist_ok=False)
        self._on_done = on_done
        self._lock = threading.Lock()
        self._queue: queue.Queue[RecordSample | tuple[str, str]] = queue.Queue(maxsize=max(72, fps * 2 + 64))
        self._attempts: dict[str, dict[str, Any]] = {}
        self._accepted: queue.Queue[str | None] = queue.Queue()
        self._closed = False
        self._retain_unpublished = False
        self._released = False
        self._failed_attempt: str | None = None
        self._error: str | None = None
        self._status = "ready"
        self._saved = 0
        self._queued = 0
        self._ready_to_publish = threading.Event()
        if not deferred:
            self._ready_to_publish.set()
        self._capture = threading.Thread(target=self._capture_loop, name="record-dataset-capture", daemon=True)
        self._publisher = threading.Thread(target=self._publish_loop, name="record-dataset-publish", daemon=True)
        self._capture.start()
        self._publisher.start()

    @staticmethod
    def validate(
        root: Path,
        camera_keys: dict[str, str],
        fps: int,
        camera_shapes: dict[str, list[int]] | None = None,
    ) -> dict[str, Any]:
        info_path = Path(root) / "meta" / "info.json"
        if not info_path.is_file():
            raise ValueError("selected dataset has no meta/info.json")
        info = json.loads(info_path.read_text(encoding="utf-8"))
        if info.get("codebase_version") != "v3.0":
            raise ValueError("Record supports writable LeRobot v3 datasets")
        if int(info.get("fps") or 0) != fps:
            raise ValueError(f"dataset FPS is {info.get('fps')}; use that FPS for Record")
        features = info.get("features") or {}
        for name in ("action", "observation.state"):
            feature = features.get(name) or {}
            if feature.get("dtype") != "float32" or list(feature.get("shape") or []) != [len(JOINT_ORDER)]:
                raise ValueError(f"dataset feature {name} is incompatible with this arm")
            expected_names = [f"{joint}.pos" for joint in JOINT_ORDER]
            if list(feature.get("names") or []) != expected_names:
                raise ValueError(f"dataset feature {name} has a different joint order")
        videos = {key for key, value in features.items() if value.get("dtype") == "video"}
        if videos != set(camera_keys.values()):
            raise ValueError(f"dataset cameras {sorted(videos)} differ from enabled main-view cameras {sorted(camera_keys.values())}")
        if camera_shapes is not None:
            for source, key in camera_keys.items():
                expected = list(features[key].get("shape") or [])
                actual = camera_shapes.get(source)
                if actual != expected:
                    raise ValueError(f"camera {source} shape {actual} differs from Dataset {expected}")
        if any(value.get("dtype") not in {"video", "float32", "int64"} for value in features.values()):
            raise ValueError("dataset contains unsupported Record features")
        if not root.is_dir() or not os.access(root, os.W_OK):
            raise ValueError("dataset directory is not writable")
        return info

    def new_attempt(self) -> str:
        attempt = uuid.uuid4().hex
        folder = self.staging / attempt
        folder.mkdir()
        with self._lock:
            self._attempts[attempt] = {
                "folder": folder, "sealed": threading.Event(), "frames": 0, "submitted": 0, "discard": False,
            }
        return attempt

    def has_samples(self, attempt: str) -> bool:
        with self._lock:
            return bool(self._attempts[attempt]["submitted"])

    def add_sample(self, sample: RecordSample) -> None:
        with self._lock:
            if self._error:
                raise RuntimeError(self._error)
            if self._closed:
                raise RuntimeError("record dataset is closing")
            if self._queue.qsize() >= max(2, self.fps * 2):
                raise RuntimeError("record capture cannot keep up; current episode needs re-recording")
        try:
            self._queue.put_nowait(sample)
        except queue.Full as exc:
            raise RuntimeError("record capture cannot keep up; current episode needs re-recording") from exc
        with self._lock:
            self._attempts[sample.attempt]["submitted"] += 1

    def seal(self, attempt: str, *, discard: bool = False) -> None:
        with self._lock:
            entry = self._attempts[attempt]
            entry["discard"] = discard
        self._queue.put_nowait(("seal", attempt))

    def discard(self, attempt: str) -> None:
        with self._lock:
            entry = self._attempts[attempt]
            entry["discard"] = True
            already_sealed = entry["sealed"].is_set()
        if already_sealed:
            shutil.rmtree(entry["folder"], ignore_errors=True)

    def accept(self, attempt: str) -> None:
        with self._lock:
            if self._error:
                raise RuntimeError(self._error)
            self._queued += 1
        self._accepted.put(attempt)

    def stop(self, *, retain_unpublished: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._retain_unpublished = retain_unpublished
            self._status = "finalizing"
        self._ready_to_publish.set()
        # These waits happen in a helper, never on the robot's control thread.
        threading.Thread(target=self._finish, name="record-dataset-finish", daemon=True).start()

    def _finish(self) -> None:
        self._queue.put(("stop", ""))
        self._capture.join()
        self._accepted.put(None)
        self._publisher.join()
        with self._lock:
            if self._retain_unpublished and self._error is None:
                self._error = "E-stop: interrupted episode retained in staging and not published"
            self._status = "error" if self._error else "completed"
        if self._error is None:
            shutil.rmtree(self.staging, ignore_errors=True)
            self._release()
        elif self._failed_attempt is None:
            self._release()

    def clear_capture_error(self) -> None:
        with self._lock:
            if self._failed_attempt is None and self._error and self._error.startswith("capture failed:"):
                self._error = None

    def retry(self) -> None:
        with self._lock:
            if not self._closed or not self._error or not self._failed_attempt or self._publisher.is_alive():
                raise RuntimeError("no completed save failure to retry")
            self._error = None
            self._status = "saving"
            failed = self._failed_attempt
            self._failed_attempt = None
        self._publisher = threading.Thread(target=self._retry_loop, args=(failed,), name="record-dataset-retry", daemon=True)
        self._publisher.start()

    def _retry_loop(self, failed: str) -> None:
        try:
            entry = self._attempts[failed]
            if entry["frames"] > 0:
                self._publish(entry["folder"])
                with self._lock:
                    self._saved += 1
            shutil.rmtree(entry["folder"], ignore_errors=True)
            self._publish_loop()
        except Exception as exc:  # noqa: BLE001 - staged samples stay available
            with self._lock:
                self._error = f"dataset save failed: {exc}"
                self._failed_attempt = failed
        with self._lock:
            self._status = "error" if self._error else "completed"
        if self._error is None:
            shutil.rmtree(self.staging, ignore_errors=True)
            self._release()

    def _release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._on_done()

    def progress(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self._status,
                "saved": self._saved,
                "queued": self._queued,
                "error": self._error,
                "can_retry": self._failed_attempt is not None,
                "staging_path": str(self.staging) if self._error else None,
            }

    def _capture_loop(self) -> None:
        while True:
            job = self._queue.get()
            if isinstance(job, tuple):
                kind, attempt = job
                if kind == "stop":
                    return
                with self._lock:
                    entry = self._attempts[attempt]
                    entry["sealed"].set()
                    discard = entry["discard"]
                if discard:
                    shutil.rmtree(entry["folder"], ignore_errors=True)
                continue
            with self._lock:
                entry = self._attempts.get(job.attempt)
                if entry is None or entry["discard"]:
                    continue
                index = int(entry["frames"])
            try:
                folder = entry["folder"]
                images = folder / "images" / f"{index:06d}"
                images.mkdir(parents=True, exist_ok=True)
                for camera, jpeg in job.images.items():
                    (images / f"{safe_cam_name(camera)}.jpg").write_bytes(jpeg)
                sample = {"observation": job.observation, "action": job.action, "image_names": list(job.images)}
                with (folder / "samples.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(sample, allow_nan=False) + "\n")
                with self._lock:
                    entry["frames"] += 1
            except Exception as exc:  # noqa: BLE001 - surface disk failures to the operator
                with self._lock:
                    self._error = f"capture failed: {exc}"

    def _publish_loop(self) -> None:
        while True:
            attempt = self._accepted.get()
            if attempt is None:
                return
            with self._lock:
                entry = self._attempts[attempt]
                self._status = "saving"
            entry["sealed"].wait()
            self._ready_to_publish.wait()
            if self._error:
                continue
            try:
                if entry["frames"] > 0:
                    self._publish(entry["folder"])
                    with self._lock:
                        self._saved += 1
                shutil.rmtree(entry["folder"], ignore_errors=True)
            except Exception as exc:  # noqa: BLE001 - staged samples remain for retry
                with self._lock:
                    self._error = f"dataset save failed: {exc}"
                    self._status = "error"
                    self._failed_attempt = attempt
                return
            with self._lock:
                self._status = "finalizing" if self._closed else "ready"

    def _publish(self, folder: Path) -> None:
        from lerobot.configs.video import RGBEncoderConfig
        from lerobot.datasets.lerobot_dataset import LeRobotDataset

        stage = folder / "native"
        if stage.is_dir():
            shutil.rmtree(stage)
        shutil.copytree(self.root / "meta", stage / "meta")
        dataset = LeRobotDataset.resume(
            repo_id=self.repo_id,
            root=stage,
            rgb_encoder=RGBEncoderConfig(vcodec="h264", preset="ultrafast", crf=22),
            encoder_threads=self.encoder_threads,
            streaming_encoding=True,
            video_backend="pyav",
        )
        try:
            with (folder / "samples.jsonl").open(encoding="utf-8") as stream:
                for index, line in enumerate(stream):
                    sample = json.loads(line)
                    frame: dict[str, Any] = {
                        "action": np.asarray([sample["action"][joint] for joint in JOINT_ORDER], dtype=np.float32),
                        "observation.state": np.asarray(
                            [sample["observation"][joint] for joint in JOINT_ORDER], dtype=np.float32
                        ),
                        "task": self.task,
                    }
                    for source, key in self.camera_keys.items():
                        jpeg = (folder / "images" / f"{index:06d}" / f"{safe_cam_name(source)}.jpg").read_bytes()
                        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if image is None:
                            raise ValueError(f"cannot decode {source} frame {index}")
                        expected_shape = dataset.meta.features[key]["shape"]
                        if list(image.shape) != list(expected_shape):
                            raise ValueError(f"camera {source} resolution differs from dataset schema")
                        frame[key] = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                    dataset.add_frame(frame)
            dataset.save_episode(parallel_encoding=False)
        finally:
            dataset.finalize()
        self._commit(stage)

    def _commit(self, stage: Path) -> None:
        from huggingface_hub.utils import WeakFileLock
        from lerobot.datasets.lerobot_dataset import LeRobotDataset

        with WeakFileLock(self.root / ".record.lock"):
            self._commit_locked(stage, LeRobotDataset)

    def _commit_locked(self, stage: Path, dataset_type: Any) -> None:
        journal = self.root / ".record-publish"
        journal.mkdir(exist_ok=False)
        backup = journal / "meta"
        shutil.copytree(self.root / "meta", backup)
        new_files = [p.relative_to(stage) for p in stage.rglob("*") if p.is_file() and p.parts[len(stage.parts)] != "meta"]
        (journal / "files.json").write_text(json.dumps([str(p) for p in new_files]), encoding="utf-8")
        try:
            for relative in new_files:
                target = self.root / relative
                if target.exists():
                    raise FileExistsError(f"dataset file already exists: {relative}")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(stage / relative, target)
            for source in (stage / "meta").rglob("*"):
                if source.is_file() and source.name != "info.json":
                    target = self.root / source.relative_to(stage)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
            info_target = self.root / "meta" / "info.json"
            os.replace(stage / "meta" / "info.json", info_target)
            reader = dataset_type(repo_id=self.repo_id, root=self.root, video_backend="pyav")
            if reader.num_episodes < 1 or reader.num_frames < 1:
                raise ValueError("published dataset is empty")
            _ = reader[reader.num_frames - 1]
        except BaseException:
            self._restore(journal)
            raise
        shutil.rmtree(journal)

    def _restore(self, journal: Path) -> None:
        backup = journal / "meta"
        if backup.is_dir():
            shutil.rmtree(self.root / "meta")
            shutil.copytree(backup, self.root / "meta")
        files_path = journal / "files.json"
        if files_path.is_file():
            for relative in json.loads(files_path.read_text(encoding="utf-8")):
                (self.root / relative).unlink(missing_ok=True)
        shutil.rmtree(journal, ignore_errors=True)


def recover_record_publish(root: Path) -> None:
    """Roll back an interrupted append before opening the dataset for writing."""
    journal = root / ".record-publish"
    if journal.is_dir():
        # __new__ avoids starting capture threads just to run the recovery routine.
        recovery = object.__new__(RecordDatasetSession)
        recovery.root = root
        recovery._restore(journal)
