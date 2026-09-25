"""Episode preview for local video sessions and LeRobot / Hugging Face datasets.

Returns a compact payload for a visualize_dataset-style viewer: camera list,
time base, and downsampled state/action series.
"""

from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from typing import Any

from .types import JOINT_ORDER

_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")
MAX_SERIES_POINTS = 400


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def sample_index(length: int, limit: int | None = MAX_SERIES_POINTS) -> list[int]:
    """One shared index for every series so all columns keep the same time base."""
    if length <= 0:
        return []
    if limit is None:
        return list(range(length))
    if limit <= 0:
        raise ValueError("sample limit must be positive")
    if length <= limit:
        return list(range(length))
    step = math.ceil(length / limit)
    return list(range(0, length, step))


def _pick(values: list[float], index: list[int]) -> list[float]:
    return [values[i] for i in index]


def _column(value: Any, position: int) -> float:
    try:
        return float(value[position])
    except (TypeError, ValueError, IndexError, KeyError):
        return float("nan")


def _number(value: Any) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def _relative_times(values: list[Any], fps: float | None = None) -> list[float]:
    """Build a finite, non-decreasing time axis starting at zero."""
    if not values:
        return []
    parsed = [_number(value) for value in values]
    step = 1.0 / fps if fps and fps > 0 else 1.0
    finite = [(i, value) for i, value in enumerate(parsed) if math.isfinite(value)]
    for (left_i, left), (right_i, right) in zip(finite, finite[1:], strict=False):
        candidate = (right - left) / (right_i - left_i)
        if candidate > 0 and math.isfinite(candidate):
            step = candidate
            break
    first_i, first_value = finite[0] if finite else (0, 0.0)
    estimated_start = first_value - first_i * step
    times: list[float] = []
    for i, value in enumerate(parsed):
        current = value if math.isfinite(value) else estimated_start + i * step
        if times and current < times[-1]:
            current = times[-1]
        times.append(current)
    origin = times[0]
    return [value - origin for value in times]


def _read_parquet_columns(path: Path) -> dict[str, list[Any]] | None:
    """Read parquet with either optional backend, without making preview fatal."""
    try:
        import pandas as pd

        frame = pd.read_parquet(path)
        return {str(name): frame[name].tolist() for name in frame.columns}
    except (ImportError, OSError, ValueError):
        pass
    except Exception:
        # A broken or unsupported parquet file should not break the monitor UI.
        pass
    try:
        import pyarrow.parquet as pq

        table = pq.read_table(path)
        return {str(name): table[name].to_pylist() for name in table.column_names}
    except (ImportError, OSError, ValueError):
        return None
    except Exception:
        return None


def _rows_from_columns(columns: dict[str, list[Any]]) -> list[dict[str, Any]]:
    length = max((len(values) for values in columns.values()), default=0)
    return [
        {name: values[i] if i < len(values) else None for name, values in columns.items()}
        for i in range(length)
    ]


def series_from_joints_csv(path: Path, *, full: bool = False) -> dict[str, Any]:
    if not path.is_file():
        return {"t": [], "series": {}, "frames": 0, "fps": None}
    times: list[float] = []
    columns: dict[str, list[float]] = {}
    order: list[str] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for i, row in enumerate(reader):
            try:
                times.append(float(row.get("t") or i))
            except (TypeError, ValueError):
                times.append(float(i))
            for key, raw in row.items():
                if not key or not key.startswith(("obs.", "act.")):
                    continue
                if key not in columns:
                    columns[key] = [float("nan")] * (len(times) - 1)
                    order.append(key)
                try:
                    columns[key].append(float(raw))
                except (TypeError, ValueError):
                    columns[key].append(float("nan"))
            for key in order:
                if len(columns[key]) < len(times):
                    columns[key].append(float("nan"))
    times = _relative_times(times)
    frames = len(times)
    fps = None
    if frames >= 2 and times[-1] > times[0]:
        fps = round((frames - 1) / (times[-1] - times[0]), 2)
    index = sample_index(frames, None if full else MAX_SERIES_POINTS)
    return {
        "t": _pick(times, index),
        "series": {key: _pick(values, index) for key, values in columns.items()},
        "frames": frames,
        "fps": fps,
    }


