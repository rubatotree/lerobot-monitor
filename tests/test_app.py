import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from lerobot_monitor import app as app_module
from lerobot_monitor import model_hub
from lerobot_monitor.app import create_app
from lerobot_monitor.cameras import RemoteMjpegCamera
from lerobot_monitor.config import (
    CamerasConfig,
    LibraryConfig,
    MonitorConfig,
    RecordingConfig,
    RobotConfig,
    ServerConfig,
)
from lerobot_monitor.policy import ActionChunk


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
        assert body["robot"]["connected"] is False
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
        assert b"arm-port" in html
        assert b"info-roll" in html
        assert b"btn-hdr-scan" in html
        assert b"btn-hdr-stop" in html
        assert b"btn-hdr-relax" in html
        assert b"btn-hdr-capture" in html
        assert b"btn-hdr-auto" in html
        assert b"Duplicate" in html
        assert b"rec-num" in html
        assert b"id=\"library\"" in html
        assert b"From follower" in html
        assert b"From leader" in html
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
        transport_end = html.index(b"</div>", transport_start)
        replay_transport = html[transport_start:transport_end]
        for control_id in (b"viz-play", b"viz-restart", b"replay-seek", b"viz-t", b"viz-exit"):
            assert b'id="' + control_id + b'"' in replay_transport
        assert b"replay-foot" not in html
        assert b"viz-bottom-" not in html
        stopped = client.post("/lerobot/api/task/stop")
        assert stopped.status_code == 200
        assert stopped.json().get("ok") is True
        queued = client.post(
            "/lerobot/api/rollout/start",
            json={"policy_path": "missing/policy", "duration_s": 1, "record": False},
        )
        assert queued.status_code == 200
        assert queued.json().get("accepted") is True
        models = client.get("/lerobot/api/models")
        assert models.status_code == 200
        assert isinstance(models.json(), list)
        assert b"btn-teleop-on" not in html
        assert b"btn-rec-on" not in html
        assert b"btn-roll-on" not in html
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
        assert listed.json()["source"] == {
            "title": "blocks",
            "subtitle": "sort",
            "description": "",
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
            json={"kind": "video", "id": video_id, "note": "check lighting", "description": "local run"},
        )
        assert saved.status_code == 200
        assert saved.json()["note"] == "check lighting"

        listed_video = next(row for row in client.get("/lerobot/api/videos").json() if row["id"] == video_id)
        assert listed_video["note"] == "check lighting"
        assert listed_video["description"] == "local run"
        assert client.get(f"/lerobot/api/videos/{video_id}").json()["note"] == "check lighting"

        saved_dataset = client.put(
            "/lerobot/api/library",
            json={
                "kind": "dataset",
                "id": "user/series",
                "note": "baseline",
                "description": "Monitor description",
            },
        )
        assert saved_dataset.status_code == 200
        listed_dataset = next(
            row for row in client.get("/lerobot/api/datasets").json() if row["repo_id"] == "user/series"
        )
        assert listed_dataset["note"] == "baseline"
        assert listed_dataset["description"] == "Monitor description"

        episodes = client.get("/lerobot/api/episodes", params={"kind": "dataset", "id": "user/series"})
        assert episodes.status_code == 200
        assert episodes.json()["source"]["description"] == "Monitor description"
        assert episodes.json()["source"]["note"] == "baseline"

        assert client.put(
            "/lerobot/api/library",
            json={"kind": "unknown", "id": "x", "note": "nope"},
        ).status_code == 400

        assert client.delete(f"/lerobot/api/videos/{video_id}").status_code == 200
        assert app.state.hub.store.library_override("video", video_id) == {}


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
            "description": "States only",
        }

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
        'id="lib-snapshots"',
        'id="snap-list"',
        'id="btn-snap-edit"',
        'id="dbg-snap-editor"',
        'data-panel="debug"',
        'id="btn-dbg-run"',
        'id="btn-dbg-send"',
        'id="dbg-cam-map"',
        'id="dbg-eval"',
        'id="md-file-input"',
    ):
        assert marker in page.text


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
            f"/lerobot/api/models/{row['id']}",
            json={"name": "ACT renamed", "revision": "main"},
        )
        assert saved.status_code == 200
        assert saved.json()["name"] == "ACT renamed"
        assert saved.json()["revision"] == "main"

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
