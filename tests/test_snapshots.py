import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from lerobot_monitor.snapshots import SnapshotLibrary, SnapshotTooLargeError


def _jpeg(width: int = 640, height: int = 480) -> bytes:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :, 1] = 180
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return encoded.tobytes()


def _manifest(snapshot_id: str, *, created: str = "2026-09-21T00:00:00+00:00") -> dict:
    return {
        "id": snapshot_id,
        "created_utc": created,
        "updated_utc": created,
        "name": snapshot_id,
        "task": "",
        "note": "",
        "description": "",
        "origin": "hardware",
        "source": None,
        "joints": {},
        "cameras": [],
    }


def test_snapshot_create_list_get_update_duplicate_delete(tmp_path: Path) -> None:
    library = SnapshotLibrary(tmp_path / "snapshots")
    created = library.create(
        name="pick cube",
        task="pick the cube",
        note="first try",
        joints={"shoulder_pan": 1.5},
        cameras=[("front camera", _jpeg())],
    )

    assert created["id"].startswith("pick_cube_")
    assert created["name"] == "pick cube"
    assert created["source"] is None
    assert created["cameras"][0]["key"] == "front_camera"
    assert created["cameras"][0]["file"] == "front_camera.jpg"
    assert library.camera_path(created["id"], "front_camera").is_file()
    assert library.preview_path(created["id"]).is_file()
    assert library.list()[0]["id"] == created["id"]

    updated = library.update(
        created["id"],
        {"note": "edited", "description": "snapshot description", "joints": {"gripper": 2}},
    )
    assert updated["note"] == "edited"
    assert updated["description"] == "snapshot description"
    assert updated["joints"] == {"gripper": 2.0}

    duplicated = library.duplicate(created["id"], name="copy")
    assert duplicated["id"] != created["id"]
    assert duplicated["name"] == "copy"
    assert library.camera_path(duplicated["id"], "front_camera").is_file()
    assert library.preview_path(duplicated["id"]).is_file()

    library.delete(created["id"])
    assert [row["id"] for row in library.list()] == [duplicated["id"]]


def test_snapshot_directory_name_is_authoritative_and_corrupt_dirs_skip(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    valid = root / "authoritative"
    valid.mkdir(parents=True)
    (valid / "snapshot.json").write_text(
        json.dumps({**_manifest("stale-id"), "name": "kept"}),
        encoding="utf-8",
    )
    missing = root / "missing-manifest"
    missing.mkdir()
    corrupt = root / "corrupt"
    corrupt.mkdir()
    (corrupt / "snapshot.json").write_text("{not json", encoding="utf-8")
    incomplete = root / "incomplete"
    incomplete.mkdir()
    (incomplete / "snapshot.json").write_text('{"id": "incomplete"}', encoding="utf-8")

    library = SnapshotLibrary(root)
    assert library.get("authoritative")["id"] == "authoritative"
    assert [row["id"] for row in library.list()] == ["authoritative"]


def test_snapshot_rejects_path_traversal_and_invalid_ids(tmp_path: Path) -> None:
    library = SnapshotLibrary(tmp_path / "snapshots")
    for snapshot_id in ("../outside", "..", ".", "bad/id", "bad\\id"):
        with pytest.raises(ValueError):
            library.get(snapshot_id)

    created = library.create(name="safe")
    with pytest.raises(FileNotFoundError):
        library.camera_path(created["id"], "../outside")
    with pytest.raises(FileNotFoundError):
        library.camera_path(created["id"], "missing")


def test_snapshot_camera_key_normalization_and_preview(tmp_path: Path) -> None:
    library = SnapshotLibrary(tmp_path / "snapshots")
    created = library.create(
        name="keys",
        cameras=[
            ("front camera", _jpeg(width=800, height=600)),
            ("front/camera", _jpeg(width=100, height=50)),
        ],
    )
    assert [camera["key"] for camera in created["cameras"]] == ["front_camera", "front_camera-2"]
    preview = library.preview_path(created["id"])
    image = cv2.imread(str(preview))
    assert image is not None
    assert image.shape[1] <= 320
    assert image.shape[0] > 0


def test_snapshot_rejects_oversized_camera_payload(tmp_path: Path) -> None:
    library = SnapshotLibrary(tmp_path / "snapshots")
    with pytest.raises(SnapshotTooLargeError):
        library.create(
            name="too-large",
            cameras=[("front", b"x" * (8 * 1024 * 1024 + 1))],
        )

