"""Complete, timestamped joint trajectories for physical playback."""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .preview import lerobot_episode_payload, local_episode_payload
from .types import JOINT_ORDER


@dataclass(frozen=True)
class JointTrajectory:
    times: tuple[float, ...]
    poses: tuple[dict[str, float], ...]
    source_hz: float | None

    @classmethod
    def load(cls, kind: str, root: Path, episode: int, source: str = "command") -> JointTrajectory:
        if source not in {"command", "state"}:
            raise ValueError("trajectory source must be command or state")
        if kind == "video":
            payload = local_episode_payload(root, episode, full=True)
        elif kind == "dataset":
            payload = lerobot_episode_payload(root, episode, full=True)
        else:
            raise ValueError("trajectory kind must be video or dataset")
        prefix = "act." if source == "command" else "obs."
        times = tuple(float(t) for t in payload.get("t") or ())
        series = payload.get("series") or {}
        tracks = {name: series[f"{prefix}{name}"] for name in JOINT_ORDER if f"{prefix}{name}" in series}
        if not times or not tracks:
            raise ValueError("episode has no joint trajectory for this source")
        if any(not math.isfinite(t) or t < 0 for t in times) or any(b <= a for a, b in zip(times, times[1:], strict=False)):
            raise ValueError("trajectory timestamps must increase strictly")
        if any(len(track) != len(times) for track in tracks.values()):
            raise ValueError("trajectory joint lengths do not match timestamps")
        poses = tuple({name: float(track[i]) for name, track in tracks.items()} for i in range(len(times)))
        if any(not math.isfinite(value) for pose in poses for value in pose.values()):
            raise ValueError("trajectory contains non-finite joint values")
        raw_hz = payload.get("action_fps") or payload.get("fps")
        hz = float(raw_hz) if raw_hz else None
        return cls(times, poses, hz if hz and math.isfinite(hz) and hz > 0 else None)

    @property
    def duration_s(self) -> float:
        return self.times[-1]

    def sample(self, elapsed_s: float, interpolation: str = "linear") -> dict[str, float]:
        if interpolation not in {"linear", "hold"}:
            raise ValueError("interpolation must be linear or hold")
        index = min(len(self.times) - 1, max(0, bisect.bisect_right(self.times, elapsed_s) - 1))
        left = self.poses[index]
        if interpolation == "hold" or index == len(self.times) - 1:
            return dict(left)
        span = self.times[index + 1] - self.times[index]
        alpha = min(1.0, max(0.0, (elapsed_s - self.times[index]) / span))
        right = self.poses[index + 1]
        return {name: value + alpha * (right[name] - value) for name, value in left.items()}
