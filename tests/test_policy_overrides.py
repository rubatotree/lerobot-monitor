from types import SimpleNamespace

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
