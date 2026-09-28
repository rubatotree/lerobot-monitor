from __future__ import annotations

import argparse
import base64
import importlib.util
import math
import struct
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def smoke() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke-cloud-models.py"
    specification = importlib.util.spec_from_file_location("cloud_smoke", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_black_observation_png_dimensions(smoke: ModuleType) -> None:
    image = base64.b64decode(smoke.black_png())
    assert image[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", image[16:24]) == (640, 480)
    assert image[24:26] == bytes([8, 2])  # RGB uint8 [480,640,3].


def test_smoke_action_validation_rejects_nan_and_mismatched_shape(smoke: ModuleType) -> None:
    valid = {"actions": [[0.0, 1.0]], "raw_actions": [[0.0, 1.0]], "shape": [1, 2]}
    assert smoke.verify_actions(valid, 2, "select_action")["finite"]
    with pytest.raises(AssertionError):
        smoke.verify_actions({**valid, "actions": [[math.nan, 1.0]]}, 2, "select_action")
    with pytest.raises(AssertionError):
        smoke.verify_actions({**valid, "shape": [2, 2]}, 2, "select_action")


def test_smoke_failure_still_requests_unload(smoke: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    class Client:
        def __init__(self) -> None:
            self.client = self
            self.calls: list[tuple[str, str]] = []

        def request(self, method: str, path: str, body: Any = None) -> Any:
            self.calls.append((method, path))
            if path == "/deployments":
                return [{"id": "model", "name": "model", "status": "ready"}]
            return {"job_id": "job"}

        def wait_job(self, operation: Any, timeout: float) -> dict[str, str]:
            return {"status": "succeeded"}

        def deployment(self, identifier: str) -> dict[str, Any]:
            return {"metadata": {"capabilities": {"select_action": True}}}

        def close(self) -> None:
            pass

    client = Client()
    monkeypatch.setattr(smoke, "SmokeClient", lambda *args: client)

    def fail(*args: Any) -> None:
        raise RuntimeError("simulated inference failure")

    monkeypatch.setattr(smoke, "exercise_mode", fail)
    arguments = argparse.Namespace(manager_url="http://127.0.0.1:8095", host="8x4090-server",
                                   timeout=120, deployment_id="model", model=None, gpu="GPU-1", load_timeout=600,
                                   task="test")
    report = smoke.run_smoke(arguments)
    assert report["status"] == "failed"
    assert "simulated inference failure" in report["error"]
    assert ("POST", "/deployments/model/unload") in client.calls
