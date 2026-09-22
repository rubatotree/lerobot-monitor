"""接入仿真机械臂：把 ``socket://`` 当成串口，并发现 Blender 端公布的虚拟总线。

真机走的是 ``SO101Follower → FeetechMotorsBus → scservo_sdk.PortHandler → serial.Serial``。
仿真机械臂在**字节流层面**假装自己是一串 STS3215，所以 monitor 侧只需要两件事：

1. 让 ``scservo_sdk.PortHandler`` 认识 ``socket://host:port``（见
   :func:`install_socket_transport`）；
2. 知道有哪些虚拟总线可用（见 :func:`discover_sim_robots`）。

除此之外一行控制代码都不用改——校准、扭矩、归一化、急停全部复用。

Blender 端（``blender/lerobot_bridge``）是这套协议的另一半，它把当前上线的
机械臂写进一份注册表文件，并在 ``/info`` 上返回同一份 JSON。
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import Any

SCHEME = "socket://"
CONNECT_TIMEOUT_S = 2.0

# 与 blender/lerobot_bridge/discovery.py 保持一致。
INFO_SCHEMA = "lerobot.sim.bridge/v1"
DEFAULT_INFO_PORT = 9390
REGISTRY_FILE = "registry.json"
INFO_TIMEOUT_S = 0.5

# 发现结果缓存：面板会频繁轮询 /api/ports，不该每次都去读盘 + 发 HTTP。
CACHE_TTL_S = 1.0

_lock = threading.Lock()
_cache: tuple[float, list[dict[str, Any]]] | None = None


# ------------------------------------------------------------------ 串口垫片


def is_socket_url(port_name: Any) -> bool:
    return isinstance(port_name, str) and port_name.startswith(SCHEME)


def parse_socket_url(port_name: str) -> tuple[str, int]:
    """拆出 ``socket://host:port``。IPv6 字面量要写成 ``socket://[::1]:9200``。"""
    rest = port_name[len(SCHEME) :]
    host, _, port_text = rest.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or not port_text.isdigit():
        raise ValueError(f"无效的虚拟串口地址 '{port_name}'，应形如 socket://127.0.0.1:9200")
    return host, int(port_text)


def make_socket_port_handler_class() -> type:
    """构造与 ``scservo_sdk.PortHandler`` 同接口的 socket 实现。

    刻意复刻而非包装 pyserial：``rxPacket`` 的忙等循环依赖 ``timeout=0``
    的"读不到就立刻返回空"语义，换成阻塞 socket 会让它的超时计算失去意义。
    """
    from scservo_sdk.port_handler import PortHandler

    class SocketPortHandler(PortHandler):  # type: ignore[misc, valid-type]
        def __init__(self, port_name: str) -> None:
            super().__init__(port_name)
            self._socket: socket.socket | None = None

        def setupPort(self, cflag_baud: int) -> bool:  # noqa: N802 — 保持 SDK 命名
            if self.is_open:
                self.closePort()
            host, port = parse_socket_url(self.port_name)
            connection = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S)
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection.setblocking(False)
            self._socket = connection
            self.ser = connection  # 部分 SDK 代码直接读 .ser
            self.is_open = True
            self.tx_time_per_byte = (1000.0 / self.baudrate) * 10.0
            return True

        def closePort(self) -> None:  # noqa: N802
            connection = self._socket
            self._socket = None
            self.ser = None
            self.is_open = False
            if connection is None:
                return
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

        def clearPort(self) -> None:  # noqa: N802
            # TCP 没有用户态发送缓冲；而且绝不能丢弃接收缓冲，否则会把还没读走
            # 的状态包吃掉。
            return None

        def getBytesAvailable(self) -> int:  # noqa: N802
            return 0

        def readPort(self, length: int) -> bytes:  # noqa: N802
            connection = self._socket
            if connection is None:
                return b""
            try:
                return connection.recv(length)
            except (BlockingIOError, InterruptedError):
                return b""
            except OSError:
                return b""

        def writePort(self, packet: Any) -> int:  # noqa: N802
            connection = self._socket
            if connection is None:
                return 0
            data = bytes(packet)
            try:
                connection.sendall(data)
            except OSError:
                return 0
            return len(data)

    return SocketPortHandler


def install_socket_transport() -> None:
    """幂等地让 ``scservo_sdk.PortHandler`` 认得出 ``socket://``。

    必须在 ``FeetechMotorsBus.__init__`` 之前调用——它在构造时就抓走了
    ``scs.PortHandler`` 这个引用。
    """
    import scservo_sdk

    if getattr(scservo_sdk.PortHandler, "_lerobot_sim_dispatch", False):
        return

    original = scservo_sdk.PortHandler
    socket_handler = make_socket_port_handler_class()

    class DispatchingPortHandler:
        _lerobot_sim_dispatch = True

        def __new__(cls, port_name: str):  # noqa: D102
            if is_socket_url(port_name):
                return socket_handler(port_name)
            return original(port_name)

    scservo_sdk.PortHandler = DispatchingPortHandler
    try:
        from scservo_sdk import port_handler as port_handler_module

        port_handler_module.PortHandler = DispatchingPortHandler
    except ImportError:
        pass


def is_virtual_port(port_name: Any) -> bool:
    return is_socket_url(port_name)


# -------------------------------------------------------------------- 发现


