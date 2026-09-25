from types import SimpleNamespace

import pytest

from lerobot_monitor.policy import apply_policy_overrides


def test_policy_prefix_and_coercion() -> None:
    cfg = SimpleNamespace(n_action_steps=50, temporal_ensemble_coeff=0.0, use_amp=False)
    applied = apply_policy_overrides(
        cfg,
        {
            "policy.n_action_steps": "1",
            "temporal_ensemble_coeff": "0.02",
            "use_amp": "true",
            "robot.port": "COM6",
            "unknown_field": "x",
        },
    )
    assert cfg.n_action_steps == 1
    assert cfg.temporal_ensemble_coeff == 0.02
    assert cfg.use_amp is True
    assert "policy.n_action_steps" in applied
    assert "robot.port" not in applied
    assert "unknown_field" not in applied


def test_policy_overrides_revalidate_config_and_clear_optional_values() -> None:
    class Config:
        n_action_steps = 50
        temporal_ensemble_coeff = 0.01

        def __post_init__(self) -> None:
            if self.temporal_ensemble_coeff is not None and self.n_action_steps > 1:
                raise NotImplementedError("n_action_steps must be 1")

    config = Config()
    with pytest.raises(NotImplementedError, match="n_action_steps"):
        apply_policy_overrides(config, {"policy.n_action_steps": "16"})

    config = Config()
    applied = apply_policy_overrides(
        config,
        {"policy.n_action_steps": "1", "policy.temporal_ensemble_coeff": "none"},
    )
    assert applied == ["policy.n_action_steps", "policy.temporal_ensemble_coeff"]
    assert config.n_action_steps == 1
    assert config.temporal_ensemble_coeff is None
