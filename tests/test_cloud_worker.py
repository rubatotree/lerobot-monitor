"""Verify native policy boundaries with CPU array doubles, without importing CUDA."""

from __future__ import annotations

import base64
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import cv2

from lerobot_monitor.cloud.worker import NativePolicyBackend, decode_images


class Tensor(np.ndarray):
    """Small torch tensor double; production transformations are tested unchanged."""

    def clone(self) -> Tensor:
        return self.copy()

    def detach(self) -> Tensor:
        return self

    def to(self, *args: Any, **kwargs: Any) -> Tensor:
        return self

    def numel(self) -> int:
        return self.size


def tensor(values: Any, **kwargs: Any) -> Tensor:
    return np.array(values, dtype=np.float32).view(Tensor)


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> NativePolicyBackend:
    amp_calls: list[str] = []
    grad_calls: list[str] = []
    fake_torch = SimpleNamespace(
        tensor=tensor, float32=np.float32, device=lambda value: SimpleNamespace(type=value),
        isfinite=np.isfinite,
        inference_mode=lambda: (grad_calls.append("inference_mode") or nullcontext()),
        no_grad=lambda: (grad_calls.append("no_grad") or nullcontext()),
        autocast=lambda device_type: (amp_calls.append(device_type) or nullcontext()),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "lerobot.policies.utils", SimpleNamespace(
        prepare_observation_for_inference=lambda observation, device, task, robot_type: observation))
    native = NativePolicyBackend()
    calls: list[Any] = []

    def postprocess(value: Tensor) -> Tensor:
        value += 10  # In-place processors must not corrupt the returned raw prefix.
        return value

    policy = SimpleNamespace(
        config=SimpleNamespace(use_amp=True, rtc_config=SimpleNamespace(mode="guided", execution_horizon=2)),
        select_action=lambda observation: (calls.append("select") or tensor([[1, 2]])),
        predict_action_chunk=lambda observation, **kwargs: (calls.append(kwargs) or tensor([[[1, 2], [3, 4]]])),
        drop_queued_actions=lambda: calls.append("drop"),
    )
    native.loaded = SimpleNamespace(
        policy=policy, device="cuda", robot_type="test_robot", preprocessor=lambda observation: observation,
        postprocessor=postprocess, reset=lambda: calls.append("reset"),
    )
    native.metadata = {"action_keys": ["x", "y"], "action_dim": 2, "image_keys": [], "rtc_training_max_delay": 2}
    native.session = {"state_keys": ["x", "y"], "task": "original", "mode": "select_action"}
    native.test_calls = calls
    native.test_amp = amp_calls
    native.test_grad = grad_calls
    return native


def request(**kwargs: Any) -> dict[str, Any]:
    return {"state": {"x": 0, "y": 0}, "images": {}, "chunk_size": 32, "inference_delay": 0, **kwargs}


def test_select_preserves_history_drops_old_task_and_uses_amp(backend: NativePolicyBackend) -> None:
    first = backend.infer(request())
    second = backend.infer(request(task="new task"))
    backend.infer(request())
    assert first["raw_actions"] == [[1, 2]]
    assert first["actions"] == [[11, 12]]
    assert second["shape"] == [1, 2]
    assert backend.test_calls == ["select", "drop", "select", "select"]
    assert backend.test_amp == ["cuda", "cuda", "cuda"]
    assert backend.test_grad == ["inference_mode", "inference_mode", "inference_mode"]
    assert backend.session["task"] == "new task"


def test_select_reports_queued_actions_for_the_chart_preview(backend: NativePolicyBackend) -> None:
    from collections import deque

    assert backend.infer(request())["queued_actions"] == []
    queue = deque([tensor([[3, 4]]), tensor([[5, 6]])])
    backend.loaded.policy._queues = {"action": queue}
    result = backend.infer(request())
    assert result["queued_actions"] == [[13, 14], [15, 16]]
    # Previewing processes clones, so the policy's own queue keeps its raw values.
    assert [item.tolist() for item in queue] == [[[3, 4]], [[5, 6]]]


