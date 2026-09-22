"""Hardware preset matching and application.

Hardware presets are deliberately identity-based. Display labels are settings,
not keys: renaming a camera must not change which physical device a preset
addresses.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from .ports import list_serial_ports

if TYPE_CHECKING:
    from .loop import ControlLoop


SYSTEM_HARDWARE_PRESET_NAME = "Disconnected"
DEFAULT_HARDWARE_PRESETS: dict[str, dict[str, Any]] = {
    SYSTEM_HARDWARE_PRESET_NAME: {
        "schema": 1,
        "system": True,
        "devices": {},
        "cameras": {},
    }
}


def match_serial_device(
    identity: dict[str, Any] | None,
    ports: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Match a saved identity without guessing when a stable identifier exists."""
    if not isinstance(identity, dict):
        return None
    kind = str(identity.get("kind") or "")
    saved_port = str(identity.get("port") or "")

    if kind == "virtual":
        role = str(identity.get("role") or "")
        robot_id = str(identity.get("robot_id") or "")
        candidates = [
            row
            for row in ports
            if row.get("virtual")
            and str(row.get("role") or "") == role
            and str(row.get("robot_id") or "") == robot_id
        ]
        if len(candidates) == 1:
            return candidates[0]
        return next((row for row in candidates if str(row.get("port") or "") == saved_port), None)

    hwid = str(identity.get("hwid") or "")
    if hwid:
        candidates = [row for row in ports if str(row.get("hwid") or "") == hwid]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            return next(
                (row for row in candidates if str(row.get("port") or "") == saved_port),
                None,
            )
        return None

    return next(
        (row for row in ports if str(row.get("port") or "") == saved_port),
        None,
    )


def camera_identity(snapshot: dict[str, Any]) -> dict[str, Any]:
    saved = snapshot.get("identity")
    if isinstance(saved, dict):
        return dict(saved)
    if snapshot.get("remote"):
        return {
            "kind": "remote",
            "robot_id": str(snapshot.get("robot_id") or ""),
            "camera_id": str(snapshot.get("camera_id") or ""),
            "object_name": str(snapshot.get("object_name") or ""),
        }
    return {
        "kind": "local",
        "index": snapshot.get("index"),
        "name": str(snapshot.get("name") or ""),
    }


