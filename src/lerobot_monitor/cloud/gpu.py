"""Read NVIDIA inventory without importing CUDA or changing device state."""

from __future__ import annotations

import csv
import subprocess
from typing import Any


def _process_owners(pids: set[int]) -> dict[int, dict[str, str]]:
    """Resolve process ownership from procfs without invoking a shell."""
    try:
        import pwd
    except ImportError:
        return {pid: {"user": "unknown", "program": "unknown"} for pid in pids}
    owners: dict[int, dict[str, str]] = {}
    for pid in pids:
        try:
            status: dict[str, str] = {}
            with open(f"/proc/{pid}/status", encoding="utf-8") as stream:
                for line in stream:
                    if ":" in line:
                        key, value = line.split(":", 1)
                        status[key] = value.strip()
            uid = int(status.get("Uid", "-1").split()[0])
            owners[pid] = {
                "user": pwd.getpwuid(uid).pw_name,
                "program": status.get("Name", "") or "unknown",
            }
        except (OSError, KeyError, ValueError, IndexError):
            owners[pid] = {"user": "unknown", "program": "unknown"}
    return owners


def _parse_processes(output: str) -> dict[str, list[dict[str, Any]]]:
    by_gpu: dict[str, list[dict[str, Any]]] = {}
    raw: list[tuple[str, int, str, int]] = []
    for fields in csv.reader(output.splitlines()):
        fields = [field.strip() for field in fields]
        if len(fields) != 4 or not fields[0].startswith("GPU-"):
            continue
        try:
            raw.append((fields[0], int(fields[1]), fields[2], int(fields[3])))
        except ValueError:
            continue
    owners = _process_owners({pid for _, pid, _, _ in raw})
    for gpu_uuid, pid, nvidia_program, memory_mb in raw:
        owner = owners.get(pid, {})
        program = str(owner.get("program") or nvidia_program or "unknown")
        by_gpu.setdefault(gpu_uuid, []).append(
            {
                "pid": pid,
                "user": str(owner.get("user") or "unknown"),
                "program": program,
                "memory_used_mb": memory_mb,
            }
        )
    for processes in by_gpu.values():
        processes.sort(key=lambda item: int(item["memory_used_mb"]), reverse=True)
    return by_gpu


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
            ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        # An unavailable process inventory must not make every GPU appear idle.
        known = processes.returncode == 0
        process_rows = _parse_processes(processes.stdout) if known else {}
        for row in rows:
            card_known = known
            card_processes = process_rows.get(row["uuid"], [])
            card_busy = bool(card_processes)
            if not known and row["healthy"]:
                card = subprocess.run(
                    ["nvidia-smi", "-i", row["uuid"], "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                card_known = card.returncode == 0
                card_processes = _parse_processes(card.stdout).get(row["uuid"], []) if card_known else []
                card_busy = bool(card_processes)
            row["processes"] = card_processes
            if card_processes:
                row["primary_user"] = card_processes[0]["user"]
                row["primary_program"] = card_processes[0]["program"]
                row["process_memory_used_mb"] = sum(int(item["memory_used_mb"]) for item in card_processes)
            row["busy"] = card_busy or row["memory_used_mb"] > 512 or not card_known
    except (OSError, subprocess.TimeoutExpired):
        for row in rows:
            row["processes"] = []
            row["busy"] = True
    return rows
