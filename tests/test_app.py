from fastapi.testclient import TestClient

from lerobot_monitor.app import create_app
from lerobot_monitor.config import MonitorConfig, RobotConfig, ServerConfig


def test_status_without_hardware() -> None:
    cfg = MonitorConfig(
        server=ServerConfig(port=0),
        robot=RobotConfig(auto_connect=False, port="COM_UNUSED"),
        cameras={},
    )
    app = create_app(cfg)
    with TestClient(app) as client:
        res = client.get("/api/status")
        assert res.status_code == 200
        body = res.json()
        assert "mode" in body
        assert body["robot"]["connected"] is False
        meta = client.get("/api/meta")
        assert meta.status_code == 200
        assert "shoulder_pan" in meta.json()["joints"]
        page = client.get("/")
        assert page.status_code == 200
        assert b"LeRobot Monitor" in page.content
