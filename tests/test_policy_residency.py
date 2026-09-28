"""Concurrency and lifecycle checks for the process-local model cache."""

from __future__ import annotations

import json
import logging
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
    # Nothing on the command line can change the tensors a checkpoint holds, so an
    # override must reuse the resident instance instead of loading a second copy.
    assert changed == original


def test_policy_overrides_never_split_the_weights_identity(tmp_path: Path) -> None:
    path = tmp_path / "implicit-defaults"
    path.mkdir()
    (path / "config.json").write_text('{"type": "act", "fps": 30}', encoding="utf-8")

    original = policy_identity(str(path), "cuda", {}, robot_type="so101_follower", rename_map={})
    action_steps = policy_identity(
        str(path),
        "cuda",
        {"policy.n_action_steps": "16"},
        robot_type="so101_follower",
        rename_map={},
    )
    ensemble = policy_identity(
        str(path),
        "cuda",
        {"policy.temporal_ensemble_coeff": "0.01"},
        robot_type="so101_follower",
        rename_map={},
    )
    other_device = policy_identity(str(path), "cuda:1", {}, robot_type="so101_follower", rename_map={})
    other_robot = policy_identity(str(path), "cuda", {}, robot_type="so100_follower", rename_map={})

    assert action_steps == original
    assert ensemble == original
    assert other_device != original
    assert other_robot != original


def test_device_spellings_share_one_entry(tmp_path: Path) -> None:
    path = _model(tmp_path / "model")
    canonical = policy_identity(str(path), "cuda", {}, robot_type="so101_follower", rename_map={})

    for spelling in ("CUDA", " cuda ", "cuda:0"):
        assert (
            policy_identity(str(path), spelling, {}, robot_type="so101_follower", rename_map={})
            == canonical
        )
    assert (
        policy_identity(str(path), "cuda:1", {}, robot_type="so101_follower", rename_map={})
        != canonical
    )


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


