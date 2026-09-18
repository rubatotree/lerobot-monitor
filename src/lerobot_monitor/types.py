"""Shared joint names, limits, and pose helpers.

Units match LeRobot SO-101 calibration: arm joints in degrees, gripper in [0, 100].
"""

from __future__ import annotations

from typing import Mapping

JOINT_ORDER: tuple[str, ...] = (
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
)

JOINT_LIMITS: dict[str, tuple[float, float]] = {
    "shoulder_pan": (-180.0, 180.0),
    "shoulder_lift": (-180.0, 180.0),
    "elbow_flex": (-180.0, 180.0),
    "wrist_flex": (-180.0, 180.0),
    "wrist_roll": (-180.0, 180.0),
    "gripper": (0.0, 100.0),
}

# Folded rest pose (former "home"). Used as the default relax preset so the
# arm can be parked before torque is dropped.
RELAX_POSE: dict[str, float] = {
    "shoulder_pan": -4.0,
    "shoulder_lift": -103.0,
    "elbow_flex": 97.0,
    "wrist_flex": 78.0,
    "wrist_roll": -65.0,
    "gripper": 0.0,
}

ZERO_POSE: dict[str, float] = {name: 0.0 for name in JOINT_ORDER}

HOME_POSE = RELAX_POSE
PRESETS: dict[str, dict[str, float]] = {
    "relax": dict(RELAX_POSE),
    "zero": dict(ZERO_POSE),
}

ARRIVAL_TOLERANCE: dict[str, float] = {
    "shoulder_pan": 2.0,
    "shoulder_lift": 2.0,
    "elbow_flex": 2.0,
    "wrist_flex": 2.0,
    "wrist_roll": 2.0,
    "gripper": 3.0,
}


def observation_to_pose(obs: Mapping[str, object]) -> dict[str, float]:
    pose: dict[str, float] = {}
    for name in JOINT_ORDER:
        key = f"{name}.pos"
        if key in obs:
            pose[name] = float(obs[key])  # type: ignore[arg-type]
        elif name in obs:
            pose[name] = float(obs[name])  # type: ignore[arg-type]
    return pose


def pose_to_action(pose: Mapping[str, float]) -> dict[str, float]:
    return {f"{name}.pos": float(pose[name]) for name in JOINT_ORDER if name in pose}


def merge_partial(current: Mapping[str, float], partial: Mapping[str, float]) -> dict[str, float]:
    merged = {name: float(current[name]) for name in JOINT_ORDER if name in current}
    for name, value in partial.items():
        if name not in JOINT_ORDER:
            raise ValueError(f"Unknown joint '{name}'. Expected one of: {', '.join(JOINT_ORDER)}")
        lo, hi = JOINT_LIMITS[name]
        merged[name] = max(lo, min(hi, float(value)))
    return merged


def lerp_pose(start: Mapping[str, float], goal: Mapping[str, float], alpha: float) -> dict[str, float]:
    a = max(0.0, min(1.0, alpha))
    return {name: float(start[name]) + a * (float(goal[name]) - float(start[name])) for name in JOINT_ORDER}


def clamp_pose(pose: Mapping[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for name in JOINT_ORDER:
        if name not in pose:
            continue
        lo, hi = JOINT_LIMITS[name]
        out[name] = max(lo, min(hi, float(pose[name])))
    return out