def registry_dir() -> Path:
    override = os.environ.get("LEROBOT_BRIDGE_HOME")
    if override:
        return Path(override).expanduser()
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "lerobot_bridge"
    return Path.home() / ".cache" / "lerobot_bridge"


def registry_path() -> Path:
    return registry_dir() / REGISTRY_FILE


def read_registry() -> dict[str, Any] | None:
    """读 Blender 端写下的注册表。读不到或格式不对都返回 None。"""
    path = registry_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != INFO_SCHEMA:
        return None
    if not isinstance(payload.get("robots"), list):
        return None
    return payload


def fetch_info(host: str = "127.0.0.1", port: int = DEFAULT_INFO_PORT) -> dict[str, Any] | None:
    """直接问 ``/info``。注册表文件丢失时作为兜底。"""
    import urllib.error
    import urllib.request

    url = f"http://{host}:{port}/info"
    try:
        with urllib.request.urlopen(url, timeout=INFO_TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != INFO_SCHEMA:
        return None
    return payload


def discover_sim_robots(use_cache: bool = True) -> list[dict[str, Any]]:
    """当前上线的仿真机械臂列表。"""
    global _cache

    if use_cache:
        with _lock:
            if _cache is not None and _cache[0] > _now() - CACHE_TTL_S:
                return _cache[1]

    payload = read_registry()
    if payload is None:
        payload = fetch_info()
    robots = [row for row in (payload or {}).get("robots", []) if isinstance(row, dict)]

    with _lock:
        _cache = (_now(), robots)
    return robots


def sim_cameras(robot: dict[str, Any]) -> list[dict[str, Any]]:
    """返回一台仿真机械臂公布的相机列表。

    新协议使用复数 ``cameras``；旧注册表只有单数 ``camera``。这里把两者
    归一化成同一形状，让相机管理器不需要理解协议版本。
    """
    raw = robot.get("cameras")
    rows = raw if isinstance(raw, list) else []
    if not rows:
        single = robot.get("camera")
        rows = [single] if isinstance(single, dict) else []

    cameras: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        camera_id = str(row.get("id") or row.get("label") or f"camera_{index + 1}")
        label = str(row.get("label") or camera_id)
        target_fps = _positive_float(row.get("target_fps", row.get("fps")), 10.0)
        cameras.append(
            {
                "id": camera_id,
                "label": label,
                "object_name": str(row.get("object_name") or ""),
                "type": str(row.get("type") or "mjpeg"),
                "url": url,
                "width": _positive_int(row.get("width"), 640),
                "height": _positive_int(row.get("height"), 480),
                "fps": _positive_float(row.get("fps"), target_fps),
                "target_fps": target_fps,
                "quality": _positive_int(row.get("quality"), 80),
                "enabled": bool(row.get("enabled", True)),
                "clients": max(0, _positive_int(row.get("clients"), 0)),
            }
        )
    return cameras


def list_virtual_ports() -> list[dict[str, Any]]:
    """把仿真机械臂伪装成串口，形状与 :func:`ports.list_serial_ports` 一致。

    这样前端不用区分真假——下拉框里多出来的几条就是虚拟总线。
    """
    rows: list[dict[str, Any]] = []
    for robot in discover_sim_robots():
        port = robot.get("port")
        if not is_virtual_port(port):
            continue
        status = robot.get("status") or {}
        cameras = sim_cameras(robot)
        rows.append(
            {
                "port": port,
                "name": f"{robot.get('type', 'sim')} ({robot.get('id', '?')})",
                "description": _describe(robot, status, cameras),
                "hwid": f"sim:{robot.get('type', '')}",
                "manufacturer": "Blender",
                "likely": True,
                "identity": {
                    "kind": "virtual",
                    "role": str(robot.get("role", "robot")),
                    "robot_id": str(robot.get("id") or ""),
                    "robot_type": str(robot.get("type") or ""),
                    "port": str(port),
                },
                "device_key": f"virtual:{robot.get('role', 'robot')}:{robot.get('id') or 'unknown'}",
                # 以下字段超出真机串口的形状，前端可以忽略，调试面板会用到。
                "virtual": True,
                "role": robot.get("role", "robot"),
                "robot_type": robot.get("type"),
                "robot_id": robot.get("id"),
                "busy": bool(status.get("connected")),
                "camera_url": cameras[0]["url"] if cameras else None,
                "camera_count": len(cameras),
                "cameras": cameras,
            }
        )
    rows.sort(key=lambda row: row["port"])
    return rows


def describe_port(port: str) -> dict[str, Any] | None:
    """给出某个虚拟端口的完整信息，用于连接前的校验与提示。"""
    for robot in discover_sim_robots():
        if robot.get("port") == port:
            return robot
    return None


def _describe(
    robot: dict[str, Any], status: dict[str, Any], cameras: list[dict[str, Any]]
) -> str:
    parts = [f"仿真 {robot.get('label') or robot.get('type', '?')}"]
    parts.append("主臂" if robot.get("role") == "leader" else "从臂")
    parts.append("已连接" if status.get("connected") else "空闲")
    if cameras:
        parts.append(f"相机 {len(cameras)} 路")
        parts.append(str(cameras[0]["url"]))
    return " · ".join(parts)


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _positive_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _now() -> float:
    import time

    return time.time()


def invalidate_cache() -> None:
    """扫描按钮按下时清缓存，避免看到上一秒的快照。"""
    global _cache
    with _lock:
        _cache = None
