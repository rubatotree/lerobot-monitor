import asyncio
import base64
import json
import os
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from lerobot_monitor import app as app_module
from lerobot_monitor import dataset_hub, model_hub
from lerobot_monitor.app import create_app
from lerobot_monitor.cameras import DeviceCamera, RemoteMjpegCamera
from lerobot_monitor.config import (
    CamerasConfig,
    LibraryConfig,
    MonitorConfig,
    RecordingConfig,
    RobotConfig,
    ServerConfig,
)
from lerobot_monitor.policy import ActionChunk
from lerobot_monitor.store import JsonStore
from lerobot_monitor.types import JOINT_ORDER


def test_virtual_record_api_publishes_to_library_dataset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dataset_hub, "lerobot_home", lambda: tmp_path / "lerobot")
    config = MonitorConfig(
        store_path=tmp_path / "store.json",
        server=ServerConfig(port=0, base_path="/lerobot"),
        robot=RobotConfig(auto_connect=False),
        cameras=CamerasConfig(probe=False),
        recording=RecordingConfig(root=tmp_path / "videos"),
        library=LibraryConfig(videos_root=tmp_path / "videos"),
    )
    app = create_app(config)
    with TestClient(app) as client:
        hub = app.state.hub
        leader = MagicMock()
        leader.connected = True
        leader.get_action_pose.return_value = {joint: 5.0 for joint in JOINT_ORDER}
        leader.snapshot.return_value = {"connected": True}
        hub.leader = leader
        hub.loop.leader = leader
        created = client.post(
            "/lerobot/api/datasets/empty",
            json={"name": "virtual record", "fps": 5, "cameras": []},
        )
        assert created.status_code == 200, created.text
        row = created.json()
        started = client.post(
            "/lerobot/api/record/start",
            json={"dataset_id": row["id"], "task": "pick", "num_episodes": 1, "episode_time_s": 2},
        )
        assert started.status_code == 200, started.text
        deadline = time.monotonic() + 10
        record = None
        while time.monotonic() < deadline:
            record = client.get("/lerobot/api/status").json()["task"]["record"]
            if record and record["phase"] == "resetting":
                break
            time.sleep(0.03)
        assert record and record["phase"] == "resetting"
        control = {"session_id": record["session_id"], "version": record["version"], "operation_id": "start-1"}
        begun = client.post("/lerobot/api/record/next", json=control)
        assert begun.status_code == 200, begun.text
        assert client.post("/lerobot/api/cameras/rescan").status_code == 409
        time.sleep(0.5)
        control.update(version=begun.json()["record"]["version"], operation_id="finish-1")
        finished = client.post("/lerobot/api/record/next", json=control)
        assert finished.status_code == 200, finished.text
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            record = client.get("/lerobot/api/status").json()["task"]["record"]
            if record["phase"] in {"completed", "error"}:
                break
            time.sleep(0.05)
        assert record["phase"] == "completed", record
        assert record["saved"] == 1
        assert record["episode_number"] == 1
        episodes = client.get("/lerobot/api/episodes", params={"kind": "dataset", "id": row["id"]})
        assert episodes.status_code == 200, episodes.text
        assert len(episodes.json()["episodes"]) == 1



def test_status_without_hardware(tmp_path: Path, monkeypatch) -> None:
    # Keep the library scan off the developer's real HF cache.
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_LEROBOT_HOME", raising=False)
    monkeypatch.delenv("LEROBOT_HOME", raising=False)
    cfg = MonitorConfig(
        store_path=tmp_path / "store.json",
        server=ServerConfig(port=0, base_path="/lerobot"),
        robot=RobotConfig(auto_connect=False, port="COM_UNUSED"),
        cameras=CamerasConfig(probe=False),
        recording=RecordingConfig(root=tmp_path / "videos"),
        library=LibraryConfig(videos_root=tmp_path / "videos", models_roots=[tmp_path / "models"]),
    )
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.get("/").status_code in (200, 307)
        res = client.get("/lerobot/api/status")
        assert res.status_code == 200
        body = res.json()
        assert "mode" in body
        assert body["robot"]["connected"] is True
        assert body["robot"]["virtual"] is True
        assert body["leader_joints"] == {}
        meta = client.get("/lerobot/api/meta")
        assert meta.status_code == 200
        assert "shoulder_pan" in meta.json()["joints"]
        cams = client.get("/lerobot/api/cameras")
        assert cams.status_code == 200
        assert cams.json() == []
        page = client.get("/lerobot/")
        assert page.status_code == 200
        html = page.content
        assert b"LeRobot Monitor" in html
        assert b"Cameras" in html
        assert b"Rollout" in html
        assert b"Deploy" not in html
        assert b"rec-reset" in html
        assert b"Extra params" in html
        assert b"preset-btns" in html
        assert b'id="side-tab-record"' in html
        assert b'id="record-panel"' in html
        assert b'id="hw-enc-threads"' in html
        assert b'id="rec-enc-threads"' not in html
        assert b'id="side-tab-rollout"' in html
        assert b'id="rollout-panel"' in html
        assert b'id="task-mode-tabs"' not in html
        assert html.count(b"Command preview") == 4
        assert b"<summary>Info</summary>" not in html
        assert b"arm-port" in html
        assert b'id="arm-preview"' in html
        assert b'id="preview-source"' in html
        assert b'id="split-preview"' in html
        assert b"info-roll" in html
        assert b"btn-hdr-scan" in html
        assert b"btn-hdr-stop" in html
        assert b"btn-hdr-relax" in html
        assert b'id="btn-hdr-resume"' in html
        assert b"btn-hdr-capture" in html
        assert b"btn-hdr-auto" in html
        assert b'class="side-footer"' not in html
        assert b'id="btn-resume"' not in html
        assert b'id="btn-preset-dup"' in html
        assert b"rec-num" in html
        assert b"id=\"library\"" in html
        assert b"From follower" not in html
        assert b"From leader" not in html
        assert b"btn-joint-serial" in html
        created = client.post("/lerobot/api/videos", json={"name": "blocks", "task": "sort"})
        assert created.status_code == 200
        video_id = created.json()["id"]
        listed = client.get("/lerobot/api/videos")
        assert any(row["id"] == video_id for row in listed.json())
        datasets = client.get("/lerobot/api/datasets")
        assert datasets.status_code == 200
        assert isinstance(datasets.json(), list)
        assert b"Videos" in html
        assert b"vid-list" in html
        assert b"id=\"viz\"" in html
        assert b"id=\"replay\"" in html
        assert html.count(b'id="replay-seek"') == 1
        assert b'id="prog-seek"' not in html
        transport_start = html.index(b'class="replay-transport"')
        transport_end = html.index(b"</header>", transport_start)
        replay_transport = html[transport_start:transport_end]
        for control_id in (b"viz-play", b"viz-restart", b"replay-seek", b"viz-t", b"replay-speed-toggle"):
            assert b'id="' + control_id + b'"' in replay_transport
        assert b'id="viz-exit"' in html
        assert b'id="viz-exit"' not in replay_transport
        assert b"replay-foot" not in html
        assert b"viz-bottom-" not in html
        stopped = client.post("/lerobot/api/task/stop")
        assert stopped.status_code == 200
        assert stopped.json().get("ok") is True
        force_stopped = client.post("/lerobot/api/task/force_stop")
        assert force_stopped.status_code == 200
        assert force_stopped.json().get("ok") is True
        queued = client.post(
            "/lerobot/api/rollout/start",
            json={"policy_path": "missing/policy", "duration_s": 1, "record": False},
        )
        assert queued.status_code == 200
        assert queued.json().get("accepted") is True
        models = client.get("/lerobot/api/models")
        assert models.status_code == 200
        assert isinstance(models.json(), list)
        robot_models = client.get("/lerobot/api/robot-models")
        assert robot_models.status_code == 200
        assert robot_models.json()[0]["id"] == "so101"
        assert client.get("/lerobot/api/robot-models/so101/manifest").json()["urdf"] == "so101.urdf"
        assert client.get("/lerobot/api/robot-models/so101/files/so101.urdf").status_code == 200
        assert client.post("/lerobot/api/robot-models/active", json={"id": "so101"}).status_code == 200
        assert client.post("/lerobot/api/virtual-follower/disconnect").status_code == 200
        assert client.post("/lerobot/api/virtual-follower/connect", json={"model_id": "so101"}).status_code == 200
        assert b"btn-teleop-on" not in html
        assert b"btn-rec-on" not in html
        assert b"btn-roll-on" not in html
        assert b"btn-robot-on" not in html
        assert b"btn-leader-on" not in html
        assert "runtime" in meta.json()
        estop = client.post("/lerobot/api/estop")
        assert estop.status_code == 200
        stopped = client.post("/lerobot/api/task/stop")
        assert stopped.status_code == 200
        assert "上：夹爪".encode("utf-8") not in html
        ports = client.get("/lerobot/api/ports")
        assert ports.status_code == 200
        assert isinstance(ports.json(), list)
        missing = client.post("/lerobot/api/cameras/0/focus", json={"autofocus": False, "focus": 40})
        assert missing.status_code == 404
        saved = client.put("/lerobot/api/presets/pose/fold", json={"gripper": 1.0})
        assert saved.status_code == 200
        listed = client.get("/lerobot/api/presets")
        assert listed.json()["pose"]["fold"]["gripper"] == 1.0
        hardware = client.put(
            "/lerobot/api/presets/hardware/bench",
            json={"schema": 1, "devices": {}, "cameras": {}},
        )
        assert hardware.status_code == 200
        renamed = client.post(
            "/lerobot/api/presets/hardware/bench/rename",
            json={"name": "bench 2"},
        )
        assert renamed.status_code == 200
        assert "bench 2" in client.get("/lerobot/api/presets").json()["hardware"]


