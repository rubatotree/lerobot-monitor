from lerobot_monitor.cameras import DeviceCamera


def test_focus_is_latched_without_opening_device() -> None:
    cam = DeviceCamera(0, width=640, height=480, jpeg_quality=80, port=5000)
    cam.set_focus(autofocus=False, focus=40)
    snap = cam.snapshot()
    assert snap["autofocus"] is False
    assert snap["focus"] == 40.0
    assert cam._focus_dirty.is_set()
    cam.set_focus(focus=300)
    assert cam.focus == 255.0
