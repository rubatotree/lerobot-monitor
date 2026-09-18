"""Load a LeRobot pretrained policy and run one inference step.

Policy I/O uses LeRobot's own helpers so ACT / SmolVLA / Pi0.5 share one path.
Cameras live in CameraHub, so this module merges them with the bus observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from .pathutil import ensure_lerobot_on_path
from .types import JOINT_ORDER, observation_to_pose


@dataclass
class LoadedPolicy:
    path: str
    device: str
    task: str
    policy: Any
    preprocessor: Any
    postprocessor: Any
    dataset_features: dict[str, Any]
    ordered_action_keys: list[str]
    robot_type: str = "so101_follower"

    def reset(self) -> None:
        self.policy.reset()
        if hasattr(self.preprocessor, "reset"):
            self.preprocessor.reset()
        if hasattr(self.postprocessor, "reset"):
            self.postprocessor.reset()


def load_policy(
    path: str,
    *,
    device: str = "cuda",
    task: str = "",
    robot_type: str = "so101_follower",
    rename_map: dict[str, str] | None = None,
) -> LoadedPolicy:
    ensure_lerobot_on_path()
    import torch

    from lerobot.configs import PreTrainedConfig
    from lerobot.policies.factory import get_policy_class, make_pre_post_processors
    from lerobot.utils.constants import ACTION

    if device.startswith("cuda") and not torch.cuda.is_available():
        device = "cpu"

    cfg = PreTrainedConfig.from_pretrained(path)
    cfg.pretrained_path = path
    cfg.device = device
    policy_cls = get_policy_class(cfg.type)
    policy = policy_cls.from_pretrained(path, config=cfg)
    policy = policy.to(device)
    policy.eval()

    preprocessor, postprocessor = make_pre_post_processors(
        policy_cfg=cfg,
        pretrained_path=path,
        preprocessor_overrides={
            "device_processor": {"device": device},
            "rename_observations_processor": {"rename_map": rename_map or {}},
        },
    )

    output = cfg.output_features.get(ACTION) or cfg.output_features.get("action")
    if output is not None and getattr(output, "names", None):
        ordered = list(output.names)
    else:
        ordered = [f"{name}.pos" for name in JOINT_ORDER]

    dataset_features = {
        ACTION: {"dtype": "float32", "shape": (len(ordered),), "names": ordered},
    }
    return LoadedPolicy(
        path=path,
        device=device,
        task=task,
        policy=policy,
        preprocessor=preprocessor,
        postprocessor=postprocessor,
        dataset_features=dataset_features,
        ordered_action_keys=ordered,
        robot_type=robot_type,
    )


def predict_pose(
    loaded: LoadedPolicy,
    joints: Mapping[str, float],
    images_rgb: Mapping[str, np.ndarray],
) -> dict[str, float]:
    """Run one policy step.

    Observation layout:
        observation.state           (J,) float32  — joints in JOINT_ORDER
        observation.images.<name>   (H, W, 3) uint8 RGB
    Action layout:
        tensor [action_dim] mapped via dataset_features names → joint pose.
    """
    import torch

    from lerobot.policies.utils import make_robot_action, prepare_observation_for_inference
    from lerobot.utils.constants import ACTION, OBS_STR

    state = np.array([float(joints[name]) for name in JOINT_ORDER if name in joints], dtype=np.float32)
    observation: dict[str, np.ndarray] = {f"{OBS_STR}.state": state}
    for cam_name, rgb in images_rgb.items():
        observation[f"{OBS_STR}.images.{cam_name}"] = np.ascontiguousarray(rgb)

    device = torch.device(loaded.device if torch.cuda.is_available() or loaded.device == "cpu" else "cpu")
    with torch.inference_mode():
        prepared = prepare_observation_for_inference(
            observation, device, loaded.task, loaded.robot_type
        )
        prepared = loaded.preprocessor(prepared)
        action = loaded.policy.select_action(prepared)
        action = loaded.postprocessor(action)

    try:
        robot_action = make_robot_action(action, loaded.dataset_features)
    except Exception:
        # Fallback when the policy tensor is already a named mapping.
        if isinstance(action, dict):
            robot_action = {str(k): float(v) for k, v in action.items()}
        else:
            flat = action.squeeze(0).detach().cpu().tolist()
            robot_action = {
                key: float(flat[i]) for i, key in enumerate(loaded.ordered_action_keys) if i < len(flat)
            }
    pose = observation_to_pose(robot_action)
    if len(pose) < len(JOINT_ORDER):
        # Fill unspecified joints from the current observation so a partial action cannot drop them.
        merged = dict(joints)
        merged.update(pose)
        return merged
    return pose
