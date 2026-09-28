"""Exercise an explicitly selected cloud model through the independent local manager.

No robot is connected. Observations are zero state and lossless black RGB images.
The selected model is unloaded in finally, including when a check fails.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import struct
import threading
import time
import uuid
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx


def black_png(height: int = 480, width: int = 640) -> str:
    """Return base64 PNG for RGB uint8 [H,W,3] without optional image packages."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    pixels = (b"\x00" + b"\x00" * (width * 3)) * height
    image = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
             + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))
    return base64.b64encode(image).decode("ascii")


class SmokeClient:
    def __init__(self, manager_url: str, host: str, timeout: float) -> None:
        if urlsplit(manager_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Use a loopback local-manager URL")
        self.base = manager_url.rstrip("/") + "/api/hosts/" + quote(host, safe="") + "/cloud/api/v1"
        self.timeout = timeout
        self.client = httpx.Client(timeout=timeout, trust_env=False)

    def request(self, method: str, path: str, body: dict[str, Any] | None = None,
                expected: int | None = None) -> Any:
        response = self.client.request(method, self.base + path, json=body)
        if expected is not None:
            if response.status_code != expected:
                raise AssertionError(f"{method} {path}: expected HTTP {expected}, got {response.status_code}: {response.text[:1000]}")
        elif response.is_error:
            raise RuntimeError(f"{method} {path}: HTTP {response.status_code}: {response.text[:1000]}")
        return response.json()

    def wait_job(self, operation: dict[str, Any], timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            jobs = self.request("GET", "/jobs")
            job = next((row for row in jobs if row["id"] == operation["job_id"]), None)
            if job and job["status"] == "succeeded":
                return job
            if job and job["status"] == "failed":
                raise RuntimeError(f"Cloud {job['kind']} job failed: {job.get('error')}")
            time.sleep(1)
        raise TimeoutError(f"Cloud job {operation['job_id']} did not complete within {timeout:g}s")

    def deployment(self, identifier: str) -> dict[str, Any]:
        return next(row for row in self.request("GET", "/deployments") if row["id"] == identifier)


class KeepAlive:
    def __init__(self, client: SmokeClient, session_id: str, epoch: int) -> None:
        self.client = client
        self.session_id = session_id
        self.epoch = epoch
        self.stop_event = threading.Event()
        self.errors: list[str] = []
        self.thread = threading.Thread(target=self._run, name="smoke-heartbeat", daemon=True)

    def _run(self) -> None:
        while not self.stop_event.wait(5):
            try:
                self.client.request("POST", f"/sessions/{self.session_id}/heartbeat", {"epoch": self.epoch})
            except Exception as exc:
                self.errors.append(str(exc))
                return

    def __enter__(self) -> KeepAlive:
        self.thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop_event.set()
        self.thread.join(timeout=self.client.timeout + 1)
        if not args[0] and self.errors:
            raise AssertionError("Heartbeat failed: " + self.errors[0])


def verify_actions(result: dict[str, Any], action_dim: int, mode: str) -> dict[str, Any]:
    """Validate both raw and absolute action arrays have matching shape [T,A]."""
    lengths = []
    for key in ("actions", "raw_actions"):
        values = result[key]
        if not values or any(len(row) != action_dim or any(not math.isfinite(value) for value in row) for row in values):
            raise AssertionError(f"{key} must have finite shape [T,{action_dim}] with T>0")
        lengths.append(len(values))
    if lengths[0] != lengths[1] or result["shape"] != [lengths[0], action_dim]:
        raise AssertionError("Raw, absolute and advertised action shapes differ")
    if mode == "select_action" and lengths[0] != 1:
        raise AssertionError("select_action must return shape [1,A]")
    return {"shape": result["shape"], "compute_seconds": result.get("compute_seconds"), "finite": True}


def exercise_mode(client: SmokeClient, deployment_id: str, mode: str, task: str) -> dict[str, Any]:
    opened = client.request("POST", f"/deployments/{deployment_id}/sessions", {"mode": mode, "task": task}, expected=201)
    session_id, epoch = opened["session_id"], opened["epoch"]
    report: dict[str, Any] = {"mode": mode, "checks": [], "metadata": opened}
    try:
        client.request("POST", f"/deployments/{deployment_id}/sessions", {"mode": mode}, expected=409)
        client.request("POST", f"/deployments/{deployment_id}/unload", {}, expected=409)
        report["checks"].extend(["exclusive_session", "unload_rejected_while_in_use"])
        client.request("POST", f"/sessions/{session_id}/heartbeat", {"epoch": epoch})
        png = black_png()
        payload = {"epoch": epoch, "request_id": uuid.uuid4().hex,
                   "state": {key: 0.0 for key in opened["state_keys"]},
                   "images": {key: png for key in opened["image_keys"]}, "task": task, "chunk_size": 8}
        with KeepAlive(client, session_id, epoch):
            started = time.monotonic()
            result = client.request("POST", f"/sessions/{session_id}/infer", payload)
            report["initial"] = {**verify_actions(result, opened["action_dim"], mode),
                                 "roundtrip_seconds": time.monotonic() - started}
            client.request("POST", f"/sessions/{session_id}/infer", payload, expected=409)
            report["checks"].append("duplicate_request_rejected")
            if mode == "rtc_chunk":
                prefix = {**payload, "request_id": uuid.uuid4().hex, "prefix_raw": result["raw_actions"][:8],
                          "prefix_absolute": result["actions"][:8], "inference_delay": 1}
                prefixed = client.request("POST", f"/sessions/{session_id}/infer", prefix)
                report["with_prefix"] = verify_actions(prefixed, opened["action_dim"], mode)
        reset = client.request("POST", f"/sessions/{session_id}/reset", {"epoch": epoch})
        if reset["epoch"] != epoch + 1:
            raise AssertionError("Reset must increment epoch")
        client.request("POST", f"/sessions/{session_id}/infer", {**payload, "request_id": uuid.uuid4().hex}, expected=409)
        report["checks"].append("stale_epoch_rejected")
        with KeepAlive(client, session_id, reset["epoch"]):
            after_reset = client.request("POST", f"/sessions/{session_id}/infer", {**payload, "epoch": reset["epoch"]})
            report["after_reset"] = verify_actions(after_reset, opened["action_dim"], mode)
        report["checks"].extend(["heartbeat", "reset_epoch", "request_id_reusable_after_reset"])
        report["status"] = "passed"
        return report
    finally:
        client.request("DELETE", f"/sessions/{session_id}")


def run_smoke(arguments: argparse.Namespace) -> dict[str, Any]:
    client = SmokeClient(arguments.manager_url, arguments.host, arguments.timeout)
    report: dict[str, Any] = {"host": arguments.host, "gpu_uuid": arguments.gpu,
                              "started_at": time.time(), "status": "failed", "modes": []}
    deployment_id: str | None = None
    loaded = False
    try:
        rows = client.request("GET", "/deployments")
        matches = [row for row in rows if row["id"] == arguments.deployment_id] if arguments.deployment_id else [
            row for row in rows if row["name"] == arguments.model or row["source"] == arguments.model]
        if len(matches) != 1:
            raise ValueError("Select exactly one existing deployment by --deployment-id or unambiguous --model")
        deployment_id = matches[0]["id"]
        report["deployment_id"] = deployment_id
        report["deployment_name"] = matches[0]["name"]
        if matches[0]["status"] == "loaded" and matches[0].get("gpu_uuid") != arguments.gpu:
            raise ValueError("Selected model is already loaded on a different GPU; choose its GPU explicitly")
        if matches[0]["status"] != "loaded":
            operation = client.request("POST", f"/deployments/{deployment_id}/load", {"gpu_uuid": arguments.gpu})
            loaded = True
            report["load_job"] = client.wait_job(operation, arguments.load_timeout)
        loaded = True
        metadata = client.deployment(deployment_id)["metadata"]
        report["metadata"] = metadata
        for mode in ("select_action", "debug_chunk", "rtc_chunk"):
            if not metadata["capabilities"].get(mode):
                report["modes"].append({"mode": mode, "status": "unsupported"})
                continue
            report["modes"].append(exercise_mode(client, deployment_id, mode, arguments.task))
        if not any(mode["status"] == "passed" for mode in report["modes"]):
            raise AssertionError("Model advertised no usable inference modes")
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if deployment_id and loaded:
            try:
                operation = client.request("POST", f"/deployments/{deployment_id}/unload", {})
                report["unload_job"] = client.wait_job(operation, 120)
            except Exception as exc:
                report["cleanup_error"] = str(exc)
                report["status"] = "failed"
        client.client.close()
        report["finished_at"] = time.time()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager-url", default="http://127.0.0.1:8095")
    parser.add_argument("--host", default="8x4090-server")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--deployment-id")
    selector.add_argument("--model", help="Exact deployment name or source")
    parser.add_argument("--gpu", required=True, help="Explicit healthy, unused GPU UUID")
    parser.add_argument("--task", default="Move the arm to the target.")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--load-timeout", type=float, default=600)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = run_smoke(arguments)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(arguments.output.resolve()), "error": report.get("error")}))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
