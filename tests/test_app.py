import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from lerobot_monitor.app import create_app
from lerobot_monitor.config import (
    CamerasConfig,
    LibraryConfig,
    MonitorConfig,
    RecordingConfig,
    RobotConfig,
    ServerConfig,
)


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
