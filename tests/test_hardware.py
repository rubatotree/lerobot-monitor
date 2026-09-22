from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from lerobot_monitor import hardware
from lerobot_monitor.hardware import (
    apply_hardware_preset,
    match_camera,
    match_serial_device,
)


def test_serial_match_prefers_hwid_and_rejects_reused_port() -> None:
    saved = {
        "kind": "serial",
        "hwid": "USB VID:PID=1A86:7523 SER=ARM-A",
        "port": "COM6",
    }
    moved = [
        {
            "port": "COM9",
            "hwid": "USB VID:PID=1A86:7523 SER=ARM-A",
        }
    ]
    assert match_serial_device(saved, moved) == moved[0]

    wrong_device_on_old_port = [
        {
            "port": "COM6",
            "hwid": "USB VID:PID=1A86:7523 SER=OTHER",
        }
    ]
    assert match_serial_device(saved, wrong_device_on_old_port) is None


def test_virtual_serial_and_remote_camera_match_by_identity() -> None:
    serial = {
        "kind": "virtual",
        "role": "leader",
        "robot_id": "sim-leader",
        "port": "socket://127.0.0.1:9201",
    }
    ports = [
        {
            "virtual": True,
            "role": "leader",
            "robot_id": "sim-leader",
            "port": "socket://127.0.0.1:9999",
        }
    ]
    assert match_serial_device(serial, ports) == ports[0]

    camera = {
        "kind": "remote",
        "robot_id": "sim-leader",
        "camera_id": "front",
        "object_name": "Camera_Front",
    }
    cameras = [
        {
            "name": "renamed_camera",
            "remote": True,
            "robot_id": "sim-leader",
            "camera_id": "front",
            "object_name": "Camera_Front",
            "label": "anything",
        }
    ]
    assert match_camera(camera, cameras) == cameras[0]


def test_local_camera_match_ignores_label() -> None:
    identity = {"kind": "local", "index": 2, "name": "2"}
    cameras = [
        {
            "name": "2",
            "index": 2,
            "remote": False,
            "label": "renamed front",
        }
    ]
    assert match_camera(identity, cameras) == cameras[0]


class _FakeSerial:
    def __init__(self, port: str) -> None:
        self.config = SimpleNamespace(port=port)
        self.connected = False
        self.error: str | None = None

    def disconnect(self) -> None:
        self.connected = False


class _FakeCameras:
    def __init__(self) -> None:
        self.rows = [
            {
                "name": "0",
                "index": 0,
                "remote": False,
                "device_key": "local:0",
                "identity": {"kind": "local", "index": 0, "name": "0"},
                "label": "front",
                "enabled": True,
                "show_main": True,
                "feed_robot": True,
                "streaming": True,
                "port": 5000,
            }
        ]
        self.calls: list[tuple[str, Any]] = []

    def snapshots(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows]

    def set_flags(self, name: str, **flags: Any) -> dict[str, Any]:
        self.calls.append(("flags", (name, flags)))
        row = next(row for row in self.rows if row["name"] == name)
        row.update(flags)
        return dict(row)

    def set_stream(self, name: str, enable: bool, port: int | None = None) -> dict[str, Any]:
        self.calls.append(("stream", (name, enable, port)))
        return {}


class _FakeLoop:
    def __init__(self) -> None:
        self.follower = _FakeSerial("COM6")
        self.leader = _FakeSerial("COM5")
        self.follower.connected = True
        self.leader.connected = True
        self.cameras = _FakeCameras()
        self.mode = "idle"
        self.pending = None
        self.writer = None
        self._debug_lease_token = None
        self._pending_release = None
        self.logs: list[tuple[str, str]] = []

    def log(self, level: str, message: str, **_: Any) -> None:
        self.logs.append((level, message))

    def _park_relax_blocking(self) -> None:
        self.logs.append(("info", "parked"))

    def _release_follower(self, reason: str) -> None:
        self.follower.connected = False
        self.logs.append(("info", f"released {reason}"))

    def _connect_follower(self) -> None:
        self.follower.connected = True

    def _remember_port(self, role: str, port: str) -> None:
        self.logs.append(("info", f"remember {role} {port}"))


def test_nothing_connected_disconnects_and_disables(monkeypatch) -> None:
    monkeypatch.setattr(hardware, "list_serial_ports", lambda: [])
    loop = _FakeLoop()

    result = apply_hardware_preset(
        loop,
        "Disconnected",
        {"system": True, "devices": {}, "cameras": {}},
    )

    assert result["ok"] is True
    assert result["complete"] is True
    assert loop.follower.connected is False
    assert loop.leader.connected is False
    assert ("flags", ("0", {"enabled": False, "show_main": False, "feed_robot": False})) in loop.cameras.calls
    assert any("Disconnected" in message for _, message in loop.logs)
    assert any("camera" in message and "disabled" in message for _, message in loop.logs)
