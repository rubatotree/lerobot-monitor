import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from lerobot_monitor.cameras import (
    CameraHub,
    DeviceCamera,
    RemoteMjpegCamera,
    extract_jpeg_frames,
    remote_camera_name,
    supported_resolutions,
)
from lerobot_monitor.config import CamerasConfig


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("show_main", [False, True])
@pytest.mark.parametrize("feed_robot", [False, True])
def test_robot_camera_routing_is_independent_of_display(
    remote: bool, enabled: bool, show_main: bool, feed_robot: bool,
) -> None:
    hub = CameraHub(CamerasConfig(probe=False))
    frame = np.zeros((2, 3, 3), dtype=np.uint8)
    device = SimpleNamespace(
        enabled=enabled, show_main=show_main, feed_robot=feed_robot,
        latest_rgb=lambda: frame, latest_bgr=lambda: frame,
    )
    (hub.remote_streams if remote else hub.streams)["front"] = device
    expected = {"front"} if enabled and feed_robot else set()
    assert set(hub.latest_rgb_map()) == expected
    # Recording may include display-only feeds; only policy routing changes.
    recorded = {"front"} if enabled and (show_main or feed_robot) else set()
    assert set(hub.latest_bgr_map()) == recorded


def test_focus_is_latched_without_opening_device() -> None:
    cam = DeviceCamera(0, width=640, height=480, jpeg_quality=80, port=5000)
    cam.set_focus(autofocus=False, focus=40)
    snap = cam.snapshot()
    assert snap["autofocus"] is False
    assert snap["focus"] == 40.0
    assert snap["device_key"] == "local:0"
    assert snap["identity"] == {"kind": "local", "index": 0, "name": "0"}
    assert cam._focus_dirty.is_set()
    cam.set_focus(focus=300)
    assert cam.focus == 255.0


def test_rollout_rejects_missing_stale_and_unsynchronized_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    hub = CameraHub(CamerasConfig(probe=False))
    first = DeviceCamera(0, width=2, height=2, jpeg_quality=80, port=5000)
    second = DeviceCamera(1, width=2, height=2, jpeg_quality=80, port=5001)
    for camera in (first, second):
        camera.enabled, camera.feed_robot, camera.show_main = True, True, False
    hub.streams = {"front": first, "side": second}
    monkeypatch.setattr("lerobot_monitor.cameras.time.perf_counter", lambda: 10.0)
    with pytest.raises(RuntimeError, match="no frame"):
        hub.rollout_rgb_map(max_age_s=1, max_skew_s=0.25)
    for camera in (first, second):
        camera._bgr = np.full((2, 2, 3), [1, 2, 3], dtype=np.uint8)
        camera.frame_received_at = 9.9
    assert hub.rollout_rgb_map(max_age_s=1, max_skew_s=0.25)["front"][0, 0].tolist() == [3, 2, 1]
    first.frame_received_at = 9.5
    with pytest.raises(RuntimeError, match="time skew"):
        hub.rollout_rgb_map(max_age_s=1, max_skew_s=0.25)
    first.frame_received_at = 8.0
    with pytest.raises(RuntimeError, match="stale"):
        hub.rollout_rgb_map(max_age_s=1, max_skew_s=0.25)


def test_extract_jpeg_frames_handles_multipart_noise() -> None:
    ok, encoded = cv2.imencode(".jpg", np.zeros((4, 4, 3), dtype=np.uint8))
    assert ok
    jpeg = encoded.tobytes()
    buffer = bytearray(b"noise" + jpeg + b"boundary" + jpeg + b"tail")
    frames = extract_jpeg_frames(buffer)
    assert frames == [jpeg, jpeg]
    # 只保留最后一个字节，避免把跨 chunk 的 SOI 前缀丢掉。
    assert bytes(buffer) == b"l"


def test_remote_camera_name_is_url_safe() -> None:
    assert remote_camera_name("sim follower/1", "front camera") == "blender_sim_follower_1_front_camera"


