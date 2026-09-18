from pathlib import Path

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


def test_status_without_hardware(tmp_path: Path) -> None:
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