def test_queued_action_preview_failure_never_breaks_inference(backend: NativePolicyBackend) -> None:
    from collections import deque

    backend.loaded.policy._queues = {"action": deque([tensor([[1, 2, 3]])])}
    result = backend.infer(request())
    assert result["actions"] == [[11, 12]]
    assert result["queued_actions"] == []


def test_queued_actions_fall_back_to_the_captured_chunk(backend: NativePolicyBackend) -> None:
    import time
    from collections import deque

    from lerobot_monitor.policy import PredictedChunkCapture

    # n_action_steps=1 (SmolVLA-style): the select_action pop drains the queue,
    # so the preview must come from the recorded chunk producer output.
    policy = backend.loaded.policy
    policy.config.n_action_steps = 1
    policy._queues = {"action": deque(maxlen=1)}
    capture = PredictedChunkCapture("_get_action_chunk")
    capture.chunk = tensor([[[1, 2], [3, 4], [5, 6]]])
    capture.recorded_at = time.perf_counter()
    policy._monitor_chunk_capture = capture

    result = backend.infer(request())

    assert result["actions"] == [[11, 12]]
    assert result["queued_actions"] == [[13, 14], [15, 16]]


def test_queued_actions_ignore_a_stale_captured_chunk(backend: NativePolicyBackend) -> None:
    from collections import deque

    from lerobot_monitor.policy import PredictedChunkCapture

    policy = backend.loaded.policy
    policy.config.n_action_steps = 1
    policy._queues = {"action": deque(maxlen=1)}
    capture = PredictedChunkCapture("_get_action_chunk")
    capture.chunk = tensor([[[1, 2], [3, 4]]])
    capture.recorded_at -= 3600.0
    policy._monitor_chunk_capture = capture

    assert backend.infer(request())["queued_actions"] == []


def test_chunk_modes_do_not_report_a_queue(backend: NativePolicyBackend) -> None:
    backend.session["mode"] = "debug_chunk"
    assert "queued_actions" not in backend.infer(request(chunk_size=1))


def test_debug_chunk_resets_and_returns_cpu_absolute_and_raw(backend: NativePolicyBackend) -> None:
    backend.session["mode"] = "debug_chunk"
    result = backend.infer(request(chunk_size=1))
    assert result["raw_actions"] == [[1, 2]]
    assert result["actions"] == [[11, 12]]
    assert backend.test_calls == ["reset", {}]


