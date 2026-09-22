"""Action-chunk inference tests.

The real torch/LeRobot stack is not installed in this test environment, so the
fixture installs a minimal fake ``torch`` plus the two ``lerobot`` modules that
``predict_action_chunk`` imports. Shapes follow the production contract:
observation state ``(J,)`` and action chunk ``(B, T, A)``.
"""

import contextlib
import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from lerobot_monitor.policy import (
    LoadedPolicy,
    create_monitor_inference_engine,
    inference_config_from_extra,
    predict_action_chunk,
)

ACTION = "action"
OBS_STR = "observation"
ACTION_NAMES = [
    "shoulder_pan.pos",
    "shoulder_lift.pos",
    "elbow_flex.pos",
    "wrist_flex.pos",
    "wrist_roll.pos",
    "gripper.pos",
]


class FakeTensor:
    """Just enough of the torch tensor API for the chunk code paths."""

    def __init__(self, array) -> None:
        self.array = np.asarray(array, dtype=np.float32)

    @property
    def ndim(self) -> int:
        return self.array.ndim

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(self.array.shape)

    def unsqueeze(self, dim: int) -> "FakeTensor":
        return FakeTensor(np.expand_dims(self.array, dim))

    def squeeze(self, dim: int) -> "FakeTensor":
        return FakeTensor(np.squeeze(self.array, axis=dim))

    def detach(self) -> "FakeTensor":
        return self

    def cpu(self) -> "FakeTensor":
        return self

    def tolist(self) -> list:
        return self.array.tolist()

    def __getitem__(self, item) -> "FakeTensor":
        return FakeTensor(self.array[item])


class FakeProcessor:
    """Identity pre/postprocessor that records reset calls."""

    def __init__(self) -> None:
        self.resets = 0

    def __call__(self, payload):
        return payload

    def reset(self) -> None:
        self.resets += 1


class ChunkPolicy:
    """Policy exposing the native ``predict_action_chunk`` API.

    ``select_action`` is present so the malformed-chunk case can degrade to the
    sequential path, mirroring policies that ship both methods.
    """

    def __init__(self, chunk) -> None:
        self.chunk = chunk
        self.resets = 0
        self.chunk_calls = 0
        self.steps: list[np.ndarray] = []

    def reset(self) -> None:
        self.resets += 1

    def predict_action_chunk(self, _prepared):
        self.chunk_calls += 1
        return self.chunk

    def select_action(self, _prepared):
        index = len(self.steps)
        row = np.full((6,), float(index), dtype=np.float32)
        self.steps.append(row)
        return FakeTensor(row.reshape(1, -1))


class SequentialPolicy:
    """Policy with only ``select_action``, as older checkpoints behave."""

    def __init__(self, dims: int = 6) -> None:
        self.dims = dims
        self.resets = 0
        self.steps: list[np.ndarray] = []

    def reset(self) -> None:
        self.resets += 1

    def select_action(self, _prepared):
        index = len(self.steps)
        row = np.full((self.dims,), float(index), dtype=np.float32)
        self.steps.append(row)
        return FakeTensor(row.reshape(1, -1))


