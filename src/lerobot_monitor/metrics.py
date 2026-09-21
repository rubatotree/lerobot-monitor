"""Score a predicted action chunk against the reference commands of an episode.

The metrics follow what embodied-AI papers report when comparing a policy
rollout with a recorded demonstration: per-joint MAE / RMSE, range-normalised
RMSE, endpoint error, and dynamic time warping for trajectory shape.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np

JOINT_SCALE_FLOOR = 0.02
"""Smallest per-joint span used for normalisation (rad, about 1.1 degrees)."""


def _matrix(samples: Sequence[Mapping[str, float]], names: Sequence[str]) -> np.ndarray:
    """Return a (steps, joints) array; missing or non-finite entries become NaN."""
    matrix = np.full((len(samples), len(names)), np.nan, dtype=float)
    for row, sample in enumerate(samples):
        for column, name in enumerate(names):
            try:
                value = float(sample.get(name))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if np.isfinite(value):
                matrix[row, column] = value
    return matrix


def dtw_distance(reference: np.ndarray, predicted: np.ndarray) -> float:
    """Normalised dynamic time warping distance between two (steps,) trajectories."""
    reference = np.asarray(reference, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    rows, columns = int(reference.size), int(predicted.size)
    cost = np.full((rows + 1, columns + 1), np.inf, dtype=float)
    cost[0, 0] = 0.0
    for row in range(1, rows + 1):
        for column in range(1, columns + 1):
            step = abs(float(reference[row - 1]) - float(predicted[column - 1]))
            cost[row, column] = step + min(
                cost[row - 1, column],
                cost[row, column - 1],
                cost[row - 1, column - 1],
            )
    return float(cost[rows, columns] / max(rows, columns))


def evaluate_action_chunk(
    predicted: Sequence[Mapping[str, float]],
    reference: Sequence[Mapping[str, float]],
) -> dict[str, Any] | None:
    """Compare a predicted chunk with reference commands sampled at the same times.

    ``predicted`` and ``reference`` are per-step joint dictionaries. Steps beyond
    the shorter sequence are ignored, and joints missing from any step are
    skipped instead of being imputed.
    """
    steps = min(len(predicted), len(reference))
    if steps <= 0:
        return None
    common: set[str] = set(reference[0]) & set(predicted[0])
    for row in range(1, steps):
        common &= set(reference[row]) & set(predicted[row])
    names = sorted(common)
    if not names:
        return None

    reference_matrix = _matrix(reference[:steps], names)
    predicted_matrix = _matrix(predicted[:steps], names)
    usable = [
        column
        for column in range(len(names))
        if np.isfinite(reference_matrix[:, column]).all() and np.isfinite(predicted_matrix[:, column]).all()
    ]
    if not usable:
        return None
    names = [names[column] for column in usable]
    reference_matrix = reference_matrix[:, usable]
    predicted_matrix = predicted_matrix[:, usable]

    error = np.abs(predicted_matrix - reference_matrix)
    per_joint: dict[str, dict[str, float]] = {}
    joint_scores: list[float] = []
    for column, name in enumerate(names):
        span = float(np.ptp(reference_matrix[:, column]))
        scale = max(span, JOINT_SCALE_FLOOR)
        mae = float(np.mean(error[:, column]))
        rmse = float(np.sqrt(np.mean(np.square(error[:, column]))))
        per_joint[name] = {
            "mae": round(mae, 6),
            "rmse": round(rmse, 6),
            "nrmse": round(rmse / scale, 6),
            "scale": round(scale, 6),
        }
        joint_scores.append(float(np.clip(1.0 - rmse / scale, 0.0, 1.0)))

    return {
        "steps": steps,
        "predicted_steps": len(predicted),
        "reference_steps": len(reference),
        "coverage": round(steps / max(1, len(predicted)), 4),
        "score": round(100.0 * float(np.mean(joint_scores)), 2),
        "mae": round(float(np.mean(error)), 6),
        "rmse": round(float(np.sqrt(np.mean(np.square(predicted_matrix - reference_matrix)))), 6),
        "endpoint_error": round(float(np.mean(np.abs(predicted_matrix[-1] - reference_matrix[-1]))), 6),
        "dtw": round(
            float(np.mean([dtw_distance(reference_matrix[:, column], predicted_matrix[:, column]) for column in range(len(names))])),
            6,
        ),
        "joints": per_joint,
        "metric": "range-normalized RMSE score (100 = exact match, 0 = error at or above the reference span)",
    }
