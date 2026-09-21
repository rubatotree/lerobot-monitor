from unittest.mock import MagicMock

import pytest

from lerobot_monitor.hub import RuntimeHub


def _hub_without_runtime_setup() -> RuntimeHub:
    hub = object.__new__(RuntimeHub)
    hub.loop = MagicMock()
    hub.cameras = MagicMock()
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
