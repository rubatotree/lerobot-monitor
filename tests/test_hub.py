from unittest.mock import MagicMock

import pytest

from lerobot_monitor.hub import RuntimeHub
from lerobot_monitor.config import MonitorConfig


def _hub_without_runtime_setup() -> RuntimeHub:
    hub = object.__new__(RuntimeHub)
    hub.loop = MagicMock()
    hub.cameras = MagicMock()
    hub.policy_residency = MagicMock()
    return hub


@pytest.mark.parametrize("enabled,available", [(True, True), (False, True), (True, False)])
def test_policy_imports_precede_runtime_threads(monkeypatch, enabled, available) -> None:
    hub = _hub_without_runtime_setup()
    hub.config = MonitorConfig()
    hub.config.rollout.preload_dependencies = enabled
    hub.runtime = {"torch": "test" if available else None, "lerobot_file": "test"}
    events = []

    def prepare(log):
        events.append("imports")
        return {"state": "ready"}

    monkeypatch.setattr("lerobot_monitor.hub.prepare_policy_runtime", prepare)
    hub.cameras.start.side_effect = lambda: events.append("cameras")
    hub.loop.start.side_effect = lambda: events.append("control")
    hub._restore_active_hardware_preset = lambda: events.append("restore")
    hub.start()
    assert events == (["imports"] if enabled and available else []) + ["cameras", "control", "restore"]
    assert hub.runtime["policy_dependencies"]["state"] == (
        "disabled" if not enabled else "ready" if available else "unavailable"
    )


def test_optional_policy_import_failure_still_starts_monitor(monkeypatch) -> None:
    hub = _hub_without_runtime_setup()
    hub.config = MonitorConfig()
    hub.runtime = {"torch": "test", "lerobot_file": "test"}
    hub._restore_active_hardware_preset = MagicMock()

    def fail(progress):
        progress("imports_factory", 0)
        raise ImportError("missing optional policy dependency")

    monkeypatch.setattr("lerobot_monitor.policy.import_policy_dependencies", fail)
    hub.start()
    assert hub.runtime["policy_dependencies"]["state"] == "error"
    assert hub.runtime["policy_dependencies"]["phase"] == "imports_factory"
    hub.cameras.start.assert_called_once()
    hub.loop.start.assert_called_once()


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
