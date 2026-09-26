from types import SimpleNamespace

import pytest

from lerobot_monitor.policy import apply_policy_overrides, apply_requested_overrides


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
    assert config.n_action_steps == 50

    config = Config()
    applied = apply_policy_overrides(
        config,
        {"policy.n_action_steps": "1", "policy.temporal_ensemble_coeff": "none"},
    )
    assert applied == ["policy.n_action_steps", "policy.temporal_ensemble_coeff"]
    assert config.n_action_steps == 1
    assert config.temporal_ensemble_coeff is None


def test_resident_overrides_restore_checkpoint_defaults_and_reject_structure_changes() -> None:
    config = SimpleNamespace(n_action_steps=50, chunk_size=50)
    loaded = SimpleNamespace(path="fake", policy=SimpleNamespace(config=config))
    apply_requested_overrides(loaded, {"policy.n_action_steps": "8"})
    assert config.n_action_steps == 8
    apply_requested_overrides(loaded, {})
    assert config.n_action_steps == 50
    with pytest.raises(ValueError, match="chunk_size cannot be changed"):
        apply_requested_overrides(loaded, {"policy.chunk_size": "64", "policy.n_action_steps": "4"})
    assert (config.chunk_size, config.n_action_steps) == (50, 50)


def test_invalid_known_override_is_atomic() -> None:
    config = SimpleNamespace(n_action_steps=50, num_inference_steps=10)
    with pytest.raises(ValueError, match="num_inference_steps"):
        apply_policy_overrides(config, {"n_action_steps": "4", "num_inference_steps": "bad"})
    assert (config.n_action_steps, config.num_inference_steps) == (50, 10)


def test_smolvla_num_steps_is_a_reversible_runtime_override() -> None:
    config = SimpleNamespace(num_steps=10)
    loaded = SimpleNamespace(path="smolvla", policy=SimpleNamespace(config=config))
    apply_requested_overrides(loaded, {"policy.num_steps": "4"})
    assert config.num_steps == 4
    with pytest.raises(ValueError, match="must be positive"):
        apply_requested_overrides(loaded, {"policy.num_steps": "0"})
    assert config.num_steps == 4
    apply_requested_overrides(loaded, {})
    assert config.num_steps == 10


def test_act_ensemble_is_rebuilt_with_lerobot_implementation(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    module = types.ModuleType("lerobot.policies.act.modeling_act")
    built: list[tuple[float, int]] = []

    def make_ensemble(coefficient: float, chunk_size: int) -> SimpleNamespace:
        built.append((coefficient, chunk_size))
        return SimpleNamespace(coefficient=coefficient)

    module.ACTTemporalEnsembler = make_ensemble
    monkeypatch.setitem(sys.modules, module.__name__, module)
    config = SimpleNamespace(type="act", n_action_steps=1, chunk_size=50, temporal_ensemble_coeff=None)
    loaded = SimpleNamespace(path="act", policy=SimpleNamespace(config=config))
    apply_requested_overrides(loaded, {"temporal_ensemble_coeff": "0.02"})
    assert loaded.policy.temporal_ensembler.coefficient == 0.02
    apply_requested_overrides(loaded, {"temporal_ensemble_coeff": "0.03"})
    assert loaded.policy.temporal_ensembler.coefficient == 0.03
    apply_requested_overrides(loaded, {})
    assert config.temporal_ensemble_coeff is None
    assert built == [(0.02, 50), (0.03, 50)]
