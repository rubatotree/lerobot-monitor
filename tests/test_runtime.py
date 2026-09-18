from lerobot_monitor.runtime import format_runtime, probe_runtime


def test_probe_runtime_has_python() -> None:
    info = probe_runtime()
    assert "python" in info
    assert info["version"]
    label = format_runtime(info)
    assert info["version"] in label