def local_episode_payload(root: Path, index: int, *, full: bool = False) -> dict[str, Any]:
    folder = root / "episodes" / f"{int(index):06d}"
    if not folder.is_dir():
        raise FileNotFoundError(f"episode {index}")
    videos_dir = folder / "videos"
    root_meta = _read_json(root / "meta.json")
    episode_meta = _read_json(folder / "meta.json")
    video_fps = episode_meta.get("video_fps") or root_meta.get("video_fps") or root_meta.get("fps")
    requested_video_fps = (
        episode_meta.get("requested_video_fps")
        or root_meta.get("requested_video_fps")
        or video_fps
    )
    encoded_video_fps = episode_meta.get("encoded_video_fps")
    if encoded_video_fps is None:
        encoded_video_fps = root_meta.get("encoded_video_fps")
    video_start_frames = episode_meta.get("video_start_frames") or {}
    per_camera_video_frames = episode_meta.get("per_camera_video_frames") or {}
    if encoded_video_fps is None and per_camera_video_frames:
        encoded_video_fps = video_fps
    cameras: list[dict[str, Any]] = []
    if videos_dir.is_dir():
        files = sorted(
            p
            for p in videos_dir.iterdir()
            if p.suffix.lower() in {".mp4", ".avi"} and p.stem != "merged"
        )
        names = [p.stem for p in files]
        cameras = [
            {
                "name": name,
                # The file itself still starts at clip time zero. This offset
                # positions it on the episode timeline when a camera appeared late.
                "timeline_offset_s": round(
                    float(video_start_frames.get(name, 0)) / float(video_fps), 6
                )
                if video_fps and float(video_fps) > 0
                else 0.0,
                "encoded_frames": int(per_camera_video_frames.get(name, 0)),
            }
            for name in names
        ]
    payload = series_from_joints_csv(folder / "joints.csv", full=full)
    action_fps = episode_meta.get("action_fps") or root_meta.get("action_fps") or root_meta.get("fps")
    duration_value = episode_meta.get("duration_s")
    duration_s = (
        float(duration_value)
        if duration_value is not None
        else max((float(value) for value in payload.get("t") or []), default=0.0)
    )
    payload.update(
        {
            "kind": "video",
            "index": int(index),
            "cameras": cameras,
            "dir": str(folder),
            "action_fps": action_fps,
            "video_fps": video_fps,
            "requested_video_fps": requested_video_fps,
            "encoded_video_fps": encoded_video_fps,
            "duration_s": duration_s,
            "task": str(episode_meta.get("task") or root_meta.get("task") or ""),
        }
    )
    return payload


def _episode_tag(index: int) -> str:
    return f"episode_{int(index):06d}"


def _cam_name(video_key: str) -> str:
    if video_key.startswith("observation.images."):
        return video_key.split(".", 2)[-1]
    return video_key


def _episode_rows(root: Path) -> list[dict[str, Any]]:
    jsonl = root / "meta" / "episodes.jsonl"
    if jsonl.is_file():
        rows: list[dict[str, Any]] = []
        try:
            for line in jsonl.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
        except OSError:
            pass
        if rows:
            return rows

    meta = root / "meta"
    if not meta.is_dir():
        return []
    files = set((meta / "episodes").rglob("*.parquet")) if (meta / "episodes").is_dir() else set()
    files.update(meta.glob("episodes*.parquet"))
    rows = []
    for path in sorted(files):
        columns = _read_parquet_columns(path)
        if not columns:
            continue
        rows.extend(_rows_from_columns(columns))
    return rows


def _episode_row(root: Path, index: int) -> dict[str, Any] | None:
    rows = _episode_rows(root)
    for position, row in enumerate(rows):
        try:
            if int(row.get("episode_index", position)) == int(index):
                return row
        except (TypeError, ValueError):
            continue
    if 0 <= int(index) < len(rows):
        return rows[int(index)]
    return None


def _format_video_path(template: str, video_key: str, index: int, chunk: int, file_index: int) -> str:
    try:
        return template.format(
            video_key=video_key,
            episode_index=index,
            chunk_index=chunk,
            file_index=file_index,
        )
    except (KeyError, ValueError):
        return template.replace("{video_key}", video_key).replace(
            "{episode_index:06d}", f"{index:06d}"
        )


def _format_data_path(template: str, index: int, chunk: int, file_index: int) -> str:
    try:
        return template.format(
            episode_index=index,
            chunk_index=chunk,
            file_index=file_index,
        )
    except (KeyError, ValueError):
        return template.replace("{episode_index:06d}", f"{index:06d}")


