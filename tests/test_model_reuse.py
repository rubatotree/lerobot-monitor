"""Regression coverage for loading once across Library, Rollout and Debug."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from lerobot_monitor import library
from lerobot_monitor import loop as loop_module
from lerobot_monitor.app import create_app
from lerobot_monitor.config import (
    CamerasConfig,
    LibraryConfig,
    MonitorConfig,
    RobotConfig,
    ServerConfig,
)
from lerobot_monitor.loop import _PolicyLoadJob
from lerobot_monitor.policy import ActionChunk, resolve_cached_policy_path
from lerobot_monitor.store import JsonStore


@pytest.fixture
def snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "current-hub"
    monkeypatch.setattr(library, "huggingface_hub_cache", lambda: cache)
    path = cache / "models--owner--policy" / "snapshots" / ("a" * 40)
    path.mkdir(parents=True)
    (path / "config.json").write_text(
        json.dumps(
            {
                "type": "act",
                "input_features": {
                    "observation.state": {"type": "STATE", "shape": [6]}
                },
                "output_features": {"action": {"type": "ACTION", "shape": [6]}},
            }
        ),
        encoding="utf-8",
    )
    (path / "model.safetensors").write_bytes(b"test weights")
    return path


def test_missing_snapshot_follows_cache_move_with_exact_commit(
    snapshot: Path, tmp_path: Path
) -> None:
    old = (
        tmp_path / "old-hub" / snapshot.parent.parent.name / "snapshots" / snapshot.name
    )
    assert not old.exists()
    assert resolve_cached_policy_path(str(old)) == str(snapshot.resolve())
    # Presets copied from Windows still work when the server runs on Linux.
    assert resolve_cached_policy_path(str(old).replace("/", "\\")) == str(
        snapshot.resolve()
    )
    assert resolve_cached_policy_path(str(old.with_name("b" * 40))) is None
    assert (
        resolve_cached_policy_path(str(old).replace("owner--policy", "owner--other"))
        is None
    )
    old.mkdir(parents=True)
    assert resolve_cached_policy_path(str(old)) == str(old.resolve())


def test_incomplete_relocated_snapshot_is_not_used(
    snapshot: Path, tmp_path: Path
) -> None:
    old = (
        tmp_path / "old-hub" / snapshot.parent.parent.name / "snapshots" / snapshot.name
    )
    (snapshot / "model.safetensors").unlink()
    assert resolve_cached_policy_path(str(old)) is None


def test_rollout_then_debug_reuses_model_after_cache_move(
    snapshot: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MonitorConfig(
        store_path=tmp_path / "store.json",
        server=ServerConfig(base_path="/lerobot"),
        robot=RobotConfig(auto_connect=False),
        cameras=CamerasConfig(probe=False),
        library=LibraryConfig(videos_root=tmp_path / "videos", models_roots=[]),
    )
    JsonStore(config.store_path).put_model(
        {"id": "owner/policy", "path": str(snapshot)}
    )
    app = create_app(config)
    hub = app.state.hub
    loaded = SimpleNamespace(
        path=str(snapshot), policy=None, preprocessor=None, postprocessor=None, task=""
    )
    loader = MagicMock(return_value=loaded)
    hub.policy_residency.loader = loader
    seen: list[Any] = []

    def predict(model: Any, *args: Any) -> ActionChunk:
        seen.append(model)
        return ActionChunk(
            actions=[{"gripper": 1.0}],
            strategy="policy_chunk",
            degraded=False,
            warnings=[],
        )

    monkeypatch.setattr(loop_module, "predict_action_chunk", predict)
    # Exercise the real inference/cache path without starting hardware or the bus loop.
    hub.loop.acquire_debug_lease = MagicMock(
        return_value={"ok": True, "token": "debug-test"}
    )
    hub.loop.release_debug_lease = MagicMock()
    old = (
        tmp_path / "old-hub" / snapshot.parent.parent.name / "snapshots" / snapshot.name
    )
    try:
        job = _PolicyLoadJob(generation=0, payload={})
        hub.loop._policy_worker(
            job, str(snapshot), "cuda", "rollout task", {"inference.type": "rtc"}
        )
        assert job.error is None and job.lease is not None
        assert job.result is loaded
        job.lease.release()
        with TestClient(app) as client:
            # A legacy client cannot inject rollout overrides into a Library load.
            response = client.post(
                "/lerobot/api/models/owner/policy/load",
                json={
                    "extra": {"policy.n_action_steps": "16"},
                },
            )
            assert response.status_code == 202, response.text
            response = client.post(
                "/lerobot/api/debug/infer",
                json={
                    "policy_path": str(old),
                    "task": "debug task",
                    "device": "cuda:0",
                    "extra": {},
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["cache_hit"] is True
            assert response.json()["model_load_ms"] == 0.0
            assert seen == [loaded]
            assert loaded.task == "debug task"
            loader.assert_called_once()
            assert len(hub.policy_residency.status(str(old))["instances"]) == 1
            assert hub.policy_residency.request(str(snapshot), "cuda", {}).extra == {
                "inference.type": "rtc"
            }
    finally:
        hub.policy_residency.close()
