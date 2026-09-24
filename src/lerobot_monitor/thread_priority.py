"""Best-effort Windows thread scheduling for robot control and recording."""

from __future__ import annotations

import ctypes
import os


def set_current_thread_priority(priority: int) -> None:
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), int(priority))
    except (AttributeError, OSError):
        pass
