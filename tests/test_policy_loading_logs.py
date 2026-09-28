"""Loading diagnostics remain useful before expensive stages finish."""

from __future__ import annotations

import contextlib
import logging
import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

from lerobot_monitor import policy


def test_direct_load_logs_phase_before_work_and_completion(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    phases: list[str] = []
    bundle = SimpleNamespace(path="owner/model")

    def load(source: str, **kwargs: Any) -> Any:
        for phase in (
            "imports_torch",
            "cache",
            "config",
            "weights",
            "device",
            "processors",
        ):
            kwargs["progress"](phase, 0)
            assert f"START {phase}" in caplog.text
            assert "bundle ready" not in caplog.text
        return bundle

    monkeypatch.setattr(policy, "_load_policy", load)
    result = policy.load_policy(
        "owner/model", device="cpu", progress=lambda phase, _: phases.append(phase)
    )
    assert result is bundle
    assert phases[-1] == "processors"
    assert "source=owner/model device=cpu" in caplog.text
    assert "DONE processors" in caplog.text
    assert "bundle ready" in caplog.text


def test_direct_load_failure_names_current_stage(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    def load(source: str, **kwargs: Any) -> Any:
        kwargs["progress"]("imports_factory", 0)
        raise ImportError("missing model dependency")

    monkeypatch.setattr(policy, "_load_policy", load)
    with pytest.raises(ImportError, match="missing model dependency"):
        policy.load_policy("owner/model", device="cpu")
    assert "FAILED imports_factory after" in caplog.text
    assert "DONE imports_factory" not in caplog.text
    assert "bundle ready" not in caplog.text


@pytest.mark.parametrize("cached", [False, True])
def test_cache_resolution_and_network_retry_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cached: bool,
) -> None:
    caplog.set_level(logging.INFO)
    attempts: list[bool] = []

    def config_from_pretrained(source: str, **kwargs: Any) -> Any:
        assert "START config" in caplog.text
        attempts.append(kwargs["local_files_only"])
        if not cached and kwargs["local_files_only"]:
            raise FileNotFoundError("configuration absent from cache")
        return SimpleNamespace(type="act", output_features={})

    model = SimpleNamespace(eval=lambda: None)
    model.to = lambda device: model

    def from_pretrained(source: str, **kwargs: Any) -> Any:
        assert "START weights" in caplog.text
        return model

    def processors(**kwargs: Any) -> tuple[Any, Any]:
        assert "START processors" in caplog.text
        return None, None

    config_module = types.ModuleType("lerobot.configs")
    config_module.PreTrainedConfig = SimpleNamespace(
        from_pretrained=config_from_pretrained
    )
    factory_module = types.ModuleType("lerobot.policies.factory")
    factory_module.get_policy_class = lambda name: SimpleNamespace(
        from_pretrained=from_pretrained
    )
    factory_module.make_pre_post_processors = processors
    constants = types.ModuleType("lerobot.utils.constants")
    constants.ACTION = "action"
    loading = types.ModuleType("lerobot.utils.loading")
    loading.model_load_context = lambda _: contextlib.nullcontext()
    for module in (config_module, factory_module, constants, loading):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(policy, "ensure_lerobot_on_path", lambda: None)
    monkeypatch.setattr(
        policy,
        "resolve_cached_policy_path",
        lambda *args: "local/snapshot" if cached else None,
    )
    policy.load_policy("owner/model", device="cpu")
    if cached:
        assert attempts == [True]
        assert "cache=local/snapshot" in caplog.text
        assert "allowing Hugging Face download" not in caplog.text
    else:
        assert attempts == [True, False]
        assert "FileNotFoundError: configuration absent from cache" in caplog.text
        assert "allowing Hugging Face download" in caplog.text
    assert "bundle ready" in caplog.text


def test_loading_records_reach_ui_without_recursive_echo() -> None:
    from lerobot_monitor.loop import _UiLogHandler

    received: list[tuple[str, str, bool]] = []

    def log(level: str, message: str, *, echo: bool) -> None:
        received.append((level, message, echo))

    handler = _UiLogHandler(SimpleNamespace(log=log))
    for name in (
        "lerobot_monitor.policy",
        "lerobot_monitor.policy_residency",
        "lerobot.utils.loading",
    ):
        handler.emit(
            logging.LogRecord(name, logging.INFO, __file__, 1, "START config", (), None)
        )
    assert received == [("info", "START config", False)] * 3
    handler.emit(
        logging.LogRecord(
            "lerobot_monitor.loop", logging.INFO, __file__, 1, "recursive", (), None
        )
    )
    assert len(received) == 3


def test_cancelled_local_attempt_never_retries_network(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    config_module = types.ModuleType("lerobot.configs")
    config_module.PreTrainedConfig = object
    factory = types.ModuleType("lerobot.policies.factory")
    factory.get_policy_class = lambda _: None
    factory.make_pre_post_processors = lambda **kw: None
    constants = types.ModuleType("lerobot.utils.constants")
    constants.ACTION = "action"
    loading = types.ModuleType("lerobot.utils.loading")
    loading.model_load_context = lambda _: contextlib.nullcontext()
    for module in (config_module, factory, constants, loading):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(policy, "ensure_lerobot_on_path", lambda: None)
    monkeypatch.setattr(policy, "resolve_cached_policy_path", lambda *args: None)
    attempts: list[str] = []

    def progress(phase: str, completed: int) -> None:
        if phase == "config":
            attempts.append(phase)
            raise policy.PolicyLoadCancelled("cancelled")

    with pytest.raises(policy.PolicyLoadCancelled):
        policy.load_policy("owner/model", device="cpu", progress=progress)
    assert attempts == ["config"]
    assert "cancelled during config" in caplog.text
    assert "allowing Hugging Face download" not in caplog.text
    assert "FAILED" not in caplog.text