@pytest.fixture
def fake_lerobot(monkeypatch):
    """Install a minimal fake torch + lerobot module set."""

    def make_robot_action(action, dataset_features):
        names = dataset_features[ACTION]["names"]
        flat = np.asarray(action.array).reshape(-1)
        return {name: float(value) for name, value in zip(names, flat)}

    torch_module = types.ModuleType("torch")
    torch_module.inference_mode = lambda: contextlib.nullcontext()
    torch_module.as_tensor = lambda value: value if isinstance(value, FakeTensor) else FakeTensor(value)
    torch_module.device = lambda name: name
    torch_module.cuda = SimpleNamespace(is_available=lambda: False)

    constants = types.ModuleType("lerobot.utils.constants")
    constants.ACTION = ACTION
    constants.OBS_STR = OBS_STR
    utils = types.ModuleType("lerobot.utils")
    utils.constants = constants
    policies_utils = types.ModuleType("lerobot.policies.utils")
    policies_utils.make_robot_action = make_robot_action
    policies_utils.prepare_observation_for_inference = (
        lambda observation, _device, _task, _robot_type: dict(observation)
    )
    policies = types.ModuleType("lerobot.policies")
    policies.utils = policies_utils
    lerobot = types.ModuleType("lerobot")
    lerobot.policies = policies
    lerobot.utils = utils

    for name, module in {
        "torch": torch_module,
        "lerobot": lerobot,
        "lerobot.policies": policies,
        "lerobot.policies.utils": policies_utils,
        "lerobot.utils": utils,
        "lerobot.utils.constants": constants,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return SimpleNamespace(make_robot_action=make_robot_action)


def loaded_policy(policy, *, names=None, preprocessor=None, postprocessor=None) -> LoadedPolicy:
    ordered = list(names or ACTION_NAMES)
    return LoadedPolicy(
        path="fake/policy",
        device="cpu",
        task="pick cube",
        policy=policy,
        preprocessor=preprocessor or FakeProcessor(),
        postprocessor=postprocessor or FakeProcessor(),
        dataset_features={ACTION: {"dtype": "float32", "shape": (len(ordered),), "names": ordered}},
        ordered_action_keys=ordered,
    )


def observation_joints(value: float = 0.0) -> dict[str, float]:
    return {name.removesuffix(".pos"): value for name in ACTION_NAMES}


def test_native_chunk_path_maps_every_timestep(fake_lerobot) -> None:
    chunk = np.arange(1 * 4 * 6, dtype=np.float32).reshape(1, 4, 6)
    policy = ChunkPolicy(FakeTensor(chunk))
    loaded = loaded_policy(policy)

    result = predict_action_chunk(loaded, observation_joints(), {}, 4)

    assert result.strategy == "policy_chunk"
    assert result.degraded is False
    assert result.warnings == []
    assert len(result.actions) == 4
    assert sorted(result.actions[0]) == sorted(observation_joints())
    # Row 0 maps to the first six raw values in dataset-feature order.
    assert result.actions[0]["shoulder_pan"] == pytest.approx(0.0)
    assert result.actions[0]["gripper"] == pytest.approx(5.0)
    assert result.actions[3]["gripper"] == pytest.approx(23.0)
    assert policy.chunk_calls == 1
    assert policy.resets == 1


def test_native_chunk_is_truncated_to_chunk_size(fake_lerobot) -> None:
    chunk = np.arange(1 * 9 * 6, dtype=np.float32).reshape(1, 9, 6)
    loaded = loaded_policy(ChunkPolicy(FakeTensor(chunk)))

    result = predict_action_chunk(loaded, observation_joints(), {}, 3)

    assert result.strategy == "policy_chunk"
    assert len(result.actions) == 3
    assert result.actions[2]["shoulder_pan"] == pytest.approx(12.0)


def test_missing_native_chunk_degrades_to_sequential_select_action(fake_lerobot) -> None:
    policy = SequentialPolicy()
    loaded = loaded_policy(policy)

    result = predict_action_chunk(loaded, observation_joints(), {}, 5)

    assert result.strategy == "sequential_select_action"
    assert result.degraded is True
    assert len(result.actions) == 5
    assert any("predict_action_chunk" in warning for warning in result.warnings)
    assert len(policy.steps) == 5
    assert result.actions[0]["shoulder_pan"] == pytest.approx(0.0)
    assert result.actions[4]["shoulder_pan"] == pytest.approx(4.0)


def test_malformed_native_chunk_degrades_with_warning(fake_lerobot) -> None:
    chunk = np.zeros((1, 3, 4, 2), dtype=np.float32)
    policy = ChunkPolicy(FakeTensor(chunk))
    loaded = loaded_policy(policy)

    result = predict_action_chunk(loaded, observation_joints(), {}, 2)

    assert result.strategy == "sequential_select_action"
    assert result.degraded is True
    assert any("native action chunk unavailable" in warning for warning in result.warnings)
    assert policy.chunk_calls == 1


def test_partial_action_is_filled_from_input_joints(fake_lerobot) -> None:
    names = ["shoulder_pan.pos", "shoulder_lift.pos", "gripper.pos"]
    chunk = np.array([[[10.0, 20.0, 30.0]]], dtype=np.float32)
    loaded = loaded_policy(ChunkPolicy(FakeTensor(chunk)), names=names)
    joints = observation_joints()
    joints["elbow_flex"] = 42.0

    result = predict_action_chunk(loaded, joints, {}, 1)

    assert len(result.actions) == 1
    action = result.actions[0]
    assert sorted(action) == sorted(joints)
    assert action["shoulder_pan"] == pytest.approx(10.0)
    assert action["gripper"] == pytest.approx(30.0)
    assert action["elbow_flex"] == pytest.approx(42.0)


def test_non_positive_chunk_size_is_rejected(fake_lerobot) -> None:
    loaded = loaded_policy(SequentialPolicy())

    with pytest.raises(ValueError, match="chunk_size must be positive"):
        predict_action_chunk(loaded, observation_joints(), {}, 0)


def test_inference_config_from_extra_parses_rtc_fields(monkeypatch) -> None:
    @dataclass
    class FakeRTCConfig:
        enabled: bool = True
        mode: str = "guided"
        prefix_attention_schedule: str = "LINEAR"
        max_guidance_weight: float = 10.0
        execution_horizon: int = 10
        debug: bool = False
        debug_maxlen: int = 100

    @dataclass
    class FakeRTCInferenceConfig:
        rtc: FakeRTCConfig
        queue_threshold: int = 30

    @dataclass
    class FakeSyncInferenceConfig:
        pass

    rtc_module = types.ModuleType("lerobot.policies.rtc.configuration_rtc")
    rtc_module.RTCConfig = FakeRTCConfig
    inference_module = types.ModuleType("lerobot.rollout.inference")
    inference_module.RTCInferenceConfig = FakeRTCInferenceConfig
    inference_module.SyncInferenceConfig = FakeSyncInferenceConfig
    monkeypatch.setitem(sys.modules, "lerobot.policies.rtc.configuration_rtc", rtc_module)
    monkeypatch.setitem(sys.modules, "lerobot.rollout.inference", inference_module)

    config = inference_config_from_extra(
        {
            "inference.type": "rtc",
            "inference.rtc.execution_horizon": "15",
            "inference.rtc.max_guidance_weight": "5.0",
            "inference.queue_threshold": "12",
        }
    )

    assert isinstance(config, FakeRTCInferenceConfig)
    assert config.rtc.execution_horizon == 15
    assert config.rtc.max_guidance_weight == pytest.approx(5.0)
    assert config.queue_threshold == 12


def test_create_monitor_engine_installs_rtc_processor(monkeypatch) -> None:
    class FakePolicyConfig:
        rtc_config = None

    class FakeRTCInferenceConfig:
        def __init__(self, rtc) -> None:
            self.rtc = rtc

    rtc_config = SimpleNamespace(enabled=True, execution_horizon=15)
    policy = SimpleNamespace(
        config=FakePolicyConfig(),
        init_rtc_processor=MagicMock(),
    )
    loaded = loaded_policy(policy)
    captured: dict[str, object] = {}
    engine = object()

    def fake_create_inference_engine(config, **kwargs):
        captured["config"] = config
        captured.update(kwargs)
        return engine

    inference_module = types.ModuleType("lerobot.rollout.inference")
    inference_module.RTCInferenceConfig = FakeRTCInferenceConfig
    inference_module.create_inference_engine = fake_create_inference_engine
    rtc_module = types.ModuleType("lerobot.rollout.inference.rtc")
    rtc_module.supports_rtc_inference = lambda _policy: True
    monkeypatch.setitem(sys.modules, "lerobot.rollout.inference", inference_module)
    monkeypatch.setitem(sys.modules, "lerobot.rollout.inference.rtc", rtc_module)

    result = create_monitor_inference_engine(
        loaded,
        inference_config=FakeRTCInferenceConfig(rtc_config),
        hw_features={"observation.state": {"names": ACTION_NAMES}},
        task="pick cube",
        fps=15,
    )

    assert result is engine
    assert policy.config.rtc_config is rtc_config
    policy.init_rtc_processor.assert_called_once_with()
    assert captured["task"] == "pick cube"
    assert captured["fps"] == 15
    assert captured["robot_wrapper"].robot_type == loaded.robot_type
