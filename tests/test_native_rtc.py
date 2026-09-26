"""Use the actual sibling LeRobot RTC engine with a gated model and no hardware."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from lerobot_monitor.config import (
    CamerasConfig,
    MonitorConfig,
    RecordingConfig,
    RobotConfig,
)
from lerobot_monitor.loop import ControlLoop
from lerobot_monitor.policy import LoadedPolicy
from lerobot_monitor.types import JOINT_ORDER

torch = pytest.importorskip("torch")
rtc = pytest.importorskip("lerobot.rollout.inference.rtc")


def test_native_rtc_keeps_sending_during_inference_and_marks_exact_chunk(tmp_path: Path) -> None:
    class Pipeline:
        steps: tuple = ()

        def __call__(self, value: Any) -> Any:
            return value

        def reset(self) -> None:
            pass

    class Policy:
        def __init__(self) -> None:
            self.config = SimpleNamespace()
            self.release = threading.Semaphore(1)
            self.second_started = threading.Event()
            self.calls = 0

        def predict_action_chunk(self, batch: dict, **kwargs: Any) -> Any:
            self.calls += 1
            if self.calls == 2:
                self.second_started.set()
            assert self.release.acquire(timeout=3)
            # Model action shape: [batch=1, horizon=10, joints=6].
            return torch.zeros(1, 10, len(JOINT_ORDER))

        def reset(self) -> None:
            pass

        def supports_text_generation(self) -> bool:
            return False

    camera_threads: list[str] = []
    cameras = MagicMock()

    def images(**kwargs: Any) -> dict:
        camera_threads.append(threading.current_thread().name)
        return {}

    cameras.rollout_rgb_map.side_effect = images
    config = MonitorConfig(
        robot=RobotConfig(auto_connect=False), cameras=CamerasConfig(probe=False),
        recording=RecordingConfig(root=tmp_path),
    )
    loop = ControlLoop(config, cameras, MagicMock(), MagicMock())
    policy = Policy()
    action_names = [f"{name}.pos" for name in JOINT_ORDER]
    loaded = LoadedPolicy(
        path="fake", device="cpu", task="test", policy=policy,
        preprocessor=Pipeline(), postprocessor=Pipeline(),
        dataset_features={"action": {"names": action_names}}, ordered_action_keys=action_names,
    )
    engine = rtc.RTCInferenceEngine(
        policy=policy, preprocessor=loaded.preprocessor, postprocessor=loaded.postprocessor,
        robot_wrapper=SimpleNamespace(robot_type="mock", action_features={}),
        rtc_config=rtc.RTCConfig(),
        hw_features={"observation.state": {"dtype": "float32", "shape": (6,), "names": list(JOINT_ORDER)}},
        task="test", fps=30, device="cpu", rtc_queue_threshold=10,
    )
    loop.joints = {name: 0.0 for name in JOINT_ORDER}
    loop.loaded_policy = loaded
    loop._inference_engine = engine
    loop.mode = "rollout"
    loop.task_t0 = time.perf_counter()
    loop._rollout_timeline.set_enabled(True)
    loop._bind_rtc_events(engine, loaded)
    engine.start()
    try:
        engine.resume()
        engine.notify_observation(dict(loop.joints))
        assert policy.second_started.wait(2)
        for _ in range(5):
            loop._read_at = time.perf_counter()
            loop._next_policy_t = 0.0
            loop._output_due = True
            loop._tick_rollout()
        assert loop.follower.send_pose.call_count == 5
        assert policy.calls == 2  # second prediction remains blocked throughout all sends
        blocks = loop._rollout_timeline.snapshot()["blocks"]
        assert blocks[0]["active"] is not None
        assert blocks[1]["active"] is None and blocks[1]["end"] is None
        assert camera_threads and set(camera_threads) == {"RTCInference"}
        cameras.latest_rgb_map.assert_not_called()
        started = time.perf_counter()
        loop._end_rollout()
        assert time.perf_counter() - started < 0.2
        assert loop._inference_engine is None
    finally:
        policy.release.release(4)
        engine.stop()
        assert engine.wait_stopped(timeout=3)