def test_remote_blender_camera_controls_return_bad_request(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    cfg = MonitorConfig(
        store_path=tmp_path / "store.json",
        server=ServerConfig(port=0, base_path="/lerobot"),
        robot=RobotConfig(auto_connect=False, port="COM_UNUSED"),
        cameras=CamerasConfig(probe=False),
        recording=RecordingConfig(root=tmp_path / "videos"),
        library=LibraryConfig(videos_root=tmp_path / "videos", models_roots=[tmp_path / "models"]),
    )
    app = create_app(cfg)
    with TestClient(app) as client:
        hub = app.state.hub
        camera = RemoteMjpegCamera(
            "blender_sim_follower_front",
            {
                "robot_id": "sim_follower",
                "id": "front",
                "label": "Front",
                "url": "http://127.0.0.1:9300/video",
            },
        )
        hub.cameras.remote_streams[camera.name] = camera

        assert client.get("/lerobot/api/cameras").json()[0]["remote"] is True
        assert client.get(f"/lerobot/api/cameras/{camera.name}/resolutions").status_code == 400
        assert client.post(
            f"/lerobot/api/cameras/{camera.name}/resolution",
            json={"width": 320, "height": 240},
        ).status_code == 400
        assert client.post(
            f"/lerobot/api/cameras/{camera.name}/focus",
            json={"focus": 10},
        ).status_code == 400
        assert client.post(
            f"/lerobot/api/cameras/{camera.name}/stream",
            json={"enable": True, "port": 5000},
        ).status_code == 400

        local_camera = DeviceCamera(2, width=640, height=480, jpeg_quality=80, port=5002)
        hub.cameras.streams[local_camera.name] = local_camera
        monkeypatch.setattr(app_module, "supported_resolutions", lambda index: [(640, 480), (1920, 1080)])
        assert client.get("/lerobot/api/cameras/2/resolutions").json() == [
            {"width": 640, "height": 480},
            {"width": 1920, "height": 1080},
        ]


def test_episode_edit_persists(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_LEROBOT_HOME", raising=False)
    monkeypatch.delenv("LEROBOT_HOME", raising=False)
    videos_root = tmp_path / "videos"
    cfg = MonitorConfig(
        store_path=tmp_path / "store.json",
        server=ServerConfig(port=0, base_path="/lerobot"),
        robot=RobotConfig(auto_connect=False, port="COM_UNUSED"),
        cameras=CamerasConfig(probe=False),
        recording=RecordingConfig(root=videos_root),
        library=LibraryConfig(videos_root=videos_root, models_roots=[tmp_path / "models"]),
    )
    app = create_app(cfg)
    with TestClient(app) as client:
        video_id = client.post("/lerobot/api/videos", json={"name": "blocks", "task": "sort"}).json()["id"]
        episode = videos_root / video_id / "episodes" / "000000"
        episode.mkdir(parents=True)
        (episode / "joints.csv").write_text("t,obs.gripper,act.gripper\n0,1,2\n", encoding="utf-8")

        listed = client.get("/lerobot/api/episodes", params={"kind": "video", "id": video_id})
        assert listed.status_code == 200
        assert listed.json()["episodes"][0]["name"] == ""
        assert listed.json()["episodes"][0]["task"] == "sort"
        assert listed.json()["source"] == {
            "title": "blocks",
            "subtitle": "sort",
        }
        assert listed.json()["title"] == listed.json()["source"]["title"]

        saved = client.put(
            "/lerobot/api/episodes",
            json={"kind": "video", "id": video_id, "episode": 0, "name": "grasp", "note": "keep"},
        )
        assert saved.status_code == 200
        assert saved.json()["name"] == "grasp"

        detail = client.get(f"/lerobot/api/videos/{video_id}")
        assert detail.json()["episodes"][0]["name"] == "grasp"
        preview = client.get("/lerobot/api/preview", params={"kind": "video", "id": video_id, "episode": 0})
        assert preview.json()["episode_name"] == "grasp"

        missing = client.put(
            "/lerobot/api/episodes",
            json={"kind": "video", "id": video_id, "episode": 7, "name": "nope"},
        )
        assert missing.status_code == 404

        for index in (1, 2):
            extra = videos_root / video_id / "episodes" / f"{index:06d}"
            extra.mkdir(parents=True)
            (extra / "joints.csv").write_text("t,obs.gripper,act.gripper\n0,1,2\n", encoding="utf-8")
            client.put(
                "/lerobot/api/episodes",
                json={"kind": "video", "id": video_id, "episode": index, "name": f"episode-{index}"},
            )

        app.state.hub.loop.writer = SimpleNamespace(
            session_id=video_id,
            closed=False,
            root=videos_root / video_id,
        )
        app.state.hub.loop.recording_mutation_lock.acquire()
        try:
            assert client.delete(f"/lerobot/api/videos/{video_id}").status_code == 409
            assert client.delete(f"/lerobot/api/videos/{video_id}/episodes/0").status_code == 409
            assert client.post(
                f"/lerobot/api/videos/{video_id}/episodes/reorder",
                json={"order": [2, 0, 1]},
            ).status_code == 409
            assert client.put(
                "/lerobot/api/episodes",
                json={"kind": "video", "id": video_id, "episode": 0, "name": "blocked"},
            ).status_code == 409
        finally:
            app.state.hub.loop.writer = None
            app.state.hub.loop.recording_mutation_lock.release()

        reordered = client.post(
            f"/lerobot/api/videos/{video_id}/episodes/reorder",
            json={"order": [2, 0, 1]},
        )
        assert reordered.status_code == 200
        assert reordered.json()["episode_index_map"] == {"2": 0, "0": 1, "1": 2}
        episodes = client.get("/lerobot/api/episodes", params={"kind": "video", "id": video_id}).json()["episodes"]
        assert [row["name"] for row in episodes] == ["episode-2", "grasp", "episode-1"]

        deleted = client.delete(f"/lerobot/api/videos/{video_id}/episodes/1")
        assert deleted.json()["episode_index_map"] == {"0": 0, "2": 1}
        episodes = client.get("/lerobot/api/episodes", params={"kind": "video", "id": video_id}).json()["episodes"]
        assert [row["name"] for row in episodes] == ["episode-2", "episode-1"]

        saved_ui = client.put("/lerobot/api/ui", json={"selected_video": video_id})
        assert saved_ui.json()["selected_video"] == video_id

        reopened = TestClient(create_app(cfg))
        with reopened:
            listed = reopened.get("/lerobot/api/episodes", params={"kind": "video", "id": video_id})
            assert [row["name"] for row in listed.json()["episodes"]] == ["episode-2", "episode-1"]
            assert reopened.get("/lerobot/api/ui").json()["selected_video"] == video_id


def test_dataset_transfer_api_reports_progress_on_the_card(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_LEROBOT_HOME", raising=False)
    monkeypatch.delenv("LEROBOT_HOME", raising=False)
    root = tmp_path / "snapshot"
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text('{"fps": 15, "total_episodes": 0}\n', encoding="utf-8")
    written = {"bytes": 0}
    release = threading.Event()

    def downloader(repo_id: str, *, revision: str = "") -> str:
        release.wait(timeout=5)
        return str(root)

    app = create_app(_debug_config(tmp_path))
    with TestClient(app) as client:
        registry = app.state.hub.dataset_registry
        registry.transfers = dataset_hub.DatasetTransferManager(
            poll_seconds=0.05,
            on_finish=registry._finish_transfer,
            progress_probe=lambda repo_id: written["bytes"],
            total_probe=lambda repo_id, revision: 100,
            downloader=downloader,
        )

        started = client.post(
            "/lerobot/api/datasets/download",
            json={"remote": "user/blocks", "revision": ""},
        )
        assert started.status_code == 200
        assert started.json()["repo_id"] == "user/blocks"
        assert started.json()["direction"] == "download"

        # The card exists before the bytes do, so the bar has somewhere to live.
        staged = next(
            row for row in client.get("/lerobot/api/datasets").json() if row["repo_id"] == "user/blocks"
        )
        assert staged["path"] == ""

        written["bytes"] = 40
        deadline = time.time() + 5
        progress: list[dict] = []
        while time.time() < deadline:
            progress = client.get("/lerobot/api/datasets/transfers").json()
            if progress and progress[0]["transferred_bytes"] == 40:
                break
            time.sleep(0.05)
        assert progress and progress[0]["percent"] == 40.0
        assert progress[0]["status"] == "transferring"
        assert app.state.hub.snapshot()["dataset_transfers"][0]["active"] is True
        blocked_delete = client.delete(
            "/lerobot/api/library", params={"kind": "dataset", "id": "user/blocks"}
        )
        assert blocked_delete.status_code == 409
        assert registry.store.dataset("user/blocks") is not None

        release.set()
        deadline = time.time() + 5
        while time.time() < deadline:
            if registry.transfers.get("user/blocks")["status"] == "done":
                break
            time.sleep(0.05)
        done = next(
            row for row in client.get("/lerobot/api/datasets").json() if row["repo_id"] == "user/blocks"
        )
        assert done["path"] == str(root)
        assert done["metadata"]["episodes"] == 0


def test_dataset_upload_api_reports_file_progress(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_LEROBOT_HOME", raising=False)
    monkeypatch.delenv("LEROBOT_HOME", raising=False)
    datasets_root = tmp_path / "datasets"
    dataset = datasets_root / "user" / "series"
    (dataset / "meta").mkdir(parents=True)
    (dataset / "meta" / "info.json").write_text('{"fps": 10, "total_episodes": 1}\n', encoding="utf-8")
    (dataset / "data").mkdir()
    for index in range(3):
        (dataset / "data" / f"file-{index:03d}.parquet").write_bytes(b"y" * 200)

    release = threading.Event()
    finished: list[dict] = []

    def uploader(repo_id, folder, *, revision="", private=False, on_progress=None):
        assert private is True
        if on_progress is not None:
            on_progress(400, 2)
        release.wait(timeout=5)
        return str(folder)

    cfg = MonitorConfig(
        store_path=tmp_path / "store.json",
        server=ServerConfig(port=0, base_path="/lerobot"),
        robot=RobotConfig(auto_connect=False, port="COM_UNUSED"),
        cameras=CamerasConfig(probe=False),
        recording=RecordingConfig(root=tmp_path / "videos"),
        library=LibraryConfig(videos_root=tmp_path / "videos", dataset_roots=[datasets_root]),
    )
    app = create_app(cfg)
    with TestClient(app) as client:
        registry = app.state.hub.dataset_registry
        registry.transfers = dataset_hub.DatasetTransferManager(
            poll_seconds=0.05,
            on_finish=lambda state: (finished.append(state), registry._finish_transfer(state)),
            uploader=uploader,
        )
        listed = client.get("/lerobot/api/datasets").json()
        assert [row["id"] for row in listed] == ["user/series"]
        assert listed[0]["upstream"] is False
        configured = client.put(
            "/lerobot/api/library",
            json={"kind": "dataset", "id": "user/series", "repo_id": "user/series", "private": True},
        )
        assert configured.status_code == 200, configured.text
        assert configured.json()["path"] == str(dataset)

        started = client.post("/lerobot/api/datasets/user%2Fseries/upload")
        assert started.status_code == 200
        assert started.json()["direction"] == "upload"
        assert started.json()["private"] is True

        deadline = time.time() + 5
        progresses: list[dict] = []
        while time.time() < deadline:
            progresses = client.get("/lerobot/api/datasets/transfers").json()
            if progresses and progresses[0]["files_done"] == 2:
                break
            time.sleep(0.05)
        assert progresses and progresses[0]["files_done"] == 2
        assert progresses[0]["files_total"] == 4
        assert 0 < progresses[0]["percent"] < 100

        release.set()
        deadline = time.time() + 5
        while not finished and time.time() < deadline:
            time.sleep(0.05)
        assert finished and finished[0]["status"] == "done"
        assert finished[0]["direction"] == "upload"


def test_open_library_folder_resolves_registered_local_resources(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    app = create_app(_debug_config(tmp_path))
    launched = MagicMock()
    monkeypatch.setattr(app_module.subprocess, "Popen", launched)

    with TestClient(app) as client:
        hub = app.state.hub
        video = hub.videos.create("open folder")
        snapshot = hub.snapshots.create(name="open folder")
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        dataset_dir = tmp_path / "dataset"
        dataset_dir.mkdir()
        monkeypatch.setattr(hub, "models", lambda: [{"id": "local-model", "path": str(model_dir)}])
        monkeypatch.setattr(hub, "resolve_dataset", lambda _id: {"path": str(dataset_dir)})

        for kind, resource_id, expected in (
            ("video", video["id"], Path(video["path"])),
            ("snapshot", snapshot["id"], Path(snapshot["path"])),
            ("model", "local-model", model_dir),
            ("dataset", "local-dataset", dataset_dir),
        ):
            response = client.post("/lerobot/api/library/open-folder", params={"kind": kind, "id": resource_id})
            assert response.status_code == 200, response.text
            assert launched.call_args.args[0][-1] == str(expected.resolve())

        assert client.post("/lerobot/api/library/open-folder", params={"kind": "video", "id": "missing"}).status_code == 404
        assert client.post("/lerobot/api/library/open-folder", params={"kind": "other", "id": "x"}).status_code == 400
    assert launched.call_count == 4


def test_library_delete_removes_hub_cache_folder_and_mismatched_ids(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_LEROBOT_HOME", raising=False)
    monkeypatch.delenv("LEROBOT_HOME", raising=False)
    repo_dir = tmp_path / "hf" / "hub" / "datasets--user--blocks"
    snapshot = repo_dir / "snapshots" / "rev1"
    (snapshot / "meta").mkdir(parents=True)
    (snapshot / "meta" / "info.json").write_text('{"fps": 15, "total_episodes": 2}\n', encoding="utf-8")
    (snapshot / "data").mkdir()
    read_only = snapshot / "data" / "file-000.parquet"
    read_only.write_bytes(b"payload")
    os.chmod(read_only, 0o444)
    (repo_dir / "blobs").mkdir(parents=True)
    (repo_dir / "blobs" / "abc").write_bytes(b"blob")
    store = JsonStore(tmp_path / "store.json")
    # Legacy entry: local id, upstream repo_id (the case the UI deletes by repo_id).
    store.put_dataset(
        {
            "id": "blocks_plus80",
            "repo_id": "user/blocks",
            "remote": "user/blocks",
            "source": "hub",
            "path": str(snapshot),
        }
    )
    app = create_app(_debug_config(tmp_path))

    with TestClient(app) as client:
        listed = client.get("/lerobot/api/datasets").json()
        assert [row["id"] for row in listed] == ["user/blocks"]

        deleted = client.delete("/lerobot/api/library", params={"kind": "dataset", "id": "user/blocks"})
        assert deleted.status_code == 200, deleted.text
        assert client.get("/lerobot/api/datasets").json() == []
        assert not repo_dir.exists()
        assert app.state.hub.store.dataset("blocks_plus80") is None


def test_library_delete_model_uses_registered_id_and_removes_whole_hub_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_LEROBOT_HOME", raising=False)
    monkeypatch.delenv("LEROBOT_HOME", raising=False)
    repo_dir = tmp_path / "hf" / "hub" / "models--rubatotree--classify-blocks-2-smolvla"
    snapshot = repo_dir / "snapshots" / "rev1"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text('{"type": "smolvla"}', encoding="utf-8")
    (snapshot / "model.safetensors").write_bytes(b"weights")
    (repo_dir / "blobs").mkdir()
    (repo_dir / "blobs" / "abc").write_bytes(b"weights")
    model_id = "rubatotree-classify-blocks-2-smolvla"
    store = JsonStore(tmp_path / "store.json")
    store.put_model({
        "id": model_id,
        "repo_id": "rubatotree/classify-blocks-2-smolvla",
        "source": "huggingface",
        "path": str(snapshot),
        "name": "rubatotree/classify-blocks-2-smolvla",
    })
    store.save_library_override("model", model_id, {"description": "test note"})
    app = create_app(_debug_config(tmp_path))

    with TestClient(app) as client:
        listed = client.get("/lerobot/api/models").json()
        assert len(listed) == 1
        assert listed[0]["id"] == model_id

        deleted = client.delete("/lerobot/api/library", params={"kind": "model", "id": model_id})
        assert deleted.status_code == 200, deleted.text
        assert client.get("/lerobot/api/models").json() == []

    assert not repo_dir.exists()
    assert app.state.hub.store.model(model_id) is None
    assert app.state.hub.store.library_override("model", model_id) == {}


def test_library_delete_model_keeps_registration_if_cache_removal_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    snapshot = tmp_path / "hf" / "hub" / "models--user--policy" / "snapshots" / "rev1"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text('{"type": "act"}', encoding="utf-8")
    (snapshot / "model.safetensors").write_bytes(b"weights")
    store = JsonStore(tmp_path / "store.json")
    store.put_model({"id": "registered-policy", "repo_id": "user/policy", "path": str(snapshot)})
    app = create_app(_debug_config(tmp_path))

    def deny_remove(path: str | Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("cache is in use")

    monkeypatch.setattr(app_module.shutil, "rmtree", deny_remove)
    with TestClient(app) as client:
        deleted = client.delete("/lerobot/api/library", params={"kind": "model", "id": "registered-policy"})
        assert deleted.status_code == 400
        assert client.get("/lerobot/api/models").json()[0]["id"] == "registered-policy"

    assert app.state.hub.store.model("registered-policy") is not None


@pytest.mark.parametrize("winerror", [5, 32])
def test_library_delete_retries_released_local_dataset_and_clears_scan(
    tmp_path: Path, monkeypatch, winerror: int
) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HF_LEROBOT_HOME", raising=False)
    monkeypatch.delenv("LEROBOT_HOME", raising=False)
    dataset = tmp_path / "datasets" / "user" / "blocks_plus80"
    (dataset / "meta").mkdir(parents=True)
    (dataset / "meta" / "info.json").write_text('{"fps": 15, "total_episodes": 1}\n', encoding="utf-8")
    (dataset / "video.mp4").write_bytes(b"video")
    config = MonitorConfig(
        store_path=tmp_path / "store.json",
        server=ServerConfig(port=0, base_path="/lerobot"),
        robot=RobotConfig(auto_connect=False, port="COM_UNUSED"),
        cameras=CamerasConfig(probe=False),
        recording=RecordingConfig(root=tmp_path / "videos"),
        library=LibraryConfig(videos_root=tmp_path / "videos", dataset_roots=[tmp_path / "datasets"]),
    )
    real_rmtree = app_module.shutil.rmtree
    attempts = 0

    def briefly_locked(path: str | Path, *args: object, **kwargs: object) -> None:
        nonlocal attempts
        if Path(path) == dataset:
            attempts += 1
            if attempts == 1:
                error = PermissionError("dataset video is still open")
                error.winerror = winerror
                raise error
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(app_module.shutil, "rmtree", briefly_locked)
    with TestClient(create_app(config)) as client:
        assert [row["id"] for row in client.get("/lerobot/api/datasets").json()] == ["user/blocks_plus80"]
        deleted = client.delete(
            "/lerobot/api/library", params={"kind": "dataset", "id": "user/blocks_plus80"}
        )
        assert deleted.status_code == 200, deleted.text
        assert attempts == 2
        assert not dataset.exists()
        assert client.get("/lerobot/api/datasets").json() == []


def test_library_edit_dataset_saves_without_syncing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_LEROBOT_HOME", raising=False)
    monkeypatch.delenv("LEROBOT_HOME", raising=False)
    datasets_root = tmp_path / "datasets"
    dataset = datasets_root / "user" / "series"
    (dataset / "meta").mkdir(parents=True)
    (dataset / "meta" / "info.json").write_text('{"fps": 10, "total_episodes": 1}\n', encoding="utf-8")

    def explode(*args, **kwargs):
        raise AssertionError("editing dataset details must not sync")

    monkeypatch.setattr(dataset_hub, "download_hf_dataset", explode)
    cfg = MonitorConfig(
        store_path=tmp_path / "store.json",
        server=ServerConfig(port=0, base_path="/lerobot"),
        robot=RobotConfig(auto_connect=False, port="COM_UNUSED"),
        cameras=CamerasConfig(probe=False),
        recording=RecordingConfig(root=tmp_path / "videos"),
        library=LibraryConfig(videos_root=tmp_path / "videos", dataset_roots=[datasets_root]),
    )
    app = create_app(cfg)

    with TestClient(app) as client:
        saved = client.put(
            "/lerobot/api/library",
            json={
                "kind": "dataset",
                "id": "user/series",
                "name": "Renamed Series",
                "description": "Monitor description",
                "repo_id": "user/series",
                "path": str(dataset),
                "private": True,
                "revision": "main",
            },
        )
        assert saved.status_code == 200
        row = saved.json()
        assert row["display_name"] == "Renamed Series"
        assert row["description"] == "Monitor description"
        assert row["revision"] == "main"
        assert row["path"] == str(dataset)
        assert row["repo_id"] == "user/series"
        assert row["private"] is True
        assert row["metadata"]["visibility"] == "private"
        assert row["path"] == str(dataset)

        moved = client.put(
            "/lerobot/api/library",
            json={
                "kind": "dataset", "id": "user/series", "remote": "user/renamed",
                "description": "Monitor description",
            },
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["id"] == "user/renamed"
        assert moved.json()["description"] == "Monitor description"
        assert moved.json()["path"] == str(dataset)
        assert [entry["id"] for entry in client.get("/lerobot/api/datasets").json()] == ["user/renamed"]


def test_library_notes_and_descriptions_merge_into_lists(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_LEROBOT_HOME", raising=False)
    monkeypatch.delenv("LEROBOT_HOME", raising=False)
    videos_root = tmp_path / "videos"
    datasets_root = tmp_path / "datasets"
    dataset = datasets_root / "user" / "series"
    (dataset / "meta").mkdir(parents=True)
    (dataset / "meta" / "info.json").write_text(
        '{"fps": 10, "total_episodes": 1, "title": "Series Demo", "description": "From dataset"}\n',
        encoding="utf-8",
    )
    cfg = MonitorConfig(
        store_path=tmp_path / "store.json",
        server=ServerConfig(port=0, base_path="/lerobot"),
        robot=RobotConfig(auto_connect=False, port="COM_UNUSED"),
        cameras=CamerasConfig(probe=False),
        recording=RecordingConfig(root=videos_root),
        library=LibraryConfig(videos_root=videos_root, dataset_roots=[datasets_root]),
    )
    app = create_app(cfg)
    with TestClient(app) as client:
        video_id = client.post("/lerobot/api/videos", json={"name": "blocks", "task": "sort"}).json()["id"]
        saved = client.put(
            "/lerobot/api/library",
            json={"kind": "video", "id": video_id, "description": "local run"},
        )
        assert saved.status_code == 200
        assert saved.json()["description"] == "local run"
        assert saved.json()["metadata"]["path"].endswith(video_id)

        listed_video = next(row for row in client.get("/lerobot/api/videos").json() if row["id"] == video_id)
        assert listed_video["description"] == "local run"
        assert client.get(f"/lerobot/api/videos/{video_id}").json()["description"] == "local run"

        reordered = client.put(
            "/lerobot/api/library",
            json={"kind": "video", "id": video_id, "description": "second\nfirst"},
        )
        assert reordered.status_code == 200
        assert reordered.json()["description"] == "second\nfirst"

        saved_dataset = client.put(
            "/lerobot/api/library",
            json={
                "kind": "dataset",
                "id": "user/series",
                "name": "Renamed Series",
                "description": "Monitor description",
            },
        )
        assert saved_dataset.status_code == 200
        listed_dataset = next(
            row for row in client.get("/lerobot/api/datasets").json() if row["id"] == "user/series"
        )
        assert listed_dataset["repo_id"] == ""
        assert listed_dataset["upstream"] is False
        assert listed_dataset["display_name"] == "Renamed Series"
        assert listed_dataset["description"] == "Monitor description"
        assert listed_dataset["metadata"]["episodes"] == 1
        assert listed_dataset["metadata"]["fps"] == 10

        episodes = client.get("/lerobot/api/episodes", params={"kind": "dataset", "id": "user/series"})
        assert episodes.status_code == 200
        assert "description" not in episodes.json()["source"]

        assert client.put(
            "/lerobot/api/library",
            json={"kind": "unknown", "id": "x", "note": "nope"},
        ).status_code == 400

        assert client.delete(
            "/lerobot/api/library",
            params={"kind": "video", "id": video_id},
        ).status_code == 200
        assert app.state.hub.store.library_override("video", video_id) == {}


def test_empty_dataset_api_creates_readable_lerobot_skeleton(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setenv("HF_LEROBOT_HOME", str(tmp_path / "lerobot"))
    monkeypatch.delenv("LEROBOT_HOME", raising=False)
    app = create_app(_debug_config(tmp_path))

    with TestClient(app) as client:
        created = client.post(
            "/lerobot/api/datasets/empty",
            json={
                "name": "Empty demo",
                "repo_id": "user/empty_demo",
                "private": True,
                "fps": 20,
                "robot_type": "so101_follower",
                "cameras": [{"key": "front", "width": 320, "height": 240}],
            },
        )
        assert created.status_code == 200
        row = created.json()
        assert row["repo_id"] == "user/empty_demo"
        assert row["private"] is True
        assert row["metadata"]["episodes"] == 0
        assert row["metadata"]["fps"] == 20

        info = json.loads(
            (Path(row["path"]) / "meta" / "info.json").read_text(encoding="utf-8")
        )
        assert info["total_episodes"] == 0
        assert info["features"]["observation.images.front"]["shape"] == [240, 320, 3]

        listed = client.get("/lerobot/api/datasets")
        assert any(item["repo_id"] == "user/empty_demo" for item in listed.json())

        local_only = client.post(
            "/lerobot/api/datasets/empty", json={"name": "Local only", "fps": 20}
        )
        assert local_only.status_code == 200, local_only.text
        assert local_only.json()["repo_id"] == ""
        assert local_only.json()["upstream"] is False
        assert client.post(f"/lerobot/api/datasets/{local_only.json()['id']}/upload").status_code == 400


def _lifecycle_config(tmp_path: Path) -> MonitorConfig:
    return MonitorConfig(
        store_path=tmp_path / "store.json",
        server=ServerConfig(port=0, base_path="/lerobot"),
        robot=RobotConfig(auto_connect=False, port="COM_UNUSED"),
        cameras=CamerasConfig(probe=False),
        recording=RecordingConfig(root=tmp_path / "videos"),
        library=LibraryConfig(videos_root=tmp_path / "videos", models_roots=[]),
    )


def _route_endpoint(app, suffix: str):
    routes = list(app.routes)
    for route in routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            routes.extend(nested.routes)
        if getattr(route, "path", "").endswith(suffix):
            return route.endpoint
    raise AssertionError(f"route ending with {suffix!r} not found")


def test_lifespan_stops_hub_when_request_context_raises(tmp_path: Path) -> None:
    app = create_app(_lifecycle_config(tmp_path))
    hub = app.state.hub
    hub.start = MagicMock()
    hub.stop = MagicMock()

    with pytest.raises(RuntimeError, match="request failed"):
        with TestClient(app):
            raise RuntimeError("request failed")

    hub.start.assert_called_once_with()
    hub.stop.assert_called_once_with()


def test_lifespan_stops_hub_when_startup_raises(tmp_path: Path) -> None:
    app = create_app(_lifecycle_config(tmp_path))
    hub = app.state.hub
    hub.start = MagicMock(side_effect=RuntimeError("startup failed"))
    hub.stop = MagicMock()

    with pytest.raises(RuntimeError, match="startup failed"):
        with TestClient(app):
            pass

    hub.stop.assert_called_once_with()


def test_websocket_preserves_non_disconnect_exceptions(tmp_path: Path) -> None:
    app = create_app(_lifecycle_config(tmp_path))
    endpoint = _route_endpoint(app, "/ws")

    class FailingSocket:
        async def accept(self) -> None:
            return None

        async def send_json(self, _payload) -> None:
            raise RuntimeError("unexpected websocket failure")

    with pytest.raises(RuntimeError, match="unexpected websocket failure"):
        asyncio.run(endpoint(FailingSocket()))


def test_websocket_does_not_swallow_cancellation(tmp_path: Path) -> None:
    app = create_app(_lifecycle_config(tmp_path))
    endpoint = _route_endpoint(app, "/ws")

    class CancelledSocket:
        async def accept(self) -> None:
            return None

        async def send_json(self, _payload) -> None:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(endpoint(CancelledSocket()))


def test_mjpeg_skips_empty_frames_and_preserves_cancellation(tmp_path: Path, monkeypatch) -> None:
    app = create_app(_lifecycle_config(tmp_path))
    hub = app.state.hub
    endpoint = _route_endpoint(app, "/camera/{name}")
    camera = MagicMock()
    camera.latest_jpeg.side_effect = [b"", b"jpeg"]
    monkeypatch.setattr(hub.cameras, "get", lambda _name: camera)

    async def first_frame() -> bytes:
        response = await endpoint("0")
        chunk = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return chunk

    chunk = asyncio.run(first_frame())
    assert chunk == b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n\r\njpeg\r\n"

    camera.latest_jpeg.side_effect = None
    camera.latest_jpeg.return_value = b""

    async def cancel_waiting_stream() -> None:
        response = await endpoint("0")
        pending = asyncio.create_task(anext(response.body_iterator))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await response.body_iterator.aclose()

    asyncio.run(cancel_waiting_stream())


def test_series_only_dataset_is_previewable_and_json_safe(tmp_path: Path, monkeypatch) -> None:
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    dataset = tmp_path / "datasets" / "user" / "series"
    (dataset / "meta").mkdir(parents=True)
    (dataset / "meta" / "info.json").write_text(
        '{"fps": 10, "total_episodes": 1, "title": "Series Demo", "description": "States only"}\n',
        encoding="utf-8",
    )
    (dataset / "meta" / "tasks.jsonl").write_text('{"task": "Reach target"}\n', encoding="utf-8")
    data = dataset / "data" / "chunk-000" / "episode_000000.parquet"
    data.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "timestamp": [0.0, 0.1],
            "episode_index": [0, 0],
            "observation.state.gripper": [float("nan"), float("inf")],
        }
    ).to_parquet(data, index=False)
    cfg = MonitorConfig(
        store_path=tmp_path / "store.json",
        server=ServerConfig(port=0, base_path="/lerobot"),
        robot=RobotConfig(auto_connect=False, port="COM_UNUSED"),
        cameras=CamerasConfig(probe=False),
        recording=RecordingConfig(root=tmp_path / "videos"),
        library=LibraryConfig(videos_root=tmp_path / "videos", dataset_roots=[tmp_path / "datasets"]),
    )

    with TestClient(create_app(cfg)) as client:
        episodes = client.get("/lerobot/api/episodes", params={"kind": "dataset", "id": "user/series"})
        assert episodes.status_code == 200
        assert episodes.json()["episodes"][0]["playable"] is True
        assert episodes.json()["episodes"][0]["has_video"] is False
        assert episodes.json()["source"] == {
            "title": "Series Demo",
            "subtitle": "Reach target",
        }
        reordered = client.post(
            "/lerobot/api/episodes/reorder",
            json={"kind": "dataset", "id": "user/series", "order": [0]},
        )
        assert reordered.status_code == 200
        deleted = client.delete(
            "/lerobot/api/episodes",
            params={"kind": "dataset", "id": "user/series", "episode": 0},
        )
        assert deleted.status_code == 200
        assert client.get(
            "/lerobot/api/episodes",
            params={"kind": "dataset", "id": "user/series"},
        ).json()["episodes"] == []

        preview = client.get(
            "/lerobot/api/preview",
            params={"kind": "dataset", "id": "user/series", "episode": 0},
        )
        assert preview.status_code == 200
        assert preview.json()["series"]["obs.gripper"] == [None, None]


def test_capture_request_forwards_recording_schema(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    cfg = MonitorConfig(
        store_path=tmp_path / "store.json",
        server=ServerConfig(port=0, base_path="/lerobot"),
        robot=RobotConfig(auto_connect=False),
        cameras=CamerasConfig(probe=False),
        recording=RecordingConfig(root=tmp_path / "videos"),
        library=LibraryConfig(videos_root=tmp_path / "videos"),
    )
    app = create_app(cfg)
    submitted: dict[str, object] = {}

    def submit(kind: str, payload: dict[str, object], _timeout: float) -> dict[str, object]:
        submitted.update({"kind": kind, "payload": payload})
        return {"ok": True}

    with TestClient(app) as client:
        app.state.hub.loop.submit = submit
        response = client.post(
            "/lerobot/api/capture/start",
            json={
                "repo_id": "org/demo",
                "video": False,
                "streaming_encoding": True,
                "encoder_threads": 3,
            },
        )

    assert response.status_code == 200
    assert submitted["kind"] == "capture_start"
    payload = submitted["payload"]
    assert isinstance(payload, dict)
    assert payload["repo_id"] == "org/demo"
    assert payload["video"] is False
    assert payload["streaming_encoding"] is True
    assert payload["encoder_threads"] == 3
    assert payload["action_fps"] == 15
    assert payload["video_fps"] == 30


def test_capture_legacy_fps_sets_both_rates_and_rejects_over_capacity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    cfg = MonitorConfig(
        store_path=tmp_path / "store.json",
        server=ServerConfig(port=0, base_path="/lerobot"),
        robot=RobotConfig(auto_connect=False),
        cameras=CamerasConfig(probe=False),
        recording=RecordingConfig(root=tmp_path / "videos"),
        library=LibraryConfig(videos_root=tmp_path / "videos"),
    )
    app = create_app(cfg)
    submitted: dict[str, object] = {}

    def submit(kind: str, payload: dict[str, object], _timeout: float) -> dict[str, object]:
        submitted.update(payload)
        return {"ok": True}

    with TestClient(app) as client:
        app.state.hub.loop.submit = submit
        response = client.post("/lerobot/api/capture/start", json={"fps": 12})
        assert response.status_code == 200
        assert submitted["action_fps"] == 12
        assert submitted["video_fps"] == 12
        invalid = client.post("/lerobot/api/capture/start", json={"video_fps": 31})
        assert invalid.status_code == 400
        assert "camera sampling capacity" in invalid.json()["detail"]

        queued: dict[str, object] = {}

        def submit_nowait(kind: str, payload: dict[str, object]) -> None:
            queued.update({"kind": kind, "payload": payload})

        app.state.hub.loop.submit_nowait = submit_nowait
        rollout = client.post(
            "/lerobot/api/rollout/start",
            json={"policy_path": "missing", "fps": 120, "record": False},
        )
        assert rollout.status_code == 200
        rollout_payload = queued["payload"]
        assert isinstance(rollout_payload, dict)
        assert rollout_payload["policy_fps"] == 120
        assert rollout_payload["action_fps"] is None
        assert rollout_payload["video_fps"] is None


def test_snapshot_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    snapshots_root = tmp_path / "snapshots"
    cfg = MonitorConfig(
        store_path=tmp_path / "store.json",
        server=ServerConfig(port=0, base_path="/lerobot"),
        robot=RobotConfig(auto_connect=False, port="COM_UNUSED"),
        cameras=CamerasConfig(probe=False),
        recording=RecordingConfig(root=tmp_path / "videos"),
        library=LibraryConfig(
            videos_root=tmp_path / "videos",
            snapshots_root=snapshots_root,
            models_roots=[],
        ),
    )
    image = np.zeros((40, 80, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    jpeg_base64 = base64.b64encode(encoded.tobytes()).decode("ascii")

    with TestClient(create_app(cfg)) as client:
        created = client.post(
            "/lerobot/api/snapshots",
            json={
                "name": "pick",
                "task": "pick cube",
                "joints": {"gripper": 1.0},
                "cameras": [{"key": "front camera", "jpeg_base64": jpeg_base64}],
            },
        )
        assert created.status_code == 200
        snapshot_id = created.json()["id"]
        assert created.json()["cameras"][0]["key"] == "front_camera"

        listed = client.get("/lerobot/api/snapshots")
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()] == [snapshot_id]
        detail = client.get(f"/lerobot/api/snapshots/{snapshot_id}")
        assert detail.status_code == 200
        camera = client.get(f"/lerobot/api/snapshots/{snapshot_id}/camera/front_camera")
        assert camera.status_code == 200
        assert camera.headers["content-type"].startswith("image/jpeg")
        preview = client.get(f"/lerobot/api/snapshots/{snapshot_id}/preview")
        assert preview.status_code == 200

        updated = client.put(
            f"/lerobot/api/snapshots/{snapshot_id}",
            json={"note": "edited", "description": "debug input"},
        )
        assert updated.status_code == 200
        assert updated.json()["note"] == "edited"

        duplicated = client.post(f"/lerobot/api/snapshots/{snapshot_id}/duplicate")
        assert duplicated.status_code == 200
        assert duplicated.json()["id"] != snapshot_id

        deleted = client.delete(f"/lerobot/api/snapshots/{snapshot_id}")
        assert deleted.status_code == 200
        assert client.get(f"/lerobot/api/snapshots/{snapshot_id}").status_code == 404


def _debug_config(tmp_path: Path) -> MonitorConfig:
    return MonitorConfig(
        store_path=tmp_path / "store.json",
        server=ServerConfig(port=0, base_path="/lerobot"),
        robot=RobotConfig(auto_connect=False, port="COM_UNUSED"),
        cameras=CamerasConfig(probe=False),
        recording=RecordingConfig(root=tmp_path / "videos"),
        library=LibraryConfig(
            videos_root=tmp_path / "videos",
            snapshots_root=tmp_path / "snapshots",
            models_roots=[],
        ),
    )


def test_joint_api_validates_sync_source_and_speed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    app = create_app(_debug_config(tmp_path))

    with TestClient(app) as client:
        invalid_source = client.post(
            "/lerobot/api/joints",
            json={"joints": {"gripper": 1.0}, "source": "policy"},
        )
        assert invalid_source.status_code == 400
        assert "joint source" in invalid_source.json()["detail"]

        invalid_speed = client.post(
            "/lerobot/api/joints",
            json={"joints": {"gripper": 1.0}, "max_speed": 0},
        )
        assert invalid_speed.status_code == 400
        assert "max_speed" in invalid_speed.json()["detail"]


def test_hardware_system_preset_apply_and_active_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    app = create_app(_debug_config(tmp_path))

    with TestClient(app) as client:
        before = client.get("/lerobot/api/meta").json()
        assert before["active_hardware_preset"] is None

        applied = client.post(
            "/lerobot/api/hardware/apply",
            json={"name": "Disconnected"},
        )
        assert applied.status_code == 200
        body = applied.json()
        assert body["ok"] is True
        assert body["complete"] is True
        assert body["summary"]["failed"] == 0

        after = client.get("/lerobot/api/meta").json()
        assert after["active_hardware_preset"] == "Disconnected"
        assert after["ui"]["active_hardware_preset"] == "Disconnected"

        forced = client.post("/lerobot/api/hardware/force_disconnect")
        assert forced.status_code == 200
        assert forced.json()["ok"] is True


def test_preset_rename_rejects_system_and_duplicate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    app = create_app(_debug_config(tmp_path))

    with TestClient(app) as client:
        unknown = client.post(
            "/lerobot/api/presets/unknown/name/rename",
            json={"name": "new"},
        )
        assert unknown.status_code == 400
        system = client.post(
            "/lerobot/api/presets/hardware/Disconnected/rename",
            json={"name": "empty"},
        )
        assert system.status_code == 400
        assert "system preset" in system.json()["detail"]

        assert client.put(
            "/lerobot/api/presets/hardware/one",
            json={"schema": 1, "devices": {}, "cameras": {}},
        ).status_code == 200
        assert client.put(
            "/lerobot/api/presets/hardware/two",
            json={"schema": 1, "devices": {}, "cameras": {}},
        ).status_code == 200
        duplicate = client.post(
            "/lerobot/api/presets/hardware/one/rename",
            json={"name": "two"},
        )
        assert duplicate.status_code == 400
        assert "already exists" in duplicate.json()["detail"]


def _jpeg_base64(width: int = 24, height: int = 16) -> str:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return base64.b64encode(encoded.tobytes()).decode("ascii")


def test_debug_infer_returns_chunk_and_releases_lease(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    app = create_app(_debug_config(tmp_path))
    hub = app.state.hub
    hub.loop.acquire_debug_lease = MagicMock(return_value={"ok": True, "token": "lease-1"})
    hub.loop.release_debug_lease = MagicMock(return_value={"ok": True, "released": True})
    hub.loop.infer_action_chunk = MagicMock(
        return_value=ActionChunk(
            actions=[{"gripper": 1.0}, {"gripper": 2.0}],
            strategy="policy_chunk",
            degraded=False,
            warnings=[],
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/lerobot/api/debug/infer",
            json={
                "policy_path": "fake/policy",
                "task": "pick cube",
                "device": "cpu",
                "chunk_size": 2,
                "fps": 10.0,
                "camera_map": {"front": "observation.images.front"},
                "source": {"kind": "video", "id": "v1", "episode": 0, "elapsed_s": 1.5},
                "joints": {"gripper": 0.0},
                "cameras": [{"key": "front", "jpeg_base64": _jpeg_base64()}],
                "reference": [{"gripper": 1.5}, {"gripper": 2.0}],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["strategy"] == "policy_chunk"
    assert body["degraded"] is False
    assert body["fps"] == 10.0
    assert [row["t_s"] for row in body["actions"]] == [0.1, 0.2]
    assert body["actions"][1]["joints"] == {"gripper": 2.0}
    assert body["source"]["id"] == "v1"
    assert body["evaluation"]["steps"] == 2
    assert body["evaluation"]["mae"] == 0.25
    assert body["evaluation"]["coverage"] == 1.0
    kwargs = hub.loop.infer_action_chunk.call_args.kwargs
    assert kwargs["path"] == "fake/policy"
    assert kwargs["device"] == "cpu"
    assert kwargs["chunk_size"] == 2
    assert sorted(kwargs["images_rgb"]) == ["front"]
    assert kwargs["images_rgb"]["front"].shape == (16, 24, 3)
    hub.loop.release_debug_lease.assert_called_once_with("lease-1")


def test_debug_infer_reports_busy_lease_as_conflict(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    app = create_app(_debug_config(tmp_path))
    hub = app.state.hub
    hub.loop.acquire_debug_lease = MagicMock(
        return_value={"ok": False, "error": "model debug is already active"}
    )
    hub.loop.release_debug_lease = MagicMock()
    hub.loop.infer_action_chunk = MagicMock()

    with TestClient(app) as client:
        response = client.post(
            "/lerobot/api/debug/infer",
            json={"policy_path": "fake/policy", "joints": {"gripper": 0.0}},
        )

    assert response.status_code == 409
    assert "already active" in response.json()["detail"]
    hub.loop.infer_action_chunk.assert_not_called()
    hub.loop.release_debug_lease.assert_not_called()


def test_debug_infer_reports_policy_failure_and_still_releases(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    app = create_app(_debug_config(tmp_path))
    hub = app.state.hub
    hub.loop.acquire_debug_lease = MagicMock(return_value={"ok": True, "token": "lease-2"})
    hub.loop.release_debug_lease = MagicMock(return_value={"ok": True, "released": True})
    hub.loop.infer_action_chunk = MagicMock(side_effect=RuntimeError("policy not found"))

    with TestClient(app) as client:
        response = client.post(
            "/lerobot/api/debug/infer",
            json={"policy_path": "missing/policy", "joints": {"gripper": 0.0}},
        )

    assert response.status_code == 400
    assert "policy not found" in response.json()["detail"]
    hub.loop.release_debug_lease.assert_called_once_with("lease-2")


def test_debug_infer_rejects_oversized_camera_payload(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    app = create_app(_debug_config(tmp_path))
    hub = app.state.hub
    hub.loop.acquire_debug_lease = MagicMock()
    hub.loop.infer_action_chunk = MagicMock()

    oversized = "A" * (8 * 1024 * 1024 + 1)
    with TestClient(app) as client:
        response = client.post(
            "/lerobot/api/debug/infer",
            json={
                "policy_path": "fake/policy",
                "joints": {"gripper": 0.0},
                "cameras": [{"key": "front", "jpeg_base64": oversized}],
            },
        )

    assert response.status_code == 413
    hub.loop.acquire_debug_lease.assert_not_called()
    hub.loop.infer_action_chunk.assert_not_called()


def test_index_page_exposes_snapshot_and_debug_dom(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    app = create_app(_debug_config(tmp_path))

    with TestClient(app) as client:
        page = client.get("/lerobot/")

    assert page.status_code == 200
    for marker in (
        'id="btn-hdr-snapshot"',
        'id="library-tabs"',
        'data-tab="videos"',
        'data-tab-panel="videos"',
        'id="lib-snapshots"',
        'id="snap-list"',
        'id="lib-search"',
        'id="btn-lib-search-clear"',
        'id="btn-md-add"',
        'id="btn-md-scan"',
        'id="btn-ds-add"',
        'id="btn-ds-scan"',
        'id="btn-ds-download"',
        'id="btn-ds-empty"',
        'id="library-modal"',
        'id="rec-dataset-select"',
        'data-drop-kind="model"',
        'id="viz-exit"',
        'id="side-tabs"',
        'data-tab="joints"',
        'data-tab-panel="joints"',
        'data-panel="debug"',
        'id="btn-dbg-run"',
        'id="btn-dbg-send"',
        'id="dbg-cam-map"',
        'id="dbg-eval"',
        'id="action-legend"',
        'id="preset-toolbar"',
        'id="preset-select"',
        'id="btn-preset-load"',
        'id="btn-preset-save"',
        'id="btn-preset-rename"',
        'id="btn-preset-dup"',
        'id="btn-preset-del"',
        'id="preset-name-popover"',
        'id="btn-arm-toggle"',
        'id="btn-leader-toggle"',
        'id="btn-hdr-arm-power"',
        'id="btn-hdr-leader-power"',
        'class="header-safety"',
        'id="btn-log-clear"',
        'id="btn-log-copy"',
        'aria-label="Arm device"',
        'aria-label="Leader device"',
    ):
        assert marker in page.text
    assert (
        page.text.find('data-tab="models"')
        < page.text.find('data-tab="datasets"')
        < page.text.find('data-tab="videos"')
        < page.text.find('data-tab="snapshots"')
    )
    assert re.search(
        r'id="replay-status".*id="viz-exit".*</header>',
        page.text,
        re.DOTALL,
    )
    assert "<h3>Record</h3>" not in page.text
    assert "<h3>Rollout</h3>" not in page.text
    assert "<label>Teleop</label>" in page.text
    assert page.text.find('id="rec-dataset-select"') < page.text.find('id="rec-task"')


def test_model_registry_api_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))

    def fake_download(repo_id: str, revision: str = "") -> str:
        path = tmp_path / "hf-models" / f"{repo_id.replace('/', '--')}-{revision or 'latest'}"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    monkeypatch.setattr(app_module, "search_hf_models", lambda q, limit=20: [
        {"repo_id": "lerobot/act_aloha", "downloads": 42, "likes": 3, "last_modified": "", "tags": ["lerobot"]}
    ])
    monkeypatch.setattr(model_hub, "download_hf_model", fake_download)
    app = create_app(_debug_config(tmp_path))

    with TestClient(app) as client:
        search = client.get("/lerobot/api/models/search", params={"q": "act"})
        assert search.status_code == 200
        assert search.json()[0]["repo_id"] == "lerobot/act_aloha"

        created = client.post(
            "/lerobot/api/models",
            json={"remote": "lerobot/act_aloha", "name": "ACT Aloha"},
        )
        assert created.status_code == 200
        row = created.json()
        assert row["managed"] is True
        assert row["repo_id"] == "lerobot/act_aloha"
        assert row["path"]

        listed = client.get("/lerobot/api/models")
        assert listed.status_code == 200
        assert [entry["id"] for entry in listed.json()] == [row["id"]]

        saved = client.put(
            "/lerobot/api/library",
            json={
                "kind": "model",
                "id": row["id"],
                "name": "ACT renamed",
                "description": "best checkpoint",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["display_name"] == "ACT renamed"
        assert saved.json()["description"] == "best checkpoint"
        assert saved.json()["metadata"]["repo_id"] == "lerobot/act_aloha"

        technical = client.put(
            f"/lerobot/api/models/{row['id']}",
            json={"revision": "main"},
        )
        assert technical.status_code == 200
        assert technical.json()["revision"] == "main"

        updated = client.post(f"/lerobot/api/models/{row['id']}/update")
        assert updated.status_code == 200
        assert updated.json()["path"].endswith("main")

        removed = client.delete(f"/lerobot/api/models/{row['id']}")
        assert removed.status_code == 200
        assert client.get("/lerobot/api/models").json() == []


def test_model_api_rejects_unknown_or_invalid_entries(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    app = create_app(_debug_config(tmp_path))

    with TestClient(app) as client:
        invalid = client.post("/lerobot/api/models", json={"remote": ""})
        assert invalid.status_code == 400
        missing = client.put("/lerobot/api/models/not-found", json={"name": "x"})
        assert missing.status_code == 404


def test_static_css_owns_hidden_replay_badges(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    app = create_app(_debug_config(tmp_path))

    with TestClient(app) as client:
        css = client.get("/lerobot/static/styles.css")

    assert css.status_code == 200
    assert ".replay-tag.hidden" in css.text
    assert ".panel-tab.active" in css.text
    assert ".side-tab-panel[hidden]" in css.text
    assert ".chart-legend" in css.text
    assert ".chart-legend-toggle" in css.text
    assert "#6ea8ff" in css.text
    assert ".chart-tooltip-actual.muted" in css.text
    assert ".chart-wrap.replay canvas { cursor: crosshair;" in css.text
    assert ".bottom-legend" in css.text


def test_static_library_search_and_live_chart_contract(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    app = create_app(_debug_config(tmp_path))

    with TestClient(app) as client:
        css = client.get("/lerobot/static/styles.css")
        script = client.get("/lerobot/static/app.js")
        page = client.get("/lerobot/")

    assert css.status_code == 200
    assert script.status_code == 200
    assert page.status_code == 200
    assert ".library-search" in css.text
    assert ".library-tools" in css.text
    assert ".library-toolbar" in css.text
    assert ".library-modal" in css.text
    assert ".library-modal-body textarea" in css.text
    assert ".library-meta-menu" in css.text
    assert ".library-select" in css.text
    assert ".library-drop-select" in css.text
    assert ".library-item.draggable-resource" in css.text
    assert ".ep-meta-summary" in css.text
    assert ".lib-meta" in css.text
    assert ".lib-description" in css.text
    assert ".lib-progress" in css.text
    assert ".lib-progress-track" in css.text
    assert ".lib-progress-fill" in css.text
    assert ".lib-progress.indeterminate" in css.text
    assert ".library-menu" in css.text
    assert ".side-tools" in css.text
    assert ".preset-name-popover" in css.text
    assert ".preset-icon-btn.loading" in css.text
    assert ".hdr-device-power" in css.text
    assert ".hdr-icon.stop.stop-requested" in css.text
    assert ".hdr-icon.task-on" in css.text
    assert ".chart-hover-tooltip" in css.text
    assert ".chart-time-control" in css.text
    assert ".joint-controls" in css.text
    assert ".joint-serial-control" in css.text
    assert ".joint-sync-button.on" in css.text
    assert ".joint-auto-lamp.on" in css.text
    assert ".joint-divider" not in css.text
    assert "grid-template-columns: minmax(0, 1fr) 32px 62px;" in css.text
    assert ".video-exit" in css.text
    assert "::-webkit-slider-runnable-track" in css.text
    assert "--joint-speed-progress" in css.text
    list_rule = re.search(r"\.lib-list\s*\{(?P<body>.*?)\}", css.text, re.DOTALL)
    assert list_rule is not None
    assert "max-height" not in list_rule.group("body")
    assert "overflow-y" not in list_rule.group("body")

    assert 'key: "command"' in script.text
    assert 'key: "prediction"' in script.text
    assert "chart-legend-toggle" in script.text
    assert "function applyChartLegendVisibility" in script.text
    assert "setInterval(refreshLibrary" not in script.text
    assert "LIBRARY_META_FIELDS_KEY" in script.text
    assert "function makeLibraryItem" in script.text
    assert "function renderLibraryMetadata" in script.text
    assert "function syncDatasetTransfers" in script.text
    assert "function makeDatasetTransferBar" in script.text
    assert "function datasetTransferLabel" in script.text
    assert 'state.direction === "upload"' in script.text
    assert "dataset_transfers" in script.text
    episode_rule = re.search(r"#ep-list\s*\{(?P<body>.*?)\}", css.text, re.DOTALL)
    assert episode_rule is not None
    assert "overflow-y: auto" in episode_rule.group("body")
    assert "Object.keys(metadata)" in script.text
    assert "function renderMetadataMenu" in script.text
    assert "function duplicateLibraryResource" in script.text
    assert "LIBRARY_FOLDER_ICON" in script.text
    assert "clearedLogSequence" in script.text
    assert "function clearLibrarySelection" in script.text
    assert "function populateRecordDatasetSelect" in script.text
    assert "function syncRecordDatasetSelection" in script.text
    assert "function populateModelSelects" in script.text
    assert "function bindLibraryDropSelect" in script.text
    assert "LIBRARY_DRAG_MIME" in script.text
    assert "function createNewRecordDataset" in script.text
    assert "function beginLibraryRename" not in script.text
    assert "lib-rename" not in script.text
    assert 'id="btn-lib-meta-settings"' in page.text
    assert 'id: "modeMarkers"' in script.text
    assert "MODE_MARKER_LABEL_OFFSET_PX" in script.text
    assert 'currentControlMode() === "rollout"' in script.text
    assert 'id: "hoverTooltip"' in script.text
    assert 'select.id = "chart-time-basis"' in script.text
    assert 'scaleSelect.id = "chart-scale"' in script.text
    assert 'option.textContent = label' in script.text
    assert '["joint", "actual", "pred"]' in script.text
    assert "chart.$modeZeroLabels.push" in script.text
    assert "decimateVisibleSeries" in script.text
    assert "const CHART_UPDATE_INTERVAL_MS = 16;" in script.text
    assert '{ seconds: 2, label: "2s" }' in script.text
    assert '{ seconds: 600, label: "10m" }' in script.text
    assert "function positionLiveNowCursor" in script.text
    assert "function animateLiveCharts" in script.text
    assert "function leftBoundaryPoint" in script.text
    assert "function interpolatedChartPoint" in script.text
    assert "const timeAxisFadePlugin" in script.text
    assert "chart.$lastAnimationNow" in script.text
    assert "function interpolateScaleValueFromTicks" in script.text
    assert "function chartActualCutoff" in script.text
    assert "actualVisible" in script.text
    assert "predictedVisible" in script.text
    assert "const pointerValuePlugin" in script.text
    assert "CHART_TIME_AXIS_FADE_MS" in script.text
    assert "TIME_TICK_STEPS_S" in script.text
    assert "TIME_LABEL_EDGE_FADE_PX" in script.text
    assert "TIME_LABEL_EDGE_GAP_PX" in script.text
    assert "leftEndpointRight" in script.text
    assert "rightEndpointLeft" in script.text
    assert "LIVE_FRAME_RATE" in script.text
    assert "LIVE_RIGHT_PADDING_S" in script.text
    assert "ROLLOUT_FUTURE_RATIO" in script.text
    assert "const pixelAlignedLinePlugin" in script.text
    assert "chart.$drawnTimeTicks" in script.text
    assert "formatChartTimeValue(model.time, chart, true)" in script.text
    assert "pointHoverRadius: 0" in script.text
    assert "chart.$tooltipVisible" in script.text
    assert "function snapChartTimeToFrame" in script.text
    assert "chart.$frameTimes" in script.text
    assert "event.button !== 1" in script.text
    assert "publishedAfterStatus" in script.text
    assert "function stepReplayFrame" in script.text
    assert 'canvas.addEventListener("wheel"' in script.text
    assert "function updateChartHoverFromClient" in script.text
    assert "const threshold = 100;" in script.text
    assert "PRESET_KIND_BY_TAB" in script.text
    assert "PRESET_SELECTION_KEY" in script.text
    assert "PRESET_SCROLL_KEY" in script.text
    assert "function hardwareFields" in script.text
    assert "function selectLibrarySearchKind" in script.text
    assert 'api("/api/hardware/apply"' in script.text
    assert 'api("/api/hardware/force_disconnect"' in script.text
    assert 'api("/api/task/force_stop"' in script.text
    assert 'const endpointRole = role === "arm" ? "robot" : "leader";' in script.text
    assert 'lbl.textContent = label;' in script.text
    assert 'id="st-bus"' not in script.text
    assert "function savedPortValue" in script.text
    assert 'id="joint-sync-source"' in page.text
    assert '<option value="none">None</option>' in page.text
    assert 'id="btn-joint-sync"' in page.text
    assert 'id="btn-joint-auto"' in page.text
    assert 'id="btn-joint-send"' in page.text
    assert 'id="btn-joint-control"' in page.text
    assert 'id="btn-joint-serial"' in page.text
    assert 'id="joint-max-speed"' in page.text
    assert 'id="btn-apply"' not in page.text
    assert 'id="btn-read-pose"' not in page.text
    assert 'id="btn-read-leader"' not in page.text
    assert 'id="chk-hold"' not in page.text
    assert 'id="viz-arm"' not in page.text
    assert 'id="btn-snap-edit"' not in page.text
    assert 'let jointSyncSource = "follower";' in script.text
    assert "function setJointSerial" in script.text
    assert "function relayLeaderPose" in script.text
    assert 'source: "leader"' in script.text
    assert "jointPredictionStale" in script.text
    assert 'if (kind === "pose") return { ...targets };' in script.text
    assert "function sendCurrentJointSourceCommand" in script.text
    assert "function sampleReplayJointPose" in script.text
    assert "function syncReplayJointPanel" in script.text
    assert "function syncJointTargetFromSource" in script.text
    assert "let jointAutoSyncEnabled = false;" in script.text
    assert "function toggleJointAutoSync" in script.text
    assert 'autoButton.disabled = running && mode !== "playback";' in script.text
    assert "const availablePose = jointSourcePose" in script.text
    assert "const sourcePose = jointAutoSyncEnabled ? availablePose : null" in script.text
    assert "function sendJointCommand" in script.text
    assert "sendJointCommand({ ...targets })" in script.text
    assert "if (send) sendJointCommand({ ...targets });" in script.text
    assert "let jointControlEnabled = false;" in script.text
    assert "function toggleJointControl" in script.text
    assert "last.motion_locked" in script.text
    assert 'api("/api/hardware/force_disconnect", { role: "arm" })' in script.text
    assert '["loading", "teleop", "record", "rollout"].includes(backendMode)' in script.text
    assert 'sampleReplayJointPose("obs.", replayElapsed)' in script.text
    assert 'sampleReplayJointPose("act.", replayElapsed)' in script.text