@pytest.mark.parametrize("prefix", ["[dshow @ abc]", "[in#0 @ abc]"])
def test_windows_supported_resolutions_come_from_device_modes(
    monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    devices = "\n".join(
        [
            f'{prefix} "Camera A" (none)',
            f'{prefix}   Alternative name "camera-a-moniker"',
            f'{prefix} "Camera B" (video)',
            f'{prefix}   Alternative name "camera-b-moniker"',
            f'{prefix} "Microphone" (audio)',
            f'{prefix}   Alternative name "microphone-moniker"',
        ]
    )
    options = "\n".join(
        [
            "[dshow @ abc] pixel_format=mjpeg min s=1280x720 fps=5 max s=1280x720 fps=30",
            "[dshow @ abc] pixel_format=yuyv422 min s=640x480 fps=5 max s=640x480 fps=30",
            "[dshow @ abc] pixel_format=mjpeg min s=1280x720 fps=5 max s=1280x720 fps=60",
        ]
    )
    commands: list[list[str]] = []

    def fake_output(command: list[str]) -> str:
        commands.append(command)
        return devices if "-list_devices" in command else options

    monkeypatch.setattr("lerobot_monitor.cameras.sys.platform", "win32")
    monkeypatch.setattr("lerobot_monitor.cameras.shutil.which", lambda _tool: "ffmpeg")
    monkeypatch.setattr("lerobot_monitor.cameras._capture_tool_output", fake_output)

    assert supported_resolutions(1) == [(640, 480), (1280, 720)]
    assert commands[1][-1] == "video=camera-b-moniker"


def test_linux_supported_resolutions_include_all_discrete_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    output = "Size: Discrete 1920x1080\nSize: Discrete 640x480\nSize: Discrete 1920x1080"
    monkeypatch.setattr("lerobot_monitor.cameras.sys.platform", "linux")
    monkeypatch.setattr("lerobot_monitor.cameras.shutil.which", lambda _tool: "v4l2-ctl")
    monkeypatch.setattr("lerobot_monitor.cameras._capture_tool_output", lambda _command: output)

    assert supported_resolutions(2) == [(640, 480), (1920, 1080)]


def test_remote_camera_decodes_frames_from_a_mjpeg_response(monkeypatch: pytest.MonkeyPatch) -> None:
    ok, encoded = cv2.imencode(".jpg", np.full((6, 8, 3), 127, dtype=np.uint8))
    assert ok
    payload = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + encoded.tobytes() + b"\r\n"

    class FakeResponse:
        def __init__(self) -> None:
            self._chunks = [payload[:20], payload[20:], b""]

        def read(self, _size: int) -> bytes:
            return self._chunks.pop(0) if self._chunks else b""

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_urlopen(*_args: object, **_kwargs: object) -> FakeResponse:
        return FakeResponse()

    monkeypatch.setattr("lerobot_monitor.cameras.urllib.request.urlopen", fake_urlopen)
    camera = RemoteMjpegCamera(
        "blender_sim_front",
        {
            "robot_id": "sim",
            "id": "front",
            "label": "Front",
            "object_name": "Camera_Front",
            "url": "http://127.0.0.1:9300/video",
            "width": 8,
            "height": 6,
            "target_fps": 15,
            "quality": 80,
        },
    )
    camera.start_capture()
    try:
        deadline = time.monotonic() + 2.0
        while camera.frame_id == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert camera.frame_id > 0
        bgr = camera.latest_bgr()
        rgb = camera.latest_rgb()
        assert bgr is not None and bgr.shape == (6, 8, 3)
        assert rgb is not None and rgb.shape == (6, 8, 3)
        snapshot = camera.snapshot()
        assert snapshot["remote"] is True
        assert snapshot["source"] == "Blender"
        assert snapshot["url"] == "http://127.0.0.1:9300/video"
        assert snapshot["device_key"] == "remote:sim:front"
        assert snapshot["identity"]["robot_id"] == "sim"
        assert snapshot["identity"]["camera_id"] == "front"
    finally:
        camera.stop_capture()


def test_hub_syncs_remote_cameras_without_removing_them_on_local_rescan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "id": "sim_follower",
            "port": "socket://127.0.0.1:9200",
            "cameras": [
                {
                    "id": "front",
                    "label": "Front",
                    "url": "http://127.0.0.1:9300/video",
                    "width": 640,
                    "height": 480,
                    "target_fps": 15,
                }
            ],
        }
    ]
    monkeypatch.setattr(
        "lerobot_monitor.cameras.discover_sim_robots",
        lambda use_cache=True: rows,
    )
    monkeypatch.setattr(
        "lerobot_monitor.cameras.detect_cameras",
        lambda max_probe=8: [],
    )
    monkeypatch.setattr(RemoteMjpegCamera, "start_capture", lambda self: None)

    hub = CameraHub(CamerasConfig(probe=False))
    snapshots = hub.sync_remote_cameras()
    assert len(snapshots) == 1
    assert snapshots[0]["name"] == "blender_sim_follower_front"
    assert snapshots[0]["show_main"] is True
    assert snapshots[0]["feed_robot"] is False

    assert hub.rescan() == snapshots
    rows.clear()
    assert hub.sync_remote_cameras() == []


def test_remote_camera_controls_are_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    camera = RemoteMjpegCamera(
        "blender_sim_front",
        {
            "robot_id": "sim",
            "id": "front",
            "url": "http://127.0.0.1:9300/video",
        },
    )
    hub = CameraHub(CamerasConfig(probe=False))
    hub.remote_streams[camera.name] = camera
    with pytest.raises(ValueError):
        hub.set_resolution(camera.name, 320, 240)
    with pytest.raises(ValueError):
        hub.set_focus(camera.name, autofocus=False, focus=10)
    with pytest.raises(ValueError):
        hub.set_stream(camera.name, True, 5000)
