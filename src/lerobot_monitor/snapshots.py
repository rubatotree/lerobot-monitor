"""Editable snapshot library backed by one directory per snapshot."""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

MAX_CAMERA_BASE64_BYTES = 8 * 1024 * 1024
MAX_CAMERA_COUNT = 12
MAX_DECODED_CAMERA_BYTES = 48 * 1024 * 1024

_SAFE_ID = re.compile(r"[A-Za-z0-9._-]+")
_SAFE_KEY = re.compile(r"[^A-Za-z0-9._-]+")


class SnapshotTooLargeError(ValueError):
    """Raised when a snapshot camera payload exceeds the API budget."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _slugify(value: str, fallback: str = "snapshot") -> str:
    text = _SAFE_KEY.sub("_", (value or "").strip()).strip("._-")
    return text or fallback


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp_name).replace(path)
    finally:
        if tmp_name is not None:
            Path(tmp_name).unlink(missing_ok=True)


def _normalize_key(value: str) -> str:
    key = _SAFE_KEY.sub("_", str(value or "").strip()).strip("._-")
    return key or "camera"


def _normalize_joints(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    joints: dict[str, float] = {}
    for raw_key, raw_value in value.items():
        try:
            number = float(raw_value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            joints[str(raw_key)] = number
    return joints


def _normalize_source(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    source: dict[str, Any] = {}
    for key in ("kind", "id", "episode", "elapsed_s"):
        if key in value:
            source[key] = value[key]
    return source or None


def _camera_payload(item: Any) -> tuple[str, bytes]:
    """Return a normalized camera key and decoded JPEG bytes.

    The API sends ``{"key": ..., "jpeg_base64": ...}`` while direct callers
    may pass ``(key, bytes)`` or a dictionary with a ``data`` field.
    """
    if isinstance(item, (tuple, list)) and len(item) == 2:
        key, raw = item
    elif isinstance(item, dict):
        key = item.get("key", "camera")
        raw = item.get("data")
        if raw is None:
            raw = item.get("jpeg")
        if raw is None:
            raw = item.get("jpeg_bytes")
        if raw is None and item.get("jpeg_base64") is not None:
            encoded = str(item["jpeg_base64"])
            if encoded.startswith("data:") and "," in encoded:
                encoded = encoded.split(",", 1)[1]
            try:
                encoded_bytes = encoded.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError("camera base64 must be ASCII") from exc
            if len(encoded_bytes) > MAX_CAMERA_BASE64_BYTES:
                raise SnapshotTooLargeError(
                    f"camera base64 exceeds {MAX_CAMERA_BASE64_BYTES} bytes"
                )
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("camera image is not valid base64") from exc
    else:
        raise ValueError("camera payload must contain bytes or jpeg_base64")
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise ValueError("camera image must be bytes")
    data = bytes(raw)
    if len(data) > MAX_CAMERA_BASE64_BYTES:
        raise SnapshotTooLargeError(f"camera image exceeds {MAX_CAMERA_BASE64_BYTES} bytes")
    return _normalize_key(str(key)), data


def _decode_camera_payloads(cameras: Any) -> list[tuple[str, bytes]]:
    if cameras is None:
        return []
    if not isinstance(cameras, (list, tuple)):
        raise ValueError("cameras must be a list")
    if len(cameras) > MAX_CAMERA_COUNT:
        raise SnapshotTooLargeError(f"at most {MAX_CAMERA_COUNT} cameras are allowed")
    decoded: list[tuple[str, bytes]] = []
    total = 0
    for item in cameras:
        key, data = _camera_payload(item)
        total += len(data)
        if total > MAX_DECODED_CAMERA_BYTES:
            raise SnapshotTooLargeError(
                f"decoded camera images exceed {MAX_DECODED_CAMERA_BYTES} bytes"
            )
        decoded.append((key, data))
    return decoded


def _unique_camera_keys(cameras: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    used: set[str] = set()
    result: list[tuple[str, bytes]] = []
    for key, data in cameras:
        base = _normalize_key(key)
        candidate = base
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{base}-{suffix}"
            suffix += 1
        used.add(candidate.casefold())
        result.append((candidate, data))
    return result


class SnapshotLibrary:
    """Persist snapshots as ``<root>/<id>/{snapshot.json,cameras/*}``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    def _snapshot_dir(self, snapshot_id: str) -> Path:
        name = str(snapshot_id or "").strip()
        if not _SAFE_ID.fullmatch(name) or name in {".", ".."}:
            raise ValueError("invalid snapshot id")
        root = self.root.resolve()
        path = (self.root / name).resolve()
        if path == root or root not in path.parents:
            raise ValueError("snapshot path escapes library root")
        return path

    def _load_manifest(self, snapshot_id: str) -> dict[str, Any] | None:
        path = self._snapshot_dir(snapshot_id)
        manifest_path = path / "snapshot.json"
        if not manifest_path.is_file():
            return None
        raw = _read_json(manifest_path)
        if not raw or not isinstance(raw.get("created_utc"), str):
            return None
        cameras_raw = raw.get("cameras")
        if not isinstance(cameras_raw, list):
            return None
        cameras: list[dict[str, Any]] = []
        for item in cameras_raw:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "")
            file_name = str(item.get("file") or "")
            if not key or not file_name or not _SAFE_ID.fullmatch(key):
                continue
            if Path(file_name).name != file_name or not _SAFE_ID.fullmatch(file_name):
                continue
            try:
                width = int(item.get("width") or 0)
                height = int(item.get("height") or 0)
            except (TypeError, ValueError):
                width, height = 0, 0
            cameras.append(
                {
                    "key": key,
                    "file": file_name,
                    "width": max(0, width),
                    "height": max(0, height),
                }
            )
        origin = str(raw.get("origin") or "hardware")
        if origin not in {"hardware", "replay"}:
            origin = "hardware"
        return {
            "id": path.name,
            "created_utc": str(raw.get("created_utc")),
            "updated_utc": str(raw.get("updated_utc") or raw.get("created_utc")),
            "name": str(raw.get("name") or ""),
            "task": str(raw.get("task") or ""),
            "note": str(raw.get("note") or ""),
            "description": str(raw.get("description") or ""),
            "origin": origin,
            "source": _normalize_source(raw.get("source")),
            "joints": _normalize_joints(raw.get("joints")),
            "cameras": cameras,
        }

    def _write_camera_image(
        self,
        image_path: Path,
        data: bytes,
    ) -> tuple[int, int]:
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(data)
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return 0, 0
        return int(image.shape[1]), int(image.shape[0])

    def _write_preview(self, snapshot_dir: Path, first_image: Path | None) -> None:
        preview_path = snapshot_dir / "preview.jpg"
        if first_image is None or not first_image.is_file():
            preview_path.unlink(missing_ok=True)
            return
        image = cv2.imread(str(first_image), cv2.IMREAD_COLOR)
        if image is None:
            preview_path.unlink(missing_ok=True)
            return
        width = int(image.shape[1])
        if width > 320:
            scale = 320.0 / float(width)
            image = cv2.resize(
                image,
                (320, max(1, int(round(image.shape[0] * scale)))),
                interpolation=cv2.INTER_AREA,
            )
        ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            preview_path.unlink(missing_ok=True)
            return
        preview_path.write_bytes(encoded.tobytes())

    def _unique_snapshot_dir(self, name: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        base = _slugify(name)
        base_id = f"{base}_{_utc_stamp()}"
        suffix = 1
        while True:
            snapshot_id = base_id if suffix == 1 else f"{base_id}_{suffix}"
            path = self.root / snapshot_id
            try:
                path.mkdir(exist_ok=False)
                return path
            except FileExistsError:
                suffix += 1

    def list(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for path in self.root.iterdir():
            if not path.is_dir():
                continue
            try:
                row = self._load_manifest(path.name)
            except (OSError, ValueError):
                continue
            if row is not None:
                rows.append(row)
        rows.sort(key=lambda row: (str(row.get("updated_utc") or ""), str(row.get("id") or "")), reverse=True)
        return rows

    def get(self, snapshot_id: str) -> dict[str, Any]:
        row = self._load_manifest(snapshot_id)
        if row is None:
            raise FileNotFoundError(snapshot_id)
        return row

    def create(
        self,
        *,
        name: str = "",
        task: str = "",
        note: str = "",
        description: str = "",
        origin: str = "hardware",
        source: dict[str, Any] | None = None,
        joints: dict[str, Any] | None = None,
        cameras: Any = None,
    ) -> dict[str, Any]:
        if origin not in {"hardware", "replay"}:
            raise ValueError("origin must be 'hardware' or 'replay'")
        decoded = _unique_camera_keys(_decode_camera_payloads(cameras))
        snapshot_dir = self._unique_snapshot_dir(name or task)
        try:
            camera_entries: list[dict[str, Any]] = []
            first_image: Path | None = None
            for key, data in decoded:
                file_name = f"{key}.jpg"
                image_path = snapshot_dir / "cameras" / file_name
                width, height = self._write_camera_image(image_path, data)
                camera_entries.append(
                    {
                        "key": key,
                        "file": file_name,
                        "width": width,
                        "height": height,
                    }
                )
                if first_image is None and width > 0 and height > 0:
                    first_image = image_path
            self._write_preview(snapshot_dir, first_image)
            now = _utc_now()
            manifest = {
                "id": snapshot_dir.name,
                "created_utc": now,
                "updated_utc": now,
                "name": str(name or ""),
                "task": str(task or ""),
                "note": str(note or ""),
                "description": str(description or ""),
                "origin": origin,
                "source": _normalize_source(source) if origin == "replay" else None,
                "joints": _normalize_joints(joints),
                "cameras": camera_entries,
            }
            _write_json(snapshot_dir / "snapshot.json", manifest)
            return self.get(snapshot_dir.name)
        except BaseException:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            raise

    def update(self, snapshot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        path = self._snapshot_dir(snapshot_id)
        with self._lock:
            manifest = self.get(snapshot_id)
            if payload.get("origin") is not None:
                origin = str(payload["origin"])
                if origin not in {"hardware", "replay"}:
                    raise ValueError("origin must be 'hardware' or 'replay'")
                manifest["origin"] = origin
            for key in ("name", "task", "note", "description"):
                if key in payload and payload[key] is not None:
                    manifest[key] = str(payload[key])
            if "source" in payload:
                manifest["source"] = (
                    _normalize_source(payload["source"])
                    if manifest.get("origin") == "replay"
                    else None
                )
            if "joints" in payload:
                manifest["joints"] = _normalize_joints(payload["joints"])
            manifest["updated_utc"] = _utc_now()
            _write_json(path / "snapshot.json", manifest)
            return self.get(snapshot_id)

    def duplicate(self, snapshot_id: str, *, name: str | None = None) -> dict[str, Any]:
        source = self._snapshot_dir(snapshot_id)
        if not (source / "snapshot.json").is_file():
            raise FileNotFoundError(snapshot_id)
        with self._lock:
            manifest = self.get(snapshot_id)
            duplicate_name = str(name if name is not None else f"{manifest.get('name') or snapshot_id} copy")
            destination = self._unique_snapshot_dir(duplicate_name)
            try:
                shutil.copytree(source, destination, dirs_exist_ok=True)
                now = _utc_now()
                manifest["id"] = destination.name
                manifest["name"] = duplicate_name
                manifest["created_utc"] = now
                manifest["updated_utc"] = now
                _write_json(destination / "snapshot.json", manifest)
                return self.get(destination.name)
            except BaseException:
                shutil.rmtree(destination, ignore_errors=True)
                raise

    def delete(self, snapshot_id: str) -> None:
        path = self._snapshot_dir(snapshot_id)
        if not path.is_dir():
            raise FileNotFoundError(snapshot_id)
        with self._lock:
            shutil.rmtree(path)

    def camera_path(self, snapshot_id: str, key: str) -> Path:
        manifest = self.get(snapshot_id)
        wanted = str(key)
        entry = next((item for item in manifest["cameras"] if item["key"] == wanted), None)
        if entry is None:
            raise FileNotFoundError(key)
        snapshot_dir = self._snapshot_dir(snapshot_id)
        cameras_dir = (snapshot_dir / "cameras").resolve()
        path = (cameras_dir / str(entry["file"])).resolve()
        if cameras_dir not in path.parents or not path.is_file():
            raise FileNotFoundError(key)
        return path

    def preview_path(self, snapshot_id: str) -> Path:
        path = self._snapshot_dir(snapshot_id) / "preview.jpg"
        if not path.is_file():
            raise FileNotFoundError("preview")
        return path
