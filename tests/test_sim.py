"""monitor 侧的虚拟串口：地址解析、发现注册表、以及与仿真总线的真实对接。

最后一组测试会去 import 隔壁 ``blender/lerobot_bridge``——那是这套协议的另一半。
它不在就整组跳过：monitor 自己的测试不该被另一个仓库的目录结构拖住。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from lerobot_monitor.ports import list_serial_ports
from lerobot_monitor.sim import (
    INFO_SCHEMA,
    describe_port,
    discover_sim_robots,
    install_socket_transport,
    invalidate_cache,
    is_virtual_port,
    list_virtual_ports,
    parse_socket_url,
    read_registry,
    registry_dir,
    registry_path,
    sim_cameras,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BLENDER_ROOT = REPO_ROOT / "blender"


# ------------------------------------------------------------- 地址与判定


def test_parse_socket_url_round_trip() -> None:
    assert parse_socket_url("socket://127.0.0.1:9200") == ("127.0.0.1", 9200)
    assert parse_socket_url("socket://192.168.1.7:1") == ("192.168.1.7", 1)
    # IPv6 字面量带方括号，交给 create_connection 之前要把括号去掉。
    assert parse_socket_url("socket://[::1]:9200") == ("::1", 9200)


@pytest.mark.parametrize(
    "bad",
    ["socket://127.0.0.1", "socket://:9200", "socket://host:port", "COM6", "socket://[::1]"],
)
def test_parse_socket_url_rejects_bad_names(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_socket_url(bad)


def test_is_virtual_port_only_accepts_socket_scheme() -> None:
    assert is_virtual_port("socket://127.0.0.1:9200")
    assert not is_virtual_port("COM6")
    assert not is_virtual_port(None)
    assert not is_virtual_port(42)


# ------------------------------------------------------------------ 发现


@pytest.fixture
def registry_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把注册表目录指到临时目录，并保证缓存不跨用例泄漏。"""
    monkeypatch.setenv("LEROBOT_BRIDGE_HOME", str(tmp_path))
    invalidate_cache()
    try:
        yield tmp_path
    finally:
        invalidate_cache()


def _robot_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "role": "robot",
        "type": "so101_follower",
        "label": "SO-101 从臂",
        "id": "sim_follower",
        "port": "socket://127.0.0.1:9200",
        "camera": {"url": "http://127.0.0.1:9300/stream"},
        "status": {"connected": False, "clients": 0},
    }
    row.update(overrides)
    return row


def _write_registry(robots: list[dict[str, Any]], schema: str = INFO_SCHEMA) -> None:
    registry_path().write_text(
        json.dumps({"schema": schema, "robots": robots}, ensure_ascii=False), encoding="utf-8"
    )


def test_registry_dir_honours_env_override(registry_home: Path) -> None:
    assert registry_dir() == registry_home
    assert registry_path() == registry_home / "registry.json"


def test_registry_dir_falls_back_to_local_appdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEROBOT_BRIDGE_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert registry_dir() == tmp_path / "lerobot_bridge"


def test_read_registry_missing_file_is_none(registry_home: Path) -> None:
    assert read_registry() is None


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "[]",
        '{"schema": "something.else/v9", "robots": []}',
        '{"schema": "lerobot.sim.bridge/v1", "robots": "nope"}',
    ],
)
def test_read_registry_rejects_malformed_payloads(registry_home: Path, payload: str) -> None:
    registry_path().write_text(payload, encoding="utf-8")
    assert read_registry() is None


def test_read_registry_accepts_matching_schema(registry_home: Path) -> None:
    _write_registry([_robot_row()])
    payload = read_registry()
    assert payload is not None
    assert payload["robots"][0]["id"] == "sim_follower"


# ---------------------------------------------------------------- 端口行


def test_list_virtual_ports_shape_matches_real_ports(registry_home: Path) -> None:
    _write_registry([_robot_row()])
    rows = list_virtual_ports()
    assert len(rows) == 1
    row = rows[0]
    # 真机串口的字段一个都不能少，否则前端会读到 undefined。
    for key in ("port", "name", "description", "hwid", "manufacturer", "likely"):
        assert key in row, f"缺少 {key}"
    assert row["port"] == "socket://127.0.0.1:9200"
    assert row["virtual"] is True
    assert row["likely"] is True
    assert row["role"] == "robot"
    assert row["robot_type"] == "so101_follower"
    assert row["camera_url"] == "http://127.0.0.1:9300/stream"
    assert row["busy"] is False


def test_list_virtual_ports_skips_rows_without_socket_port(registry_home: Path) -> None:
    """注册表里混进真机串口名时不能把它当成虚拟总线重复列出。"""
    _write_registry([_robot_row(port="COM6")])
    assert list_virtual_ports() == []


def test_list_virtual_ports_marks_connected_bus_busy(registry_home: Path) -> None:
    _write_registry([_robot_row(status={"connected": True, "clients": 1})])
    assert list_virtual_ports()[0]["busy"] is True