def match_camera(
    identity: dict[str, Any] | None,
    cameras: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(identity, dict):
        return None
    kind = str(identity.get("kind") or "")
    if kind == "remote":
        robot_id = str(identity.get("robot_id") or "")
        camera_id = str(identity.get("camera_id") or "")
        object_name = str(identity.get("object_name") or "")
        candidates = [
            row
            for row in cameras
            if row.get("remote")
            and str(row.get("robot_id") or "") == robot_id
            and str(row.get("camera_id") or "") == camera_id
        ]
        if object_name:
            exact = [
                row
                for row in candidates
                if not row.get("object_name") or str(row.get("object_name") or "") == object_name
            ]
            if exact:
                candidates = exact
        return candidates[0] if len(candidates) == 1 else None

    name = str(identity.get("name") or "")
    index = identity.get("index")
    for row in cameras:
        if row.get("remote"):
            continue
        if index is not None and row.get("index") == index:
            return row
        if name and str(row.get("name") or "") == name:
            return row
    return None


def apply_hardware_preset(
    loop: "ControlLoop",
    name: str,
    preset: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Apply one preset synchronously on the control-loop thread."""
    preflight_error = _preflight_error(loop)
    if preflight_error:
        loop.log("error", f'hardware preset "{name}" blocked: {preflight_error}')
        return {"ok": False, "error": preflight_error}

    results: list[dict[str, Any]] = []
    devices = preset.get("devices")
    cameras = preset.get("cameras")
    devices = devices if isinstance(devices, dict) else {}
    cameras = cameras if isinstance(cameras, dict) else {}
    ports = list_serial_ports()

    loop.log(
        "info",
        f'hardware preset "{name}": applying {len(devices)} device(s), {len(cameras)} camera(s)',
    )

    _apply_serial_role(loop, name, "arm", devices.get("arm"), ports, results, force=force)
    _apply_serial_role(loop, name, "leader", devices.get("leader"), ports, results, force=force)
    _apply_cameras(loop, name, cameras, results)

    counts = {
        "success": sum(1 for row in results if row["status"] == "success"),
        "skipped": sum(1 for row in results if row["status"] == "skipped"),
        "failed": sum(1 for row in results if row["status"] == "failed"),
    }
    complete = counts["failed"] == 0 and counts["skipped"] == 0
    level = "info" if complete else "error"
    loop.log(
        level,
        f'hardware preset "{name}" finished: '
        f'{counts["success"]} ok, {counts["skipped"]} skipped, {counts["failed"]} failed',
    )
    return {
        "ok": True,
        "complete": complete,
        "results": results,
        "summary": {**counts, "total": len(results)},
    }


def _preflight_error(loop: "ControlLoop") -> str:
    if loop._debug_lease_token is not None:
        return "model debug is active"
    if loop.pending is not None:
        return f"task {loop.pending} is pending"
    if loop.writer is not None:
        return "recording or capture is active"
    if loop._pending_release is not None:
        return "a bus release is in progress"
    if loop.mode not in {"idle", "offline"}:
        return f"control loop is {loop.mode}"
    return ""


def _apply_serial_role(
    loop: "ControlLoop",
    preset_name: str,
    role: str,
    spec: Any,
    ports: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    force: bool,
) -> None:
    arm = loop.follower
    leader = loop.leader
    device = arm if role == "arm" else leader
    identity = spec.get("identity") if isinstance(spec, dict) else None

    if not isinstance(identity, dict):
        if device.connected:
            try:
                _disconnect_serial_role(loop, role, force=force)
            except Exception as exc:  # noqa: BLE001
                _record_result(
                    loop,
                    preset_name,
                    results,
                    role,
                    identity,
                    "failed",
                    f"disconnect failed: {exc}",
                )
                return
        _record_result(loop, preset_name, results, role, identity, "success", "disconnected")
        return

    matched = match_serial_device(identity, ports)
    if matched is None:
        if device.connected:
            try:
                _disconnect_serial_role(loop, role, force=force)
            except Exception as exc:  # noqa: BLE001
                _record_result(
                    loop,
                    preset_name,
                    results,
                    role,
                    identity,
                    "failed",
                    f"device not found and disconnect failed: {exc}",
                )
                return
        _record_result(
            loop,
            preset_name,
            results,
            role,
            identity,
            "skipped",
            "saved device not found; no fallback by old port",
        )
        return

    resolved_port = str(matched.get("port") or "")
    if device.connected and str(device.config.port) == resolved_port:
        _record_result(
            loop,
            preset_name,
            results,
            role,
            identity,
            "success",
            f"already connected on {resolved_port}",
            resolved_port=resolved_port,
        )
        return

    try:
        if device.connected:
            _disconnect_serial_role(loop, role, force=force)
        device.config.port = resolved_port
        if role == "arm":
            loop._connect_follower()
            if not arm.connected:
                raise RuntimeError(arm.error or "follower connect failed")
        else:
            leader.connect()
        loop._remember_port(role, resolved_port)
    except Exception as exc:  # noqa: BLE001
        _record_result(
            loop,
            preset_name,
            results,
            role,
            identity,
            "failed",
            f"connect failed: {exc}",
            resolved_port=resolved_port,
        )
        return

    _record_result(
        loop,
        preset_name,
        results,
        role,
        identity,
        "success",
        f"connected on {resolved_port}",
        resolved_port=resolved_port,
    )


def _disconnect_serial_role(loop: "ControlLoop", role: str, *, force: bool) -> None:
    if role == "arm":
        if loop.follower.connected:
            if not force:
                loop._park_relax_blocking()
            loop._release_follower("hardware preset")
        return
    loop.leader.disconnect()


def _apply_cameras(
    loop: "ControlLoop",
    preset_name: str,
    preset_cameras: dict[str, Any],
    results: list[dict[str, Any]],
) -> None:
    current = loop.cameras.snapshots()
    listed_names: set[str] = set()

    for key, spec in preset_cameras.items():
        identity = spec.get("identity") if isinstance(spec, dict) else None
        settings = spec.get("settings") if isinstance(spec, dict) else None
        matched = match_camera(identity, current)
        if matched is None:
            _record_result(
                loop,
                preset_name,
                results,
                "camera",
                identity,
                "skipped",
                f"preset entry {key} not found",
            )
            continue
        name = str(matched.get("name") or "")
        listed_names.add(name)
        try:
            detail = _apply_camera_settings(loop, matched, settings if isinstance(settings, dict) else {})
        except Exception as exc:  # noqa: BLE001
            _record_result(
                loop,
                preset_name,
                results,
                "camera",
                identity,
                "failed",
                f"{name}: {exc}",
            )
            continue
        _record_result(
            loop,
            preset_name,
            results,
            "camera",
            identity,
            "success",
            f"{name}: {detail}",
        )

    for snapshot in current:
        name = str(snapshot.get("name") or "")
        if not name or name in listed_names:
            continue
        identity = camera_identity(snapshot)
        try:
            detail = _disable_camera(loop, snapshot)
        except Exception as exc:  # noqa: BLE001
            _record_result(
                loop,
                preset_name,
                results,
                "camera",
                identity,
                "failed",
                f"{name}: disable failed: {exc}",
            )
            continue
        _record_result(
            loop,
            preset_name,
            results,
            "camera",
            identity,
            "success",
            f"{name}: {detail}",
        )


def _apply_camera_settings(
    loop: "ControlLoop",
    snapshot: dict[str, Any],
    settings: dict[str, Any],
) -> str:
    name = str(snapshot["name"])
    remote = bool(snapshot.get("remote"))
    label = settings.get("label")
    if label is not None:
        loop.cameras.set_label(name, str(label))

    if not remote:
        width = settings.get("width")
        height = settings.get("height")
        if width is not None and height is not None:
            loop.cameras.set_resolution(name, int(width), int(height))
        autofocus = settings.get("autofocus")
        focus = settings.get("focus")
        if autofocus is not None or focus is not None:
            loop.cameras.set_focus(
                name,
                autofocus=None if autofocus is None else bool(autofocus),
                focus=None if focus is None else float(focus),
            )

    enabled = bool(settings.get("enabled", False))
    show_main = bool(settings.get("show_main", False))
    feed_robot = bool(settings.get("feed_robot", False))
    loop.cameras.set_flags(
        name,
        enabled=enabled,
        show_main=show_main,
        feed_robot=feed_robot,
    )

    stream_text = ""
    if not remote:
        stream_enabled = enabled and bool(settings.get("streaming", False))
        port = settings.get("port")
        loop.cameras.set_stream(
            name,
            stream_enabled,
            None if port is None else int(port),
        )
        stream_text = f", stream={'on' if stream_enabled else 'off'}"
        if port is not None:
            stream_text += f"@{int(port)}"
    return (
        f"label={label or snapshot.get('label') or name}, "
        f"enabled={str(enabled).lower()}, "
        f"main={str(show_main).lower()}, "
        f"robot={str(feed_robot).lower()}{stream_text}"
    )


def _disable_camera(loop: "ControlLoop", snapshot: dict[str, Any]) -> str:
    name = str(snapshot["name"])
    loop.cameras.set_flags(
        name,
        enabled=False,
        show_main=False,
        feed_robot=False,
    )
    if not snapshot.get("remote"):
        loop.cameras.set_stream(name, False)
    return "disabled"


def _record_result(
    loop: "ControlLoop",
    preset_name: str,
    results: list[dict[str, Any]],
    role: str,
    identity: Any,
    status: str,
    detail: str,
    *,
    resolved_port: str = "",
) -> None:
    identity_text = _identity_text(identity)
    result = {
        "role": role,
        "identity": dict(identity) if isinstance(identity, dict) else {},
        "status": status,
        "detail": detail,
        "resolved_port": resolved_port,
    }
    results.append(result)
    message = f'hardware preset "{preset_name}": {role} {identity_text} {detail}'
    loop.log("error" if status == "failed" else "info", message)


def _identity_text(identity: Any) -> str:
    if not isinstance(identity, dict) or not identity:
        return "(none)"
    kind = str(identity.get("kind") or "")
    if kind == "virtual":
        return f"role={identity.get('role') or '?'} robot_id={identity.get('robot_id') or '?'}"
    if kind == "serial":
        return f"hwid={identity.get('hwid') or '?'}"
    if kind == "remote":
        return (
            f"robot_id={identity.get('robot_id') or '?'} "
            f"camera_id={identity.get('camera_id') or '?'}"
        )
    if kind == "local":
        return f"local index={identity.get('index')} name={identity.get('name')}"
    return str(identity)
