"""Store camera JPEGs during recording and encode them after capture ends."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import cv2

from .library import _read_json, _write_json, episode_dir
from .session import mosaic_bgr, safe_cam_name
from .thread_priority import set_current_thread_priority


class DeferredVideoEncoder:
    def __init__(
        self, root: Path, *, fps: int, threads: int, merge: bool, video_format: str
    ) -> None:
        self.root = Path(root)
        self.fps = int(fps)
        self.threads = int(threads)
        self.merge = bool(merge)
        self.video_format = video_format
        self._last_slot: dict[tuple[int, str], int] = {}
        self._last_file: dict[tuple[int, str], Path] = {}
        self._status_path = self.root / "encoding.json"
        self._status("recording", 0, "Saving camera images")

    def _status(
        self, state: str, percent: int, message: str, *, error: str = ""
    ) -> None:
        _write_json(
            self._status_path,
            {
                "state": state,
                "percent": max(0, min(100, percent)),
                "message": message,
                "error": error,
            },
        )

    @staticmethod
    def _frame_path(folder: Path, slot: int) -> Path:
        return folder / f"{slot:09d}.jpg"

    @staticmethod
    def _repeat(previous: Path, target: Path) -> None:
        try:
            os.link(previous, target)
        except OSError:
            shutil.copyfile(previous, target)

    def add_video(
        self, images_jpeg: dict[str, bytes], *, episode_index: int, elapsed_s: float
    ) -> None:
        slot = max(0, int(max(0.0, elapsed_s) * self.fps + 0.5) - 1)
        current: set[str] = set()
        for name, jpeg in images_jpeg.items():
            if not jpeg:
                continue
            key = (episode_index, safe_cam_name(name))
            current.add(key[1])
            previous_slot = self._last_slot.get(key, -1)
            if slot <= previous_slot:
                continue
            folder = episode_dir(self.root, episode_index) / "frames" / key[1]
            folder.mkdir(parents=True, exist_ok=True)
            if previous_slot < 0:
                first = self._frame_path(folder, 0)
                first.write_bytes(jpeg)
                previous_slot = 0
                self._last_file[key] = first
                preview = episode_dir(self.root, episode_index) / "preview.jpg"
                if not preview.exists():
                    preview.write_bytes(jpeg)
            previous = self._last_file[key]
            for index in range(previous_slot + 1, slot):
                repeated = self._frame_path(folder, index)
                self._repeat(previous, repeated)
                previous = repeated
            if slot > previous_slot:
                target = self._frame_path(folder, slot)
                target.write_bytes(jpeg)
                previous = target
            self._last_file[key] = previous
            self._last_slot[key] = slot
        for key, previous in list(self._last_file.items()):
            if key[0] != episode_index or key[1] in current:
                continue
            previous_slot = self._last_slot[key]
            folder = previous.parent
            for index in range(previous_slot + 1, slot + 1):
                repeated = self._frame_path(folder, index)
                self._repeat(previous, repeated)
                previous = repeated
            self._last_file[key] = previous
            self._last_slot[key] = max(previous_slot, slot)

    def start_encoding(self) -> None:
        thread = threading.Thread(
            target=self._encode_safely, name="post-record-video-encoder", daemon=True
        )
        thread.start()

    def fail(self, exc: Exception) -> None:
        self._status("failed", 0, "Video recording failed", error=str(exc))

    def _encode_safely(self) -> None:
        set_current_thread_priority(-1)
        try:
            self._encode()
        except Exception as exc:  # noqa: BLE001 - report background failures in Videos
            self._status("failed", 0, "Video encoding failed", error=str(exc))

    def _encode(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("FFmpeg is required to encode saved camera images")
        episodes = sorted((self.root / "episodes").glob("[0-9]*"))
        jobs: list[tuple[Path, int, Path]] = []
        episode_counts: dict[int, dict[str, int]] = {}
        for episode in episodes:
            frames_root = episode / "frames"
            if not frames_root.is_dir():
                continue
            counts: dict[str, int] = {}
            for folder in sorted(frames_root.iterdir()):
                if not folder.is_dir():
                    continue
                count = len(list(folder.glob("*.jpg")))
                if count:
                    counts[folder.name] = count
                    jobs.append(
                        (
                            folder,
                            count,
                            episode / "videos" / f"{folder.name}.{self.video_format}",
                        )
                    )
            if counts:
                episode_counts[int(episode.name)] = counts
        if not jobs:
            raise RuntimeError(
                "No camera images were captured; no video can be encoded"
            )
        total_frames = sum(count for _, count, _ in jobs)
        if self.merge:
            total_frames += sum(
                max(counts.values())
                for counts in episode_counts.values()
                if len(counts) > 1
            )
        done_frames = 0
        self._status("encoding", 0, "Encoding camera videos")
        for folder, count, output in jobs:
            self._encode_sequence(
                ffmpeg, folder, count, output, done_frames, total_frames
            )
            done_frames += count
            self._status(
                "encoding",
                int(done_frames * 100 / total_frames),
                f"Encoded {output.name}",
            )
        if self.merge:
            for index, counts in episode_counts.items():
                if len(counts) <= 1:
                    continue
                episode = episode_dir(self.root, index)
                merged_frames = episode / "frames" / "merged"
                count = max(counts.values())
                self._build_merged_frames(episode, counts, merged_frames, count)
                output = episode / "videos" / f"merged.{self.video_format}"
                self._encode_sequence(
                    ffmpeg, merged_frames, count, output, done_frames, total_frames
                )
                done_frames += count
                self._status(
                    "encoding",
                    int(done_frames * 100 / total_frames),
                    f"Encoded {output.name}",
                )
        self._update_metadata(episode_counts)
        self._status("done", 100, "Videos ready")

    def _encode_sequence(
        self,
        ffmpeg: str,
        folder: Path,
        count: int,
        output: Path,
        done_frames: int,
        total_frames: int,
    ) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".part")
        is_avi = self.video_format == "avi"
        command = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-framerate",
            str(self.fps),
            "-start_number",
            "0",
            "-i",
            str(folder / "%09d.jpg"),
            "-frames:v",
            str(count),
            "-an",
            "-c:v",
            "libxvid" if is_avi else "libx264",
            "-threads",
            str(self.threads),
        ]
        if not is_avi:
            command += [
                "-preset",
                "ultrafast",
                "-crf",
                "22",
                "-vf",
                "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
            ]
        command += [
            "-progress",
            "pipe:1",
            "-nostats",
            "-f",
            "avi" if is_avi else "mp4",
            str(temporary),
        ]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        if os.name == "nt":
            flags |= subprocess.BELOW_NORMAL_PRIORITY_CLASS
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags
        )
        last_update = 0.0
        assert process.stdout is not None
        for raw in process.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("frame="):
                continue
            try:
                frame = min(count, int(line.partition("=")[2]))
            except ValueError:
                continue
            now = time.monotonic()
            if now - last_update >= 0.3:
                percent = int((done_frames + frame) * 100 / total_frames)
                self._status("encoding", percent, f"Encoding {output.name}")
                last_update = now
        error = (
            process.stderr.read().decode("utf-8", errors="replace")
            if process.stderr
            else ""
        )
        returncode = process.wait()
        if returncode:
            raise RuntimeError(
                f"FFmpeg could not encode {output.name}: {error.strip()}"
            )
        temporary.replace(output)

    def _build_merged_frames(
        self,
        episode: Path,
        counts: dict[str, int],
        folder: Path,
        total: int,
    ) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        for slot in range(total):
            images = {}
            for name, count in counts.items():
                frame = cv2.imread(
                    str(
                        self._frame_path(
                            episode / "frames" / name, min(slot, count - 1)
                        )
                    )
                )
                if frame is None:
                    raise RuntimeError(
                        f"Cannot read saved image for {name}, frame {slot}"
                    )
                images[name] = frame
            merged = mosaic_bgr(images)
            if merged is None or not cv2.imwrite(
                str(self._frame_path(folder, slot)), merged
            ):
                raise RuntimeError(f"Cannot save merged image at frame {slot}")

    def _update_metadata(self, episode_counts: dict[int, dict[str, int]]) -> None:
        root_path = self.root / "meta.json"
        root_meta = _read_json(root_path)
        all_rows: list[dict[str, Any]] = []
        total_video_frames = 0
        for row in root_meta.get("episodes") or []:
            index = int(row.get("index", -1))
            counts = dict(episode_counts.get(index) or {})
            if counts:
                if self.merge and len(counts) > 1:
                    counts["merged"] = max(counts.values())
                count = max(counts.values())
                total_video_frames += count
                row.update(
                    {
                        "videos": [
                            f"{name}.{self.video_format}" for name in sorted(counts)
                        ],
                        "video_frames": count,
                        "actual_video_frames": count,
                        "requested_video_frames": count,
                        "effective_video_frames": count,
                        "per_camera_video_frames": dict(sorted(counts.items())),
                        "video_start_frames": {name: 0 for name in counts},
                        "encoded_video_fps": self.fps,
                        "effective_encoding_fps": self.fps,
                        "effective_video_fps": self.fps,
                        "duration_s": max(
                            float(row.get("duration_s") or 0), count / self.fps
                        ),
                    }
                )
                _write_json(episode_dir(self.root, index) / "meta.json", row)
            all_rows.append(row)
        root_meta["episodes"] = all_rows
        root_meta["video_frames"] = total_video_frames
        root_meta["actual_video_frames"] = total_video_frames
        root_meta["requested_video_frames"] = total_video_frames
        root_meta["effective_video_frames"] = total_video_frames
        root_meta["duration_s"] = round(
            sum(float(row.get("duration_s") or 0) for row in all_rows), 4
        )
        _write_json(root_path, root_meta)
