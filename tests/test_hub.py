from unittest.mock import MagicMock

import pytest

from lerobot_monitor.hub import RuntimeHub


def _hub_without_runtime_setup() -> RuntimeHub:
    hub = object.__new__(RuntimeHub)
    hub.loop = MagicMock()
    hub.cameras = MagicMock()
    hub.policy_residency = MagicMock()
    return hub


def test_stop_always_stops_cameras_and_preserves_loop_error() -> None:
    hub = _hub_without_runtime_setup()
    loop_error = RuntimeError("control loop shutdown failed")
    hub.loop.stop.side_effect = loop_error

    with pytest.raises(RuntimeError) as raised:
        hub.stop()

    assert raised.value is loop_error
    hub.loop.stop.assert_called_once_with()
    hub.cameras.stop.assert_called_once_with()
    hub.policy_residency.close.assert_called_once_with()


def test_stop_keeps_loop_error_if_camera_shutdown_also_fails() -> None:
    hub = _hub_without_runtime_setup()
    loop_error = RuntimeError("control loop shutdown failed")
    hub.loop.stop.side_effect = loop_error
    hub.cameras.stop.side_effect = OSError("camera shutdown failed")

    with pytest.raises(RuntimeError) as raised:
        hub.stop()

    assert raised.value is loop_error
    assert any("camera shutdown also failed" in note for note in loop_error.__notes__)
    hub.cameras.stop.assert_called_once_with()
    hub.policy_residency.close.assert_called_once_with()


def test_hardware_restore_queues_only_explicit_active_preset() -> None:
    hub = _hub_without_runtime_setup()
    hub.store = MagicMock()

    hub.store.ui.return_value = {}
    hub._restore_active_hardware_preset()
    hub.loop.submit_nowait.assert_not_called()
    hub.loop.log.assert_called_once()

    preset = {"schema": 1, "system": True, "devices": {}, "cameras": {}}
    hub.store.ui.return_value = {"active_hardware_preset": "Disconnected"}
    hub.store.presets.return_value = {"Disconnected": preset}
    hub._restore_active_hardware_preset()
    hub.loop.submit_nowait.assert_called_once_with(
        "hardware_apply",
        {"name": "Disconnected", "preset": preset},
    )