def lerobot_episode_videos(root: Path, index: int) -> list[dict[str, Any]]:
    tag = _episode_tag(index)
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not root.is_dir():
        return found
    for path in root.rglob(f"{tag}.mp4"):
        key = _cam_name(path.parent.name) or path.stem
        if key in seen:
            continue
        seen.add(key)
        found.append({"name": key, "file": str(path), "start": 0.0, "end": None})
    if found:
        found.sort(key=lambda row: row["name"])
        return found

    info = _read_json(root / "meta" / "info.json")
    features = info.get("features") or {}
    video_keys = [
        key
        for key, feat in features.items()
        if isinstance(feat, dict) and feat.get("dtype") in {"video", "image"}
    ]
    template = info.get("video_path") or "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"
    row = _episode_row(root, index)
    for key in video_keys:
        chunk = 0
        file_index = 0
        start = 0.0
        end = None
        if row:
            chunk = int(row.get(f"videos/{key}/chunk_index") or 0)
            file_index = int(row.get(f"videos/{key}/file_index") or 0)
            if row.get(f"videos/{key}/from_timestamp") is not None:
                start = float(row[f"videos/{key}/from_timestamp"])
            if row.get(f"videos/{key}/to_timestamp") is not None:
                end = float(row[f"videos/{key}/to_timestamp"])
        rel = _format_video_path(template, key, index, chunk, file_index)
        path = root / rel
        name = _cam_name(key)
        if not path.is_file() or name in seen:
            continue
        seen.add(name)
        found.append({"name": name, "file": str(path), "start": start, "end": end})
    found.sort(key=lambda item: item["name"])
    return found


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _episode_data_files(
    root: Path, index: int, row: dict[str, Any] | None
) -> tuple[list[Path], Path | None]:
    data = root / "data"
    legacy = sorted(data.rglob(f"{_episode_tag(index)}.parquet")) if data.is_dir() else []
    if legacy:
        return legacy, legacy[0]

    expected: Path | None = None
    if row:
        info = _read_json(root / "meta" / "info.json")
        template = info.get("data_path") or "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
        chunk = _integer(row.get("data/chunk_index"))
        file_index = _integer(row.get("data/file_index"))
        expected = root / _format_data_path(template, index, chunk, file_index)
        if expected.is_file():
            return [expected], expected

    if not data.is_dir():
        return [], expected
    shared = sorted(data.glob("chunk-*/file-*.parquet"))
    if not shared:
        shared = sorted(data.rglob("*.parquet"))
    return shared, expected


def _metadata_local_bounds(root: Path, row: dict[str, Any]) -> tuple[int, int] | None:
    start = _integer(row.get("dataset_from_index"), -1)
    end = _integer(row.get("dataset_to_index"), -1)
    if start < 0 or end < start:
        return None
    chunk = _integer(row.get("data/chunk_index"), -1)
    file_index = _integer(row.get("data/file_index"), -1)
    starts = [
        _integer(candidate.get("dataset_from_index"), -1)
        for candidate in _episode_rows(root)
        if _integer(candidate.get("data/chunk_index"), -2) == chunk
        and _integer(candidate.get("data/file_index"), -2) == file_index
    ]
    valid_starts = [value for value in starts if value >= 0]
    file_start = min(valid_starts) if valid_starts else 0
    return start - file_start, end - file_start


def _episode_positions(
    root: Path,
    columns: dict[str, list[Any]],
    index: int,
    row: dict[str, Any] | None,
    is_expected_file: bool,
    is_legacy_file: bool,
) -> list[int]:
    length = max((len(values) for values in columns.values()), default=0)
    episode_indices = columns.get("episode_index")
    if episode_indices is not None:
        return [i for i, value in enumerate(episode_indices) if _integer(value, -1) == index]

    if row and is_expected_file:
        start = _integer(row.get("dataset_from_index"), -1)
        end = _integer(row.get("dataset_to_index"), -1)
        global_indices = columns.get("index")
        if start >= 0 and end >= start and global_indices is not None:
            return [
                i
                for i, value in enumerate(global_indices)
                if start <= _integer(value, -1) < end
            ]
        bounds = _metadata_local_bounds(root, row)
        if bounds:
            local_start, local_end = bounds
            return list(range(max(0, local_start), min(length, local_end)))

    if is_legacy_file:
        return list(range(length))
    return []