def test_load_status_reports_imports_and_stage_timings(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    path = _model(tmp_path / "model")
    entered = threading.Event()
    release = threading.Event()

    def loader(source: str, **kwargs: Any) -> Any:
        kwargs["progress"]("imports", 0)
        entered.set()
        assert release.wait(3)
        kwargs["progress"]("weights", 1)
        return _loaded(source)

    manager = _manager(loader)
    try:
        manager.request(str(path), "cpu", {})
        assert entered.wait(2)
        assert "START imports" in caplog.text
        assert "queued cold load" in caplog.text
        assert "model ready" not in caplog.text
        active = manager.status(str(path))["instances"][0]
        assert active["phase"] == "imports"
        assert active["completed_steps"] == 0
        assert active["elapsed_ms"] >= active["phase_elapsed_ms"] >= 0
        release.set()
        _wait_for_state(manager, path, "ready")
        ready = manager.status(str(path))["instances"][0]
        assert ready["completed_steps"] == 4
        assert ready["phase_elapsed_ms"] is None
        assert ready["elapsed_ms"] == ready["load_ms"]
        assert ready["stage_durations_ms"]["imports"] >= active["phase_elapsed_ms"]
        assert ready["stage_durations_ms"]["weights"] >= 0
        assert ready["stage_durations_ms"]["finalizing"] >= 0
    finally:
        release.set()
        manager.close()


def test_loading_logs_never_hold_residency_lock(tmp_path: Path) -> None:
    """A UI handler may need another thread to read status before it can finish."""
    path = _model(tmp_path / "model")
    manager = _manager(lambda source, **kwargs: (kwargs["progress"]("weights", 1), _loaded(source))[1])
    blocked: list[str] = []

    class StatusHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            finished = threading.Event()

            def read_status() -> None:
                manager.status(str(path))
                finished.set()

            thread = threading.Thread(target=read_status, daemon=True)
            thread.start()
            if not finished.wait(1):
                blocked.append(record.getMessage())

    handler = StatusHandler()
    old_level = residency_module.logger.level
    residency_module.logger.setLevel(logging.INFO)
    residency_module.logger.addHandler(handler)
    try:
        manager.acquire(str(path), "cpu", {}).release()
        manager.acquire_ready(str(path), "cpu", {}).release()
        assert not blocked
    finally:
        residency_module.logger.removeHandler(handler)
        residency_module.logger.setLevel(old_level)
        manager.close()


def test_failed_stage_is_logged_with_duration_and_no_ready(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    path = _model(tmp_path / "model")

    def loader(source: str, **kwargs: Any) -> Any:
        kwargs["progress"]("processors", 3)
        raise ValueError("tokenizer files missing")

    manager = _manager(loader)
    try:
        with pytest.raises(RuntimeError, match="tokenizer files missing"):
            manager.acquire(str(path), "cpu", {})
        assert "FAILED processors after" in caplog.text
        assert "device=cpu" in caplog.text
        assert "model ready" not in caplog.text
        assert manager.status(str(path))["instances"][0]["stage_durations_ms"]["processors"] >= 0
    finally:
        manager.close()


def test_owner_wait_and_cache_reuse_are_logged_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    path = _model(tmp_path / "model")
    manager = _manager(lambda source, **kwargs: _loaded(source))
    first = manager.acquire(str(path), "cpu", {})
    acquired = threading.Event()

    def wait_for_owner() -> None:
        lease = manager.acquire(str(path), "cpu", {})
        lease.release()
        acquired.set()

    waiter = threading.Thread(target=wait_for_owner, daemon=True)
    waiter.start()
    try:
        deadline = time.monotonic() + 3
        while "waiting for previous inference owner" not in caplog.text and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "waiting for previous inference owner" in caplog.text
        assert not acquired.is_set()
        first.release()
        assert acquired.wait(3)
        assert "resident cache hit" in caplog.text
        assert caplog.text.count("waiting for previous inference owner") == 1
    finally:
        first.release()
        waiter.join(timeout=3)
        manager.close()


def test_load_diagnostics_samples_blocked_worker_without_waiting(tmp_path: Path) -> None:
    path = _model(tmp_path / "model")
    entered = threading.Event()
    release = threading.Event()

    def blocked_loader(source: str, **kwargs: Any) -> Any:
        kwargs["progress"]("imports_factory", 0)
        entered.set()
        assert release.wait(3)
        return _loaded(source)

    manager = _manager(blocked_loader)
    try:
        manager.request(str(path), "cpu", {})
        assert entered.wait(2)
        diagnostic = manager.load_diagnostics()
        worker = next(row for row in diagnostic["threads"] if row["id"] == diagnostic["worker_thread_id"])
        assert worker["name"] == "policy-residency-load"
        assert any("in blocked_loader" in line for line in worker["stack"])
        assert 0 < len(worker["stack"]) <= 48
        # The public response consists only of JSON data, never live frames/locals.
        json.dumps(diagnostic)
        assert manager.status(str(path))["instances"][0]["phase"] == "imports_factory"
    finally:
        release.set()
        _wait_for_state(manager, path, "ready")
        manager.close()


def test_busy_and_stopping_instances_cannot_unload(tmp_path: Path) -> None:
    path = _model(tmp_path / "model")
    manager = _manager(lambda path, **kwargs: _loaded(path))
    try:
        lease = manager.acquire(str(path), "cpu", {})
        assert not manager.has_stopping_inference()
        with pytest.raises(PolicyBusyError):
            manager.unload(str(path))
        stopped = threading.Event()
        lease.retire(stopped)
        assert manager.has_stopping_inference()
        assert manager.status(str(path))["state"] == "stopping"
        with pytest.raises(PolicyBusyError):
            manager.unload(str(path))
        stopped.set()
        _wait_for_state(manager, path, "ready")
        assert not manager.has_stopping_inference()
        assert manager.unload(str(path)) == 1
        assert manager.unload(str(path)) in {0, 1}
        _wait_for_state(manager, path, "unloaded")
        assert manager.unload(str(path)) == 0
    finally:
        manager.close()


def test_status_refresh_uses_resident_keys_without_filesystem_io(tmp_path, monkeypatch) -> None:
    path = _model(tmp_path / "model")
    manager = _manager(lambda path, **kwargs: _loaded(path))
    try:
        manager.request(str(path), "cpu", {})
        _wait_for_state(manager, path, "ready")
        source = str(path.resolve())
        expected = manager.status(str(path))

        def unexpected_io(*args, **kwargs):
            raise AssertionError("status refresh must not resolve or stat resident paths")

        with monkeypatch.context() as patch:
            patch.setattr(residency_module, "_canonical_source", unexpected_io)
            patch.setattr(Path, "stat", unexpected_io)
            actual = manager.all_statuses()
            assert actual == {source: expected}
            assert not manager.has_stopping_inference()
            actual[source]["instances"][0]["stage_durations_ms"]["modified"] = 1
            actual[source]["instances"][0]["overrides"]["modified"] = "value"
            assert manager.all_statuses() == {source: expected}
    finally:
        manager.close()


def test_load_stage_remembers_new_snapshot_alias_for_status_pushes(tmp_path, monkeypatch) -> None:
    snapshot = _model(tmp_path / "snapshot")
    resolved = []
    entered, finish = threading.Event(), threading.Event()
    monkeypatch.setattr(
        residency_module, "resolve_cached_policy_path",
        lambda path, *args: resolved[0] if path == "owner/model" and resolved else None,
    )

    def loader(source, **kwargs):
        resolved.append(str(snapshot))
        kwargs["progress"]("weights", 1)
        entered.set()
        assert finish.wait(4)
        return _loaded(source)

    manager = _manager(loader)
    try:
        manager.request("owner/model", "cpu", {})
        assert entered.wait(2)
        with monkeypatch.context() as patch:
            def unexpected_io(*args, **kwargs):
                raise AssertionError("status push re-resolved the downloaded snapshot")

            patch.setattr(residency_module, "_canonical_source", unexpected_io)
            status = manager.all_statuses()["owner/model"]
            assert status["state"] == "loading"
            assert status["source_paths"] == sorted(["owner/model", str(snapshot)])
    finally:
        finish.set()
        manager.close()


def test_status_push_keeps_devices_grouped_during_snapshot_rekey(tmp_path, monkeypatch) -> None:
    snapshot = _model(tmp_path / "snapshot")
    resolved = []
    first_started, finish_first = threading.Event(), threading.Event()
    second_started, finish_second = threading.Event(), threading.Event()
    monkeypatch.setattr(
        residency_module, "resolve_cached_policy_path",
        lambda path, *args: resolved[0] if path == "owner/model" and resolved else None,
    )

    def loader(source, **kwargs):
        if kwargs["device"] == "cpu":
            first_started.set()
            assert finish_first.wait(4)
        else:
            kwargs["progress"]("weights", 1)
            second_started.set()
            assert finish_second.wait(4)
        return _loaded(source)

    manager = _manager(loader)
    try:
        manager.request("owner/model", "cpu", {})
        assert first_started.wait(2)
        manager.request("owner/model", "cuda", {})
        resolved.append(str(snapshot))
        finish_first.set()
        assert second_started.wait(2)
        with monkeypatch.context() as patch:
            def unexpected_io(*args, **kwargs):
                raise AssertionError("status aggregation must use cached aliases")

            patch.setattr(residency_module, "_canonical_source", unexpected_io)
            statuses = manager.all_statuses()
            assert set(statuses) == {"owner/model", str(snapshot)}
            for status in statuses.values():
                assert status["state"] == "loading"
                assert {item["state"] for item in status["instances"]} == {"ready", "loading"}
                assert {item["device"] for item in status["instances"]} == {"cpu", "cuda"}
    finally:
        finish_first.set()
        finish_second.set()
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
    engine._stop_signal_logged = False
    engine._thread_exit_logged = False
    try:
        assert engine.stop() is False
        assert engine._rtc_thread is worker
        assert engine._shutdown_event.is_set()
        assert engine.wait_stopped(timeout=0.01) is False
        assert engine._rtc_thread is worker
        release.set()
        assert engine.wait_stopped(timeout=2) is True
        assert engine._rtc_thread is None
        assert engine.stop() is True
    finally:
        release.set()
        worker.join(timeout=2)


def test_engine_stop_logs_one_transition_per_lifecycle(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from lerobot_monitor.pathutil import ensure_lerobot_on_path

    ensure_lerobot_on_path()
    from lerobot.rollout.inference import rtc

    monkeypatch.setattr(rtc, "_RTC_JOIN_TIMEOUT_S", 0.01)
    release = threading.Event()
    worker = threading.Thread(target=lambda: release.wait(5), daemon=True)
    worker.start()
    engine = rtc.RTCInferenceEngine.__new__(rtc.RTCInferenceEngine)
    engine._shutdown_event = threading.Event()
    engine._policy_active = threading.Event()
    engine._rtc_thread = worker
    engine._stop_signal_logged = False
    engine._thread_exit_logged = False
    try:
        with caplog.at_level(logging.INFO, logger=rtc.__name__):
            assert engine.stop() is False
            assert engine.stop() is False
            assert engine.wait_stopped(timeout=0.01) is False
            release.set()
            assert engine.stop() is True
            assert engine.stop() is True
        messages = [record.getMessage() for record in caplog.records]
        assert messages.count("Stopping RTC inference thread...") == 1
        assert messages.count("RTC inference thread stopped") == 1
        assert sum("still finishing an inference" in message for message in messages) == 1
        assert not any("did not join" in message for message in messages)
    finally:
        release.set()
        worker.join(timeout=2)


def test_acquire_waits_out_a_cold_load_without_a_bound(tmp_path: Path) -> None:
    path = _model(tmp_path / "model")
    release = threading.Event()
    entered = threading.Event()

    def loader(source: str, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return _loaded(source)

    manager = _manager(loader)
    try:
        manager.request(str(path), "cpu", {})
        assert entered.wait(2)
        result: list[Any] = []

        def worker() -> None:
            result.append(manager.acquire(str(path), "cpu", {}, timeout=0.2))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        time.sleep(0.6)
        # A cold load is legitimate work, so the stopping-state bound must not cut it off.
        assert result == []
        release.set()
        thread.join(timeout=5)
        assert result and result[0].loaded is not None
        result[0].release()
    finally:
        release.set()
        manager.close()


def test_acquire_times_out_on_a_stopping_entry(tmp_path: Path) -> None:
    path = _model(tmp_path / "model")
    manager = _manager(lambda source, **kwargs: _loaded(source))
    try:
        lease = manager.acquire(str(path), "cpu", {})
        stopped = threading.Event()
        lease.retire(stopped)

        with pytest.raises(PolicyBusyError, match=r"not ready after 0\.2s"):
            manager.acquire(str(path), "cpu", {}, timeout=0.2)

        stopped.set()
        _wait_for_state(manager, path, "ready")
        retry = manager.acquire(str(path), "cpu", {}, timeout=2)
        assert retry.cache_hit is True
        assert retry.loaded is not None
        retry.release()
    finally:
        manager.close()


@pytest.mark.parametrize("checkpoint", [False, True])
def test_cancel_loading_wakes_waiters_and_discards_late_model(tmp_path: Path, checkpoint: bool) -> None:
    path = _model(tmp_path / "cancel")
    entered, finish = threading.Event(), threading.Event()
    later: list[str] = []
    errors: list[Exception] = []

    def loader(source: str, **kwargs: Any) -> Any:
        entered.set()
        assert finish.wait(4)
        if checkpoint:
            kwargs["progress"]("processors", 2)
            later.append("processors")
        return _loaded(source)

    manager = _manager(loader)
    manager.request(str(path), "cpu", {})
    assert entered.wait(2)

    def acquire() -> None:
        try:
            manager.acquire(str(path), "cpu", {})
        except Exception as exc:
            errors.append(exc)

    waiter = threading.Thread(target=acquire)
    waiter.start()
    time.sleep(0.03)
    try:
        assert manager.cancel_load(str(path), "stale") == 0
        assert manager.cancel_load(str(path)) == 1
        assert manager.status(str(path))["state"] == "cancelling"
        assert manager.cancel_load(str(path)) == 0
        waiter.join(1)
        assert len(errors) == 1 and not waiter.is_alive()
        with pytest.raises(PolicyBusyError):
            manager.request(str(path), "cpu", {})
        finish.set()
        _wait_for_state(manager, path, "cancelled")
        assert manager.ready(str(path), "cpu", {}) is None
        assert later == []
    finally:
        finish.set()
        manager.close()


def test_queued_cancel_retry_does_not_revive_old_queue_job(tmp_path: Path) -> None:
    first, second = _model(tmp_path / "first"), _model(tmp_path / "second")
    entered, finish = threading.Event(), threading.Event()
    calls: list[str] = []

    def loader(source: str, **kwargs: Any) -> Any:
        calls.append(source)
        if source == str(first):
            entered.set()
            assert finish.wait(4)
        return _loaded(source)

    manager = _manager(loader)
    try:
        manager.request(str(first), "cpu", {})
        assert entered.wait(2)
        old = manager.request(str(second), "cpu", {})
        assert manager.cancel_load(str(second)) == 1
        assert manager.status(str(second))["state"] == "cancelled"
        new = manager.request(str(second), "cpu", {})
        assert new is not old
        finish.set()
        _wait_for_state(manager, second, "ready")
        assert calls == [str(first), str(second)]
    finally:
        finish.set()
        manager.close()


def test_release_before_callback_registration_remains_busy_until_owner_exits(tmp_path: Path) -> None:
    path = _model(tmp_path / "model")
    manager = _manager(lambda source, **kw: _loaded(source))
    lease = manager.acquire(str(path), "cpu", {})
    callbacks: list[bool] = []
    try:
        assert manager.release_owner(str(path), "stale") == 0
        assert manager.release_owner(str(path), lease.entry.instance_id) == 1
        assert lease.cancelled.is_set()
        assert manager.status(str(path))["state"] == "stopping"
        assert manager.acquire_ready(str(path), "cpu", {}) is None
        assert manager.release_owner(str(path)) == 0
        lease.set_stop_callback(lambda: callbacks.append(True))
        assert callbacks == [True]
        lease.release()
        assert manager.status(str(path))["state"] == "ready"
        assert manager.ready(str(path), "cpu", {}) is not None
    finally:
        lease.release()
        manager.close()


def test_release_cancels_existing_waiter_without_reacquiring(tmp_path: Path) -> None:
    path = _model(tmp_path / "model")
    manager = _manager(lambda source, **kw: _loaded(source))
    lease = manager.acquire(str(path), "cpu", {})
    errors: list[Exception] = []

    def acquire() -> None:
        try:
            manager.acquire(str(path), "cpu", {})
        except Exception as exc:
            errors.append(exc)

    waiter = threading.Thread(target=acquire)
    waiter.start()
    time.sleep(0.03)
    manager.release_owner(str(path))
    lease.release()
    waiter.join(2)
    try:
        assert len(errors) == 1 and not waiter.is_alive()
        assert manager.status(str(path))["state"] == "ready"
    finally:
        manager.close()


def test_cancel_finds_remote_alias_after_cache_appears(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _model(tmp_path / "snapshot")
    entered, finish = threading.Event(), threading.Event()
    resolved: list[str] = []
    monkeypatch.setattr(residency_module, "resolve_cached_policy_path", lambda path, *args: resolved[0] if path == "owner/model" and resolved else None)

    def loader(source: str, **kwargs: Any) -> Any:
        entered.set()
        assert finish.wait(4)
        return _loaded(source)

    manager = _manager(loader)
    try:
        entry = manager.request("owner/model", "cpu", {})
        assert entered.wait(2)
        resolved.append(str(snapshot))
        assert manager.status("owner/model")["state"] == "loading"
        assert manager.request("owner/model", "cpu", {}) is entry
        assert manager.status("owner/model")["source_paths"] == sorted(["owner/model", str(snapshot)])
        assert manager.cancel_load("owner/model", entry.instance_id) == 1
        with pytest.raises(PolicyBusyError):
            manager.request("owner/model", "cpu", {})
        finish.set()
        _wait_for_state(manager, snapshot, "cancelled")
    finally:
        finish.set()
        manager.close()


def test_release_during_request_log_cancels_original_acquire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _model(tmp_path / "model")
    manager = _manager(lambda source, **kw: _loaded(source))
    lease = manager.acquire(str(path), "cpu", {})
    entered, resume = threading.Event(), threading.Event()
    errors: list[Exception] = []
    original = residency_module.logger.info

    def log(message: str, *args: Any, **kwargs: Any) -> None:
        if threading.current_thread().name == "waiting-owner":
            entered.set()
            assert resume.wait(3)
        original(message, *args, **kwargs)

    monkeypatch.setattr(residency_module.logger, "info", log)

    def acquire() -> None:
        try:
            manager.acquire(str(path), "cpu", {})
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=acquire, name="waiting-owner")
    worker.start()
    try:
        assert entered.wait(2)
        assert manager.release_owner(str(path)) == 1
        lease.release()
        resume.set()
        worker.join(2)
        assert len(errors) == 1 and not worker.is_alive()
        assert manager.status(str(path))["state"] == "ready"
    finally:
        resume.set()
        lease.release()
        manager.close()