def test_list_virtual_ports_survives_robot_without_status_or_camera(registry_home: Path) -> None:
    _write_registry([{"type": "so101_leader", "id": "lead", "port": "socket://127.0.0.1:9201"}])
    row = list_virtual_ports()[0]
    assert row["camera_url"] is None
    assert row["busy"] is False
    assert row["role"] == "robot"  # 缺省按从臂描述，不影响连接


def test_sim_cameras_prefers_the_multi_camera_payload() -> None:
    cameras = sim_cameras(
        _robot_row(
            cameras=[
                {
                    "id": "front",
                    "label": "Front",
                    "object_name": "Camera_Front",
                    "url": "http://127.0.0.1:9300/video",
                    "width": 1280,
                    "height": 720,
                    "target_fps": 15,
                    "quality": 82,
                },
                {
                    "id": "side",
                    "url": "http://127.0.0.1:9301/video",
                    "enabled": False,
                },
            ]
        )
    )
    assert [camera["id"] for camera in cameras] == ["front", "side"]
    assert cameras[0]["label"] == "Front"
    assert cameras[0]["object_name"] == "Camera_Front"
    assert cameras[0]["width"] == 1280
    assert cameras[0]["target_fps"] == 15.0
    assert cameras[1]["enabled"] is False


def test_sim_cameras_falls_back_to_the_legacy_single_camera() -> None:
    cameras = sim_cameras(_robot_row())
    assert len(cameras) == 1
    assert cameras[0]["url"] == "http://127.0.0.1:9300/stream"
    assert cameras[0]["enabled"] is True


def test_list_virtual_ports_reports_camera_count(registry_home: Path) -> None:
    _write_registry(
        [
            _robot_row(
                cameras=[
                    {"id": "front", "url": "http://127.0.0.1:9300/video"},
                    {"id": "side", "url": "http://127.0.0.1:9301/video"},
                ]
            )
        ]
    )
    row = list_virtual_ports()[0]
    assert row["camera_count"] == 2
    assert len(row["cameras"]) == 2
    assert "相机 2 路" in row["description"]


def test_describe_port_finds_by_port(registry_home: Path) -> None:
    _write_registry([_robot_row(role="leader", label="SO-101 主臂")])
    described = describe_port("socket://127.0.0.1:9200")
    assert described is not None
    assert described["label"] == "SO-101 主臂"
    assert describe_port("socket://127.0.0.1:9999") is None


def test_discovery_is_cached_until_invalidated(registry_home: Path) -> None:
    _write_registry([_robot_row(id="first")])
    assert [row["id"] for row in discover_sim_robots()] == ["first"]

    # 缓存窗口内换掉注册表内容，读到的应当还是上一份快照。
    _write_registry([_robot_row(id="second")])
    assert [row["id"] for row in discover_sim_robots()] == ["first"]

    invalidate_cache()
    assert [row["id"] for row in discover_sim_robots()] == ["second"]


def test_cache_can_be_bypassed(registry_home: Path) -> None:
    _write_registry([_robot_row(id="first")])
    discover_sim_robots()
    _write_registry([_robot_row(id="second")])
    assert [row["id"] for row in discover_sim_robots(use_cache=False)] == ["second"]


def test_port_listing_merges_virtual_rows(registry_home: Path) -> None:
    """真机枚举与虚拟总线合并在同一份列表里，前端不需要区分两者。"""
    _write_registry([_robot_row()])
    ports = [row["port"] for row in list_serial_ports()]
    assert "socket://127.0.0.1:9200" in ports


# ---------------------------------------------------------------- 串口垫片


def test_install_socket_transport_is_idempotent() -> None:
    scservo_sdk = pytest.importorskip("scservo_sdk")

    install_socket_transport()
    installed = scservo_sdk.PortHandler
    install_socket_transport()
    assert scservo_sdk.PortHandler is installed


def test_socket_url_dispatches_to_the_socket_handler() -> None:
    """装完之后 scservo_sdk.PortHandler 必须按名字分流，且不能碰真机串口。"""
    scservo_sdk = pytest.importorskip("scservo_sdk")

    install_socket_transport()
    virtual = scservo_sdk.PortHandler("socket://127.0.0.1:1")
    assert type(virtual).__name__ == "SocketPortHandler"
    assert hasattr(virtual, "_socket")  # 只是构造，没连上去

    real = scservo_sdk.PortHandler("COM99")
    assert type(real).__name__ == "PortHandler"
    assert not hasattr(real, "_socket")


# -------------------------------------------------- 与仿真总线的真实对接