def test_rtc_reanchors_against_cached_raw_state(backend: NativePolicyBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    class Relative:
        enabled = True
        action_names = None

        def get_cached_state(self) -> Tensor:
            return tensor([[10, 20]])

    class Normalizer:
        pass

    relative = Relative()
    normalizer = Normalizer()
    backend.loaded.preprocessor = SimpleNamespace(steps=[relative, normalizer])
    calls: list[dict[str, Any]] = []

    def reanchor(**kwargs: Any) -> Tensor:
        calls.append(kwargs)
        return kwargs["prev_actions_absolute"] - kwargs["current_state"]

    monkeypatch.setitem(sys.modules, "lerobot.processor", SimpleNamespace(RelativeActionsProcessorStep=Relative, NormalizerProcessorStep=Normalizer))
    monkeypatch.setitem(sys.modules, "lerobot.policies.rtc", SimpleNamespace(reanchor_relative_rtc_prefix=reanchor))
    monkeypatch.setitem(sys.modules, "lerobot.rollout.inference.rtc", SimpleNamespace(_normalize_prev_actions_length=lambda value, target_steps: value[:target_steps]))
    prefix = backend._rtc_prefix(request(prefix_raw=[[90, 90], [91, 91]], prefix_absolute=[[12, 23], [13, 24]]), "cuda")
    assert prefix.tolist() == [[2, 3], [3, 4]]
    assert calls[0]["normalizer_step"] is normalizer
    assert relative.action_names == ["x", "y"]


def test_rtc_full_pipeline_and_trained_delay_bound(backend: NativePolicyBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    backend.session["mode"] = "rtc_chunk"
    monkeypatch.setattr(backend, "_rtc_prefix", lambda payload, device: tensor([[1, 2]]))
    result = backend.infer(request(inference_delay=1, prefix_raw=[[1, 2]], prefix_absolute=[[11, 12]]))
    assert result["raw_actions"] == [[1, 2], [3, 4]]
    assert result["actions"] == [[11, 12], [13, 14]]
    assert backend.test_calls[0]["inference_delay"] == 1
    assert backend.test_grad == ["no_grad"]
    backend.loaded.policy.config.rtc_config.mode = "trained"
    with pytest.raises(ValueError, match="available prefix"):
        backend.infer(request(inference_delay=2, prefix_raw=[[1, 2]], prefix_absolute=[[11, 12]]))


@pytest.mark.parametrize("values", [tensor([[[1, np.nan]]]), tensor([[[1]]]), tensor([[1, 2]])])
def test_rejects_nonfinite_or_malformed_policy_chunk(backend: NativePolicyBackend, values: Tensor) -> None:
    backend.session["mode"] = "debug_chunk"
    backend.loaded.policy.predict_action_chunk = lambda observation, **kwargs: values
    with pytest.raises(ValueError, match="finite|shape"):
        backend.infer(request())


def test_rejects_missing_state_and_required_images(backend: NativePolicyBackend) -> None:
    with pytest.raises(ValueError, match="state keys"):
        backend.infer(request(state={"x": 0}))
    backend.metadata["image_keys"] = ["observation.images.front"]
    with pytest.raises(ValueError, match="required images"):
        backend.infer(request())


def test_png_roundtrip_and_invalid_encoding() -> None:
    pixels = np.arange(18, dtype=np.uint8).reshape(2, 3, 3)
    _, encoded = cv2.imencode(".png", cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR))
    assert np.array_equal(decode_images({"front": base64.b64encode(encoded.tobytes()).decode()})["front"], pixels)
    with pytest.raises(ValueError, match="PNG"):
        decode_images({"front": base64.b64encode(b"not png").decode()})
    with pytest.raises(ValueError):
        decode_images({"front": "invalid base64"})


def test_sync_capability_rejects_relative_actions(backend: NativePolicyBackend) -> None:
    backend.metadata["capabilities"] = {"select_action": False}
    with pytest.raises(ValueError, match="does not support"):
        backend.open({"mode": "select_action"})


def test_guided_rtc_allows_native_autograd_when_torch_available(monkeypatch: pytest.MonkeyPatch) -> None:
    torch = pytest.importorskip("torch")
    monkeypatch.setitem(sys.modules, "lerobot.policies.utils", SimpleNamespace(
        prepare_observation_for_inference=lambda observation, device, task, robot_type: observation))

    def guided(observation: Any, **kwargs: Any) -> Any:
        # Same enable_grad boundary used by LeRobot's guided RTC correction.
        with torch.enable_grad():
            state = torch.ones(1, 2, 2).requires_grad_()
            result = state.square()
            return torch.autograd.grad(result.sum(), state)[0]

    backend = NativePolicyBackend()
    backend.loaded = SimpleNamespace(device="cpu", robot_type="test", preprocessor=lambda value: value,
        postprocessor=lambda value: value + 10,
        policy=SimpleNamespace(config=SimpleNamespace(use_amp=False, rtc_config=SimpleNamespace(mode="guided")),
                               predict_action_chunk=guided))
    backend.metadata = {"action_keys": ["x", "y"], "action_dim": 2, "image_keys": []}
    backend.session = {"state_keys": ["x", "y"], "task": "", "mode": "rtc_chunk"}
    monkeypatch.setattr(backend, "_rtc_prefix", lambda payload, device: None)
    result = backend.infer(request())
    assert result["raw_actions"] == [[2, 2], [2, 2]]
    assert result["actions"] == [[12, 12], [12, 12]]
