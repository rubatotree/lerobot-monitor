"""Episode preview for local video sessions and LeRobot / Hugging Face datasets.

Returns a compact payload for a visualize_dataset-style viewer: camera list,
time base, and downsampled state/action series.
"""

from __future__ import annotations

import csv
import json
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


def downsample(values: list[float], times: list[float], limit: int = MAX_SERIES_POINTS) -> tuple[list[float], list[float]]:
    n = len(values)
    if n <= limit or n == 0:
        return times, values
    step = max(1, n // limit)
    return times[::step], values[::step]


def series_from_joints_csv(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"t": [], "series": {}, "frames": 0, "fps": None}
    times: list[float] = []
    columns: dict[str, list[float]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for i, row in enumerate(reader):
            try:
                times.append(float(row.get("t") or i))
            except ValueError:
                times.append(float(i))
            for key, raw in row.items():
                if not key.startswith(("obs.", "act.")):
                    continue
                try:
                    columns.setdefault(key, []).append(float(raw))
                except (TypeError, ValueError):
                    columns.setdefault(key, []).append(float("nan"))
    frames = len(times)
    fps = None
    if frames >= 2 and times[-1] > times[0]:
        fps = round((frames - 1) / (times[-1] - times[0]), 2)
    series: dict[str, list[float]] = {}
    out_t = times
    for key, values in columns.items():
        t_ds, v_ds = downsample(values, times)
        series[key] = v_ds
        out_t = t_ds
    return {"t": out_t, "series": series, "frames": frames, "fps": fps}


def local_episode_payload(root: Path, index: int) -> dict[str, Any]:
    folder = root / "episodes" / f"{int(index):06d}"
    if not folder.is_dir():
        raise FileNotFoundError(f"episode {index}")
    videos_dir = folder / "videos"
    cameras: list[dict[str, str]] = []
    if videos_dir.is_dir():
        files = sorted(p for p in videos_dir.iterdir() if p.suffix.lower() in {".mp4", ".avi"})
        names = [p.stem for p in files]
        if "merged" in names:
            names = ["merged"] + [n for n in names if n != "merged"]
        cameras = [{"name": name} for name in names]
    payload = series_from_joints_csv(folder / "joints.csv")
    payload.update(
        {
            "kind": "video",
            "index": int(index),
            "cameras": cameras,
            "dir": str(folder),
        }
    )
    return payload


def _episode_tag(index: int) -> str:
    return f"episode_{int(index):06d}"


def _cam_name(video_key: str) -> str:
    if video_key.startswith("observation.images."):
        return video_key.split(".", 2)[-1]
    return video_key


def _episode_row(root: Path, index: int) -> dict[str, Any] | None:
    jsonl = root / "meta" / "episodes.jsonl"
    if jsonl.is_file():
        try:
            for i, line in enumerate(jsonl.read_text(encoding="utf-8").splitlines()):
                if not line.strip():
                    continue
                row = json.loads(line)
                if int(row.get("episode_index", i)) == int(index):
                    return row
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    try:
        import pandas as pd
    except ImportError:
        return None
    files = list((root / "meta").rglob("*.parquet")) if (root / "meta").is_dir() else []
    for path in files:
        try:
            frame = pd.read_parquet(path)
        except Exception:
            continue
        if "episode_index" in frame.columns:
            hit = frame[frame["episode_index"] == int(index)]
        elif int(index) < len(frame):
            hit = frame.iloc[[int(index)]]
        else:
            continue
        if hit.empty:
            continue
        return {str(k): hit.iloc[0][k] for k in hit.columns}
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


def _series_from_parquet(root: Path, index: int) -> dict[str, Any]:
    tag = _episode_tag(index)
    files = list(root.rglob(f"{tag}.parquet"))
    if not files:
        return {"t": [], "series": {}, "frames": 0, "fps": None}
    try:
        import pandas as pd
    except ImportError:
        return {"t": [], "series": {}, "frames": 0, "fps": None}
    try:
        frame = pd.read_parquet(files[0])
    except Exception:
        return {"t": [], "series": {}, "frames": 0, "fps": None}
    times: list[float]
    if "timestamp" in frame.columns:
        times = [float(v) for v in frame["timestamp"].tolist()]
    else:
        times = [float(i) for i in range(len(frame))]
    series: dict[str, list[float]] = {}
    for name in JOINT_ORDER:
        for prefix, col in (("obs", f"observation.state.{name}"), ("act", f"action.{name}")):
            if col in frame.columns:
                values = [float(v) for v in frame[col].tolist()]
                t_ds, v_ds = downsample(values, times)
                series[f"{prefix}.{name}"] = v_ds
                times = t_ds
    if "observation.state" in frame.columns and not any(k.startswith("obs.") for k in series):
        try:
            states = frame["observation.state"].tolist()
            for i, name in enumerate(JOINT_ORDER):
                values = [float(row[i]) for row in states if row is not None and len(row) > i]
                t_ds, v_ds = downsample(values, times[: len(values)])
                series[f"obs.{name}"] = v_ds
        except Exception:
            pass
    if "action" in frame.columns and not any(k.startswith("act.") for k in series):
        try:
            actions = frame["action"].tolist()
            for i, name in enumerate(JOINT_ORDER):
                values = [float(row[i]) for row in actions if row is not None and len(row) > i]
                t_ds, v_ds = downsample(values, times[: len(values)])
                series[f"act.{name}"] = v_ds
        except Exception:
            pass
    frames = len(times)
    fps = None
    if frames >= 2 and times[-1] > times[0]:
        fps = round((frames - 1) / (times[-1] - times[0]), 2)
    return {"t": times, "series": series, "frames": frames, "fps": fps}


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


def lerobot_episode_payload(root: Path, index: int) -> dict[str, Any]:
    videos = lerobot_episode_videos(root, index)
    cameras = [
        {
            "name": row["name"],
            "start": row.get("start") or 0.0,
            "end": row.get("end"),
        }
        for row in videos
    ]
    payload = _series_from_parquet(root, index)
    info = _read_json(root / "meta" / "info.json")
    payload.update(
        {
            "kind": "dataset",
            "index": int(index),
            "cameras": cameras,
            "fps": payload.get("fps") or info.get("fps"),
            "episodes": lerobot_episode_count(root),
            "task": info.get("task") or "",
        }
    )
    return payload


def find_lerobot_video(root: Path, index: int, cam: str) -> Path:
    wanted = _SAFE.sub("_", cam.strip()) or cam
    for row in lerobot_episode_videos(root, index):
        if row["name"] == cam or _SAFE.sub("_", row["name"]) == wanted:
            return Path(row["file"])
    raise FileNotFoundError(cam)
