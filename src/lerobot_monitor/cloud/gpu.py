"""Read NVIDIA inventory without importing CUDA or changing device state."""

from __future__ import annotations

import csv
import subprocess
from typing import Any


def probe_gpus() -> list[dict[str, Any]]:
    """Preserve healthy GPU rows even when nvidia-smi reports a faulty card."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid,name,memory.total,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows: list[dict[str, Any]] = []
    for fields in csv.reader(result.stdout.splitlines()):
        fields = [field.strip() for field in fields]
        if len(fields) != 5 or not fields[1].startswith("GPU-"):
            continue
        try:
            rows.append({"index": int(fields[0]), "uuid": fields[1], "name": fields[2],
                         "memory_total_mb": int(fields[3]), "memory_used_mb": int(fields[4]), "healthy": True})
        except ValueError:
            rows.append({"index": fields[0], "uuid": fields[1], "name": fields[2],
                         "memory_total_mb": 0, "memory_used_mb": 0, "healthy": False})
    try:
        processes = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        # An unavailable process inventory must not make every GPU appear idle.
        known = processes.returncode == 0
        busy = {line.split(",")[0].strip() for line in processes.stdout.splitlines() if "," in line}
        for row in rows:
            card_known = known
            card_busy = row["uuid"] in busy
            if not known and row["healthy"]:
                card = subprocess.run(
                    ["nvidia-smi", "-i", row["uuid"], "--query-compute-apps=gpu_uuid,pid", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                card_known = card.returncode == 0
                card_busy = any("," in line and line.split(",")[0].strip() == row["uuid"] for line in card.stdout.splitlines())
            row["busy"] = card_busy or row["memory_used_mb"] > 512 or not card_known
    except (OSError, subprocess.TimeoutExpired):
        for row in rows:
            row["busy"] = True
    return rows
