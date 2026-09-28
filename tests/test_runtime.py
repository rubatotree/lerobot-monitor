import pytest

from lerobot_monitor.runtime import format_runtime, probe_runtime


def test_probe_runtime_has_python() -> None:
    info = probe_runtime()
    assert "python" in info
    assert info["version"]
    label = format_runtime(info)
    assert info["version"] in label


@pytest.mark.parametrize("failure", [False, True])
def test_policy_preparation_reports_stages_without_hiding_errors(monkeypatch, failure):
    from lerobot_monitor.runtime import prepare_policy_runtime

    def imports(progress):
        progress("imports_config", 0)
        progress("imports_factory", 0)
        if failure:
            raise ModuleNotFoundError("optional dependency")

    monkeypatch.setattr("lerobot_monitor.policy.import_policy_dependencies", imports)
    messages = []
    result = prepare_policy_runtime(lambda level, message: messages.append(message))
    assert result["state"] == ("error" if failure else "ready")
    assert result["stage_durations_ms"]["imports_config"] >= 0
    assert result["elapsed_ms"] >= 0
    assert any("START imports_factory" in message for message in messages)
    if failure:
        assert result["phase"] == "imports_factory"
        assert result["error"] == "ModuleNotFoundError: optional dependency"
        assert not any("DONE imports_factory" in message for message in messages)
    else:
        assert result["stage_durations_ms"]["imports_factory"] >= 0
