from lerobot_monitor.ports import _likely, list_serial_ports


def test_likely_ch340() -> None:
    assert _likely("USB-SERIAL CH340 (COM6)", "USB VID:PID=1A86:7523")
    assert _likely("USB-Enhanced-SERIAL CH343 (COM5)", "USB VID:PID=1A86:55D3", "wch.cn")
    assert _likely("CP2102 USB to UART Bridge", "")
    assert not _likely("Bluetooth Link", "BTHENUM")


def test_list_serial_ports_returns_list() -> None:
    ports = list_serial_ports()
    assert isinstance(ports, list)
    for row in ports:
        assert "port" in row
        assert "likely" in row
        assert "description" in row
