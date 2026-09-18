"""Local camera manager (merged from win_cam_server).

Each DirectShow / V4L device has a capture thread. A dedicated MJPEG port is
started only when the operator enables the network stream. Capture itself does
not occupy that port, so lerobot-record can later open http://host:5000/.
"""

from __future__ import annotations

import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import cv2
import numpy as np

from .config import CamerasConfig
from .store import JsonStore


def detect_cameras(max_probe: int = 8) -> list[int]:
    """Probe indices 0..max_probe-1 and return those that open."""
    available: list[int] = []
    logger = cv2.utils.logging
    previous_level = logger.getLogLevel()
    logger.setLogLevel(logger.LOG_LEVEL_SILENT)
    try:
        for idx in range(max_probe):
            cap = (
                cv2.VideoCapture(idx, cv2.CAP_DSHOW)
                if sys.platform == "win32"
                else cv2.VideoCapture(idx)
            )
            try:
                if cap.isOpened():
                    available.append(idx)
            finally:
                cap.release()
    finally:
        logger.setLogLevel(previous_level)
    return available


class DeviceCamera:
    def __init__(self, index: int, *, width: int, height: int, jpeg_quality: int, port: int) -> None:
        self.index = index
        self.name = str(index)
        self.width = width
        self.height = height
        self.jpeg_quality = jpeg_quality
        self.port = port
        self.sensor_width = width
        self.sensor_height = height
        self.connected = False
        self.error: str | None = None
        self.capture_fps = 0.0
        self.frame_id = 0
        self.streaming = False
        self.enabled = True
        self.show_main = False
        self.feed_robot = False
        self.label = f"cam {index}"
        self.autofocus = True
        self.focus = 0.0
        self.focus_min = 0.0
        self.focus_max = 255.0
        self._focus_dirty = threading.Event()

        self._jpeg = b""
        self._bgr: np.ndarray | None = None
        self._lock = threading.Lock()
        self._frame_ready = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None

    def start_capture(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"cam-{self.index}", daemon=True)
        self._thread.start()

    def stop_capture(self) -> None:
        self.stop_streaming()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def restart_capture(self) -> None:
        streaming = self.streaming
        port = self.port
        self.stop_capture()
        self.start_capture()
        if streaming:
            self.start_streaming(port)

    def latest_jpeg(self) -> bytes:
        with self._lock:
            return self._jpeg

    def latest_bgr(self) -> np.ndarray | None:
        with self._lock:
            return None if self._bgr is None else self._bgr.copy()

    def latest_rgb(self) -> np.ndarray | None:
        bgr = self.latest_bgr()
        if bgr is None:
            return None
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    def wait_for_frame(self, last_id: int, timeout: float = 1.0) -> tuple[int, bytes]:
        with self._frame_ready:
            self._frame_ready.wait_for(lambda: self.frame_id != last_id, timeout=timeout)
            return self.frame_id, self._jpeg

    def snapshot(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "connected": self.connected,
            "error": self.error,
            "fps": round(self.capture_fps, 1),
            "width": self.width,
            "height": self.height,
            "sensor_width": self.sensor_width,
            "sensor_height": self.sensor_height,
            "port": self.port,
            "streaming": self.streaming,
            "enabled": self.enabled,
            "show_main": self.show_main,
            "feed_robot": self.feed_robot,
            "label": self.label,
            "autofocus": self.autofocus,
            "focus": round(float(self.focus), 1),
            "focus_min": self.focus_min,
            "focus_max": self.focus_max,
            "frame_id": self.frame_id,
            "url": f"http://127.0.0.1:{self.port}/video" if self.streaming else None,
        }

    def set_focus(self, *, autofocus: bool | None = None, focus: float | None = None) -> None:
        if autofocus is not None:
            self.autofocus = bool(autofocus)
        if focus is not None:
            self.focus = max(self.focus_min, min(self.focus_max, float(focus)))
        self._focus_dirty.set()

    def _apply_focus(self, cap: cv2.VideoCapture) -> None:
        try:
            cap.set(cv2.CAP_PROP_AUTOFOCUS, 1.0 if self.autofocus else 0.0)
            if not self.autofocus:
                cap.set(cv2.CAP_PROP_FOCUS, float(self.focus))
                actual = cap.get(cv2.CAP_PROP_FOCUS)
                if actual is not None and actual >= 0:
                    self.focus = float(actual)
        except Exception:
            return

    def _open(self) -> cv2.VideoCapture:
        if sys.platform == "win32":
            return cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
        return cv2.VideoCapture(self.index)

    def _negotiate(self, cap: cv2.VideoCapture, target: tuple[int, int]) -> tuple[int, int]:
        actual = target
        for _ in range(2):
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, target[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, target[1])
            cap.read()
            actual = (
                round(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                round(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            )
            if actual == target or actual[0] <= 0 or actual[1] <= 0:
                break
        return actual if actual[0] > 0 and actual[1] > 0 else target

    def _loop(self) -> None:
        while not self._stop.is_set():
            cap = self._open()
            if not cap.isOpened():
                self.connected = False
                self.error = f"cannot open device {self.index}"
                cap.release()
                self._stop.wait(1.0)
                continue

            target = (self.width, self.height)
            self.sensor_width, self.sensor_height = self._negotiate(cap, target)
            self._apply_focus(cap)
            self._focus_dirty.clear()
            self.connected = True
            self.error = None
            frames = 0
            window = time.perf_counter()
            while not self._stop.is_set():
                if self._focus_dirty.is_set():
                    self._apply_focus(cap)
                    self._focus_dirty.clear()
                ok, frame = cap.read()
                if not ok or frame is None:
                    self.connected = False
                    self.error = "stream dropped"
                    break
                if (frame.shape[1], frame.shape[0]) != target:
                    frame = cv2.resize(frame, target, interpolation=cv2.INTER_AREA)
                ok_j, buf = cv2.imencode(
                    ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
                )
                with self._frame_ready:
                    self._bgr = frame
                    if ok_j:
                        self._jpeg = buf.tobytes()
                    self.frame_id += 1
                    self._frame_ready.notify_all()
                frames += 1
                now = time.perf_counter()
                if now - window >= 1.0:
                    self.capture_fps = frames / (now - window)
                    frames = 0
                    window = now
            cap.release()
            if not self._stop.is_set():
                self._stop.wait(0.5)

    def start_streaming(self, port: int | None = None) -> tuple[bool, str]:
        if self.streaming:
            self.stop_streaming()
        if port is not None:
            self.port = int(port)
        if self._thread is None or not self._thread.is_alive():
            self.start_capture()
        stream = self

        class _MjpegServer(ThreadingHTTPServer):
            allow_reuse_address = True

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def _cors(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, no-store")

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                path = (self.path or "/").split("?", 1)[0]
                if path in {"/status", "/info"}:
                    body = (
                        f'{{"index":{stream.index},"port":{stream.port},'
                        f'"streaming":{str(stream.streaming).lower()}}}\n'
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self._cors()
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path not in {"/", "/video", "/stream.mjpg", "/stream"}:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self._cors()
                self.end_headers()
                try:
                    last_id = -1
                    while True:
                        frame_id, jpeg = stream.wait_for_frame(last_id)
                        if not jpeg:
                            continue
                        last_id = frame_id
                        self.wfile.write(
                            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                            + str(len(jpeg)).encode()
                            + b"\r\n\r\n"
                            + jpeg
                            + b"\r\n"
                        )
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    return

        try:
            self._server = _MjpegServer(("0.0.0.0", self.port), _Handler)
        except OSError as exc:
            return False, str(exc)
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name=f"mjpeg-{self.index}",
        )
        self._server_thread.start()
        self.streaming = True
        return True, ""

    def stop_streaming(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass
            try:
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        self._server_thread = None
        self.streaming = False


class CameraHub:
    def __init__(self, config: CamerasConfig, store: JsonStore | None = None) -> None:
        self.config = config
        self.store = store
        self.streams: dict[str, DeviceCamera] = {}
        self._lock = threading.Lock()

    def _persist(self, device: DeviceCamera) -> None:
        if self.store is None:
            return
        self.store.save_camera(
            device.name,
            {
                "label": device.label,
                "enabled": device.enabled,
                "show_main": device.show_main,
                "feed_robot": device.feed_robot,
                "streaming": device.streaming,
                "port": device.port,
                "width": device.width,
                "height": device.height,
                "autofocus": device.autofocus,
                "focus": device.focus,
            },
        )

    def _apply_saved(self, device: DeviceCamera) -> None:
        saved = self.store.camera_settings(device.name) if self.store else {}
        if saved.get("label"):
            device.label = str(saved["label"])
        else:
            device.label = f"cam {device.index}"
        device.enabled = bool(saved.get("enabled", True))
        device.show_main = bool(saved.get("show_main", False))
        device.feed_robot = bool(saved.get("feed_robot", False))
        if saved.get("port"):
            device.port = int(saved["port"])
        if saved.get("width"):
            device.width = int(saved["width"])
        if saved.get("height"):
            device.height = int(saved["height"])
        if "autofocus" in saved:
            device.autofocus = bool(saved["autofocus"])
        if saved.get("focus") is not None:
            device.focus = float(saved["focus"])
        want_stream = bool(saved.get("streaming", False))
        if device.enabled:
            device.start_capture()
            if want_stream:
                device.start_streaming(device.port)
        else:
            device.show_main = False
            device.feed_robot = False

    def start(self) -> None:
        if self.config.probe:
            self.rescan()

    def stop(self) -> None:
        with self._lock:
            devices = list(self.streams.values())
        for device in devices:
            device.stop_capture()

    def rescan(self) -> list[dict[str, Any]]:
        found = detect_cameras(self.config.max_probe)
        with self._lock:
            existing = set(self.streams.keys())
            wanted = {str(i) for i in found}
            for name in existing - wanted:
                self.streams[name].stop_capture()
                del self.streams[name]
            for index in found:
                name = str(index)
                if name in self.streams:
                    continue
                device = DeviceCamera(
                    index,
                    width=self.config.default_width,
                    height=self.config.default_height,
                    jpeg_quality=self.config.jpeg_quality,
                    port=self.config.default_port_base + index,
                )
                self.streams[name] = device
                self._apply_saved(device)
        return self.snapshots()

    def get(self, name: str) -> DeviceCamera:
        with self._lock:
            device = self.streams.get(str(name))
        if device is None:
            raise KeyError(name)
        return device

    def set_resolution(self, name: str, width: int, height: int) -> dict[str, Any]:
        if width <= 0 or height <= 0:
            raise ValueError("invalid resolution")
        device = self.get(name)
        device.width = width
        device.height = height
        if device.enabled:
            device.restart_capture()
        self._persist(device)
        return device.snapshot()

    def set_focus(
        self,
        name: str,
        *,
        autofocus: bool | None = None,
        focus: float | None = None,
    ) -> dict[str, Any]:
        device = self.get(name)
        device.set_focus(autofocus=autofocus, focus=focus)
        self._persist(device)
        return device.snapshot()

    def set_label(self, name: str, label: str) -> dict[str, Any]:
        device = self.get(name)
        text = label.strip()
        device.label = text if text else f"cam {device.index}"
        self._persist(device)
        return device.snapshot()

    def set_flags(
        self,
        name: str,
        *,
        enabled: bool | None = None,
        show_main: bool | None = None,
        feed_robot: bool | None = None,
    ) -> dict[str, Any]:
        device = self.get(name)
        if enabled is not None:
            device.enabled = enabled
            if enabled:
                device.start_capture()
            else:
                device.stop_capture()
                device.show_main = False
                device.feed_robot = False
        if show_main is not None:
            device.show_main = bool(show_main) and device.enabled
            if not device.show_main:
                device.feed_robot = False
        if feed_robot is not None:
            device.feed_robot = bool(feed_robot) and device.enabled and device.show_main
        self._persist(device)
        return device.snapshot()

    def set_stream(self, name: str, enable: bool, port: int | None = None) -> dict[str, Any]:
        device = self.get(name)
        if enable:
            if not device.enabled:
                device.enabled = True
            ok, err = device.start_streaming(port)
            if not ok:
                raise RuntimeError(err)
        else:
            device.stop_streaming()
            if port is not None:
                device.port = int(port)
        self._persist(device)
        return device.snapshot()

    def names(self) -> list[str]:
        with self._lock:
            return list(self.streams.keys())

    def latest_jpeg(self, name: str) -> bytes:
        try:
            return self.get(name).latest_jpeg()
        except KeyError:
            return b""

    def latest_rgb_map(self) -> dict[str, np.ndarray]:
        return self._image_map("rgb", robot=True)

    def latest_bgr_map(self) -> dict[str, np.ndarray]:
        return self._image_map("bgr", robot=False)

    def latest_main_bgr_map(self) -> dict[str, np.ndarray]:
        """Frames shown on the main view, keyed by camera label."""
        out: dict[str, np.ndarray] = {}
        with self._lock:
            devices = list(self.streams.values())
        for device in devices:
            if not device.enabled or not device.show_main:
                continue
            frame = device.latest_bgr()
            if frame is None:
                continue
            key = device.label.strip() if device.label else device.name
            out[key or device.name] = frame
        return out

    def _image_map(self, color: str, *, robot: bool) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        with self._lock:
            devices = list(self.streams.items())
        for name, device in devices:
            if not device.enabled:
                continue
            if robot and not (device.show_main and device.feed_robot):
                continue
            if not robot and not (device.show_main or device.feed_robot):
                continue
            frame = device.latest_rgb() if color == "rgb" else device.latest_bgr()
            if frame is not None:
                out[name] = frame
        return out

    def snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            devices = list(self.streams.values())
        return [device.snapshot() for device in devices]