def _series_from_parquet(root: Path, index: int, *, full: bool = False) -> dict[str, Any]:
    row = _episode_row(root, index)
    files, expected = _episode_data_files(root, index, row)
    selected: list[dict[str, Any]] = []
    for path in files:
        parquet = _read_parquet_columns(path)
        if not parquet:
            continue
        positions = _episode_positions(
            root,
            parquet,
            index,
            row,
            expected is not None and path == expected,
            path.stem == _episode_tag(index),
        )
        for position in positions:
            selected.append(
                {
                    name: values[position] if position < len(values) else None
                    for name, values in parquet.items()
                }
            )
        if positions and expected is not None and path == expected:
            break

    if not selected:
        return {"t": [], "series": {}, "frames": 0, "fps": None}

    info = _read_json(root / "meta" / "info.json")
    configured_fps = _number(info.get("fps"))
    fps_hint = configured_fps if math.isfinite(configured_fps) and configured_fps > 0 else None
    if "timestamp" in selected[0]:
        raw_times = [item.get("timestamp") for item in selected]
    elif "frame_index" in selected[0]:
        raw_times = [
            _number(item.get("frame_index")) / fps_hint if fps_hint else item.get("frame_index")
            for item in selected
        ]
    else:
        raw_times = [i / fps_hint if fps_hint else i for i in range(len(selected))]
    times = _relative_times(raw_times, fps_hint)

    columns: dict[str, list[float]] = {}
    available = set().union(*(item.keys() for item in selected))
    for name in JOINT_ORDER:
        for prefix, column_name in (("obs", f"observation.state.{name}"), ("act", f"action.{name}")):
            if column_name in available:
                columns[f"{prefix}.{name}"] = [_number(item.get(column_name)) for item in selected]
    if "observation.state" in available and not any(key.startswith("obs.") for key in columns):
        for position, name in enumerate(JOINT_ORDER):
            columns[f"obs.{name}"] = [_column(item.get("observation.state"), position) for item in selected]
    if "action" in available and not any(key.startswith("act.") for key in columns):
        for position, name in enumerate(JOINT_ORDER):
            columns[f"act.{name}"] = [_column(item.get("action"), position) for item in selected]

    frames = len(times)
    fps = None
    if frames >= 2 and times[-1] > times[0]:
        fps = round((frames - 1) / (times[-1] - times[0]), 2)
    sample = sample_index(frames, None if full else MAX_SERIES_POINTS)
    return {
        "t": _pick(times, sample),
        "series": {key: _pick(values, sample) for key, values in columns.items()},
        "frames": frames,
        "fps": fps,
    }


def lerobot_episode_count(root: Path) -> int:
    info = _read_json(root / "meta" / "info.json")
    total = info.get("total_episodes") or info.get("total_episodes_in_set")
    if total:
        return int(total)
    videos = list(root.rglob("episode_*.mp4"))
    indices = set()
    for path in videos:
        m = re.search(r"episode_(\d+)", path.stem)
        if m:
            indices.add(int(m.group(1)))
    return max(indices) + 1 if indices else 0


def lerobot_episode_payload(root: Path, index: int, *, full: bool = False) -> dict[str, Any]:
    videos = lerobot_episode_videos(root, index)
    cameras = [
        {
            "name": row["name"],
            "start": row.get("start") or 0.0,
            "end": row.get("end"),
        }
        for row in videos
    ]
    payload = _series_from_parquet(root, index, full=full)
    info = _read_json(root / "meta" / "info.json")
    quality = _read_json(root / "meta" / "quality" / f"episode_{int(index):06d}.json")
    quality_summary = {
        "status": quality.get("status", "unknown"),
        "filled_count": int(quality.get("filled_count") or 0),
        "frames": int(quality.get("frames") or 0),
        "filled_frames": [row["frame_index"] for row in quality.get("samples", []) if row.get("filled")],
    } if quality else None
    payload.update(
        {
            "kind": "dataset",
            "index": int(index),
            "cameras": cameras,
            "fps": payload.get("fps") or info.get("fps"),
            "action_fps": info.get("action_fps") or info.get("fps"),
            "video_fps": info.get("video_fps") or info.get("fps"),
            "episodes": lerobot_episode_count(root),
            "task": info.get("task") or "",
            "quality": quality_summary,
        }
    )
    return payload


def find_lerobot_video(root: Path, index: int, cam: str) -> Path:
    wanted = _SAFE.sub("_", cam.strip()) or cam
    for row in lerobot_episode_videos(root, index):
        if row["name"] == cam or _SAFE.sub("_", row["name"]) == wanted:
            return Path(row["file"])
    raise FileNotFoundError(cam)
