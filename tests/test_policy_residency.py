"""Concurrency and lifecycle checks for the process-local model cache."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import lerobot_monitor.policy_residency as residency_module
from lerobot_monitor.policy import apply_policy_overrides
from lerobot_monitor.policy_residency import (
    PolicyBusyError,
    PolicyResidencyManager,
    policy_identity,
)


def _model(path: Path) -> Path:
    path.mkdir()
    (path / "config.json").write_text(
        json.dumps({"type": "act", "n_action_steps": 50, "fps": 30}), encoding="utf-8"
    )
    return path


def _loaded(path: str) -> Any:
    return SimpleNamespace(path=path, policy=None, preprocessor=None, postprocessor=None, task="")


def _manager(loader: Any) -> PolicyResidencyManager:
    return PolicyResidencyManager(loader, robot_type="so101_follower", rename_map={})


def _wait_for_state(manager: PolicyResidencyManager, path: Path, state: str) -> None:
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if manager.status(str(path))["state"] == state:
            return
        time.sleep(0.01)
    raise AssertionError(f"model did not reach {state}: {manager.status(str(path))}")


def test_runtime_overrides_and_path_alias_share_identity(tmp_path: Path) -> None:
    path = _model(tmp_path / "model")
    common = {"inference.type": "rtc", "fps": "15", "robot.use_degrees": "true"}
    original = policy_identity(str(path), "CUDA", {}, robot_type="so101_follower", rename_map={})
    alias = policy_identity(str(path / ".." / "model"), "cuda", common, robot_type="so101_follower", rename_map={})
    changed = policy_identity(
        str(path), "cuda", {**common, "policy.n_action_steps": "16"},
        robot_type="so101_follower", rename_map={},
    )
    assert original == alias
    assert changed != original


def test_repo_id_and_resolved_snapshot_share_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _model(tmp_path / "snapshot")
    monkeypatch.setattr(
        residency_module,
        "resolve_cached_policy_path",
        lambda path, revision="": str(snapshot) if path == "owner/model" and revision == "v2" else None,
    )
    remote = policy_identity(
        "owner/model", "cuda", {"policy.pretrained_revision": "v2"},
        robot_type="so101_follower", rename_map={},
    )
    local = policy_identity(str(snapshot), "cuda", {}, robot_type="so101_follower", rename_map={})
    assert remote == local


def test_runtime_fields_never_override_model_configuration() -> None:
    config = SimpleNamespace(fps=30, interpolation_multiplier=1, n_action_steps=50)
    applied = apply_policy_overrides(
        config,
        {"fps": "15", "interpolation_multiplier": "2", "inference.type": "rtc", "policy.n_action_steps": "16"},
    )
    assert applied == ["policy.n_action_steps"]
    assert (config.fps, config.interpolation_multiplier, config.n_action_steps) == (30, 1, 16)


def test_load_is_coalesced_and_ready_model_skips_other_cold_load(tmp_path: Path) -> None:
    first = _model(tmp_path / "first")
    second = _model(tmp_path / "second")
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def loader(path: str, **kwargs: Any) -> Any:
        calls.append(path)
        kwargs["progress"]("weights", 1)
        if path == str(second):
            entered.set()
            assert release.wait(3)
        return _loaded(path)

    manager = _manager(loader)
    try:
        manager.request(str(first), "cpu", {})
        _wait_for_state(manager, first, "ready")
        manager.request(str(second), "cpu", {})
        manager.request(str(second), "cpu", {})
        assert entered.wait(2)
        before = time.perf_counter()
        lease = manager.acquire_ready(str(first), "cpu", {})
        assert lease is not None and lease.loaded is not None
        assert time.perf_counter() - before < 0.1
        lease.release()
        assert manager.status(str(second))["state"] == "loading"
        release.set()
        _wait_for_state(manager, second, "ready")
        assert calls.count(str(second)) == 1
    finally:
        release.set()
        manager.close()


def test_busy_and_stopping_instances_cannot_unload(tmp_path: Path) -> None:
    path = _model(tmp_path / "model")
    manager = _manager(lambda path, **kwargs: _loaded(path))
    try:
        lease = manager.acquire(str(path), "cpu", {})
        with pytest.raises(PolicyBusyError):
            manager.unload(str(path))
        stopped = threading.Event()
        lease.retire(stopped)
        assert manager.status(str(path))["state"] == "stopping"
        with pytest.raises(PolicyBusyError):
            manager.unload(str(path))
        stopped.set()
        _wait_for_state(manager, path, "ready")
        assert manager.unload(str(path)) == 1
        _wait_for_state(manager, path, "unloaded")
    finally:
        manager.close()


def test_clean_invalidates_late_load_result(tmp_path: Path) -> None:
    path = _model(tmp_path / "model")
    entered = threading.Event()
    release = threading.Event()

    def loader(source: str, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(3)
        return _loaded(source)

    manager = _manager(loader)
    try:
        manager.request(str(path), "cpu", {})
        assert entered.wait(2)
        assert manager.clear_all() == 1
        release.set()
        time.sleep(0.1)
        assert manager.status(str(path))["state"] == "unloaded"
    finally:
        release.set()
        manager.close()


def test_failed_load_can_retry_without_evicting_other_model(tmp_path: Path) -> None:
    bad = _model(tmp_path / "bad")
    good = _model(tmp_path / "good")
    attempts = 0

    def loader(source: str, **kwargs: Any) -> Any:
        nonlocal attempts
        if source == str(bad):
            attempts += 1
            if attempts == 1:
                raise RuntimeError("out of memory")
        return _loaded(source)

    manager = _manager(loader)
    try:
        manager.request(str(good), "cpu", {})
        _wait_for_state(manager, good, "ready")
        manager.request(str(bad), "cpu", {})
        _wait_for_state(manager, bad, "error")
        assert "out of memory" in manager.status(str(bad))["instances"][0]["error"]
        assert manager.status(str(good))["state"] == "ready"
        manager.request(str(bad), "cpu", {})
        _wait_for_state(manager, bad, "ready")
        assert attempts == 2
    finally:
        manager.close()


def test_rtc_stop_does_not_forget_a_thread_that_is_still_running(monkeypatch: pytest.MonkeyPatch) -> None:
    from lerobot_monitor.pathutil import ensure_lerobot_on_path

    ensure_lerobot_on_path()
    from lerobot.rollout.inference import rtc

    monkeypatch.setattr(rtc, "_RTC_JOIN_TIMEOUT_S", 0.01)
    release = threading.Event()
    worker = threading.Thread(target=lambda: release.wait(2), daemon=True)
    worker.start()
    engine = rtc.RTCInferenceEngine.__new__(rtc.RTCInferenceEngine)
    engine._shutdown_event = threading.Event()
    engine._policy_active = threading.Event()
    engine._rtc_thread = worker
    try:
        assert engine.stop() is False
        assert engine._rtc_thread is worker
        release.set()
        assert engine.stop() is True
        assert engine._rtc_thread is None
    finally:
        release.set()
        worker.join(timeout=2)