def _load_emulator() -> Any:
    """按文件路径加载 blender 侧的仿真总线，避免两个仓库的 tests 包互相遮蔽。"""
    path = BLENDER_ROOT / "tests" / "test_lerobot_integration.py"
    if not path.is_file():
        pytest.skip(f"未找到仿真总线实现：{path}")
    spec = importlib.util.spec_from_file_location("blender_sim_emulator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def emulator() -> Any:
    """起一台仿真总线。它假装自己是六颗 STS3215，监听一个本地 TCP 端口。"""
    pytest.importorskip("scservo_sdk")
    install_socket_transport()
    module = _load_emulator()
    bus = module.SimBus()
    bus.start()
    try:
        yield module, bus
    finally:
        bus.stop()


@pytest.fixture
def calibration_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 lerobot 的校准目录指到临时目录。

    ``HF_LEROBOT_CALIBRATION`` 是在 ``constants`` 导入时算好的常量，改环境变量
    已经来不及，只能改 ``robots.robot`` 模块里绑定的那个名字。
    """
    import lerobot.robots.robot as robot_module

    root = tmp_path / "calibration"
    monkeypatch.setattr(robot_module, "HF_LEROBOT_CALIBRATION", root)
    return root


def _write_follower_calibration(root: Path, robot_id: str) -> None:
    """用扩展自己的导出逻辑写校准，确保格式与真机完全一致。"""
    if str(BLENDER_ROOT) not in sys.path:
        sys.path.insert(0, str(BLENDER_ROOT))
    from lerobot_bridge.calibration import build_calibration_document
    from lerobot_bridge.mapping import JointCalibration
    from lerobot_bridge.profiles import get_profile

    profile = get_profile("so101_follower")
    calibrations = {
        spec.name: JointCalibration(range_min=0, range_max=spec.resolution - 1)
        for spec in profile.joints
    }
    directory = root / "robots" / "so_follower"
    directory.mkdir(parents=True, exist_ok=True)
    document = build_calibration_document(profile, calibrations)
    (directory / f"{robot_id}.json").write_text(
        json.dumps(document, indent=4) + "\n", encoding="utf-8"
    )


def test_follower_arm_connects_to_emulated_bus(
    emulator: Any, calibration_home: Path, registry_home: Path
) -> None:
    """monitor 的 FollowerArm 拿一个 socket:// 端口名就能连上仿真机械臂。

    这条路径上没有一行是为仿真特判的：``FollowerArm.connect`` 只是把串口名
    原样交给 lerobot，垫片负责把字节流接到 Blender 那边。
    """
    from lerobot_monitor.config import RobotConfig
    from lerobot_monitor.robot import FollowerArm

    _module, bus = emulator
    robot_id = "sim_follower"
    _write_follower_calibration(calibration_home, robot_id)

    follower = FollowerArm(RobotConfig(port=bus.url, id=robot_id, use_degrees=True))
    follower.connect()
    try:
        assert follower.connected, follower.error
        assert set(follower.get_pose()) == set(follower.snapshot()["joints"])
        pose = follower.get_pose()
        assert all(abs(pose[name]) < 0.1 for name in pose if name != "gripper")
        assert abs(pose["gripper"]) < 10.0
    finally:
        follower.disconnect()


def test_rollout_style_action_moves_the_emulated_scene(
    emulator: Any, calibration_home: Path, registry_home: Path
) -> None:
    """模型 rollout 下发的动作要真的驱动仿真场景，这是整条链路的目的。"""
    from lerobot_monitor.config import RobotConfig
    from lerobot_monitor.robot import FollowerArm

    _module, bus = emulator
    robot_id = "sim_follower"
    _write_follower_calibration(calibration_home, robot_id)

    follower = FollowerArm(RobotConfig(port=bus.url, id=robot_id, use_degrees=True))
    follower.connect()
    try:
        follower.enable_torque()
        target = {"shoulder_pan": 30.0, "elbow_flex": -25.0, "gripper": 80.0}
        sent = follower.send_pose({name: 0.0 for name in follower.snapshot()["joints"]} | target)
        assert set(sent) == set(follower.snapshot()["joints"])

        # sync_write does not acknowledge a broadcast packet. Wait until the
        # transport thread has applied the goals before advancing the scene.
        deadline = time.monotonic() + 2.0
        while any(
            abs(bus.bank.goal_lerobot_position(name) - value) >= 0.5
            for name, value in target.items()
        ):
            assert time.monotonic() < deadline, "仿真总线未在超时前收到目标关节位置"
            time.sleep(0.005)

        for _ in range(5):
            bus.tick()

        pose = follower.get_pose()
        for name, value in target.items():
            assert abs(pose[name] - value) < 0.5, f"{name}: 期望 {value}，实得 {pose[name]}"
    finally:
        follower.disconnect()


def test_port_listing_offers_the_running_emulated_bus(emulator: Any, registry_home: Path) -> None:
    """把注册表写到 monitor 会读的位置，虚拟总线就出现在端口下拉框里。"""
    _module, bus = emulator

    if str(BLENDER_ROOT) not in sys.path:
        sys.path.insert(0, str(BLENDER_ROOT))
    from lerobot_bridge.discovery import build_payload

    # 用扩展自己的建包函数写注册表，格式对不上会在这里就暴露。
    payload = build_payload([_robot_row(port=bus.url, id="sim_follower")])
    registry_path().write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    invalidate_cache()

    rows = [row for row in list_serial_ports() if row["port"] == bus.url]
    assert len(rows) == 1
    assert rows[0]["virtual"] is True
    assert rows[0]["likely"] is True
    assert describe_port(bus.url) is not None
