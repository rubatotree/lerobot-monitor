"""Load a LeRobot pretrained policy and run one inference step.

Policy I/O uses LeRobot's own helpers so ACT / SmolVLA / Pi0.5 share one path.
Cameras live in CameraHub, so this module merges them with the bus observation.
"""

from __future__ import annotations

import inspect
import logging
import os
import sys
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, fields
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import numpy as np

from .pathutil import ensure_lerobot_on_path
from .rollout_timeline import RolloutTimeline
from .types import JOINT_ORDER, observation_to_pose

logger = logging.getLogger(__name__)


@contextmanager
def _prefer_hub_cache() -> Iterator[None]:
    """Fail closed against the network if weights are already in the HF cache."""
    key = "HF_HUB_OFFLINE"
    previous = os.environ.get(key)
    os.environ[key] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def _coerce_override(raw: str, current: Any) -> Any:
    text = str(raw).strip()
    if text.lower() in {"none", "null"}:
        return None
    if isinstance(current, bool):
        return text.lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int) and not isinstance(current, bool):
        return int(float(text))
    if isinstance(current, float):
        return float(text)
    if isinstance(current, Enum):
        return type(current)(text.upper())
    lower = text.lower()
    if current is None and lower in {"true", "false"}:
        return lower == "true"
    if current is None:
        try:
            return int(text) if "." not in text else float(text)
        except ValueError:
            return text
    return text


RUNTIME_POLICY_EXTRA_KEYS = frozenset({"task", "fps", "interpolation_multiplier", "control_rate"})
RUNTIME_POLICY_EXTRA_PREFIXES = ("inference.", "robot.", "dataset.", "teleop.", "strategy.", "record.")


def apply_policy_overrides(cfg: Any, extra: Mapping[str, str] | None) -> list[str]:
    """Set ``policy.n_action_steps``-style extras onto a loaded policy config.

    Nested dotted paths walk attributes. Unknown keys are ignored.
    """
    applied: list[str] = []
    if not extra:
        return applied
    for key, value in extra.items():
        path = str(key).strip()
        if path.startswith("--"):
            path = path[2:]
        if path.startswith("policy."):
            path = path[len("policy.") :]
        if not path or path in RUNTIME_POLICY_EXTRA_KEYS or path.startswith(RUNTIME_POLICY_EXTRA_PREFIXES):
            continue
        try:
            obj = cfg
            parts = path.split(".")
            for part in parts[:-1]:
                obj = getattr(obj, part)
            name = parts[-1]
            current = getattr(obj, name)
            setattr(obj, name, _coerce_override(str(value), current))
        except Exception:
            continue
        applied.append(key)
    if applied:
        # ACT couples n_action_steps and temporal_ensemble_coeff in __post_init__.
        # Applying overrides after config construction must not bypass that validation.
        validate = getattr(cfg, "__post_init__", None)
        if callable(validate):
            validate()
    return applied


def apply_requested_overrides(loaded: LoadedPolicy, extra: Mapping[str, str] | None) -> list[str]:
    """Apply ``policy.*`` extras to an already-loaded instance; return the keys applied.

    Runs on every claim of a resident instance, so one weight copy can serve requests
    that differ only in runtime configuration instead of loading a second one.
    """
    config = getattr(loaded.policy, "config", None)
    if config is None:
        return []
    applied = apply_policy_overrides(config, extra)
    if applied:
        logger.info("applied policy overrides to resident %s: %s", loaded.path, ", ".join(applied))
    return applied


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


@dataclass
class ActionChunk:
    """One predicted action chunk plus the path used to obtain it."""

    actions: list[dict[str, float]]
    strategy: str
    degraded: bool
    warnings: list[str]
    cache_hit: bool = False
    model_wait_ms: float = 0.0
    model_load_ms: float = 0.0
    compute_ms: float = 0.0


def resolve_cached_policy_path(path: str, revision: str = "") -> str | None:
    """Resolve a repo id to an already-cached local snapshot without touching the Hub."""
    local_path = Path(path).expanduser()
    if local_path.is_dir():
        return str(local_path.resolve())
    try:
        from .library import cached_hub_snapshot, is_policy_dir
        from .model_hub import parse_remote

        parsed = parse_remote(path, revision=revision)
    except Exception:
        return None
    if parsed.source != "huggingface":
        return None
    snapshot = cached_hub_snapshot(parsed.repo_id, parsed.revision)
    if snapshot is None or not is_policy_dir(Path(snapshot)):
        return None
    return snapshot


def load_policy(
    path: str,
    *,
    device: str = "cuda",
    task: str = "",
    robot_type: str = "so101_follower",
    rename_map: dict[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
    progress: Callable[[str, int], None] | None = None,
) -> LoadedPolicy:
    ensure_lerobot_on_path()
    import torch

    from lerobot.configs import PreTrainedConfig
    from lerobot.policies.factory import get_policy_class, make_pre_post_processors
    from lerobot.utils.constants import ACTION

    requested_revision = str((extra or {}).get("policy.pretrained_revision", "")).strip()
    cached_path = resolve_cached_policy_path(path, requested_revision)
    load_path = cached_path or path
    is_local_source = cached_path is not None or Path(path).expanduser().is_dir()
    if cached_path is not None:
        logger.info("using cached policy snapshot for %s: %s", path, cached_path)

    if device.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError(
            f"CUDA requested but this process has no GPU torch "
            f"(torch={torch.__version__}, cuda_built={torch.version.cuda}, "
            f"python={sys.executable}). Install a CUDA wheel into the lerobot venv "
            f"or set device=cpu."
        )

    def report(phase: str, completed: int) -> None:
        if progress is not None:
            progress(phase, completed)

    def _load(*, local_only: bool) -> LoadedPolicy:
        kwargs = {"local_files_only": local_only}
        report("config", 0)
        cfg = PreTrainedConfig.from_pretrained(load_path, **kwargs)
        cfg.pretrained_path = load_path
        cfg.device = device
        apply_policy_overrides(cfg, extra)
        vlm_model_name = getattr(cfg, "vlm_model_name", None)
        if isinstance(vlm_model_name, str) and vlm_model_name:
            cached_vlm = resolve_cached_policy_path(vlm_model_name)
            if cached_vlm is not None:
                logger.info("using cached VLM snapshot for %s: %s", vlm_model_name, cached_vlm)
                cfg.vlm_model_name = cached_vlm
        report("weights", 1)
        policy_cls = get_policy_class(cfg.type)
        policy = policy_cls.from_pretrained(load_path, config=cfg, local_files_only=local_only)
        report("device", 2)
        policy = policy.to(device)
        policy.eval()
        report("processors", 3)
        preprocessor_overrides = {
            "device_processor": {"device": device},
            "rename_observations_processor": {"rename_map": rename_map or {}},
        }
        resolved_vlm_name = getattr(cfg, "vlm_model_name", None)
        if isinstance(resolved_vlm_name, str) and resolved_vlm_name:
            preprocessor_overrides["tokenizer_processor"] = {
                "tokenizer_name": resolved_vlm_name,
            }
        preprocessor, postprocessor = make_pre_post_processors(
            policy_cfg=cfg,
            pretrained_path=load_path,
            pretrained_revision=requested_revision or None,
            preprocessor_overrides=preprocessor_overrides,
        )
        output = cfg.output_features.get(ACTION) or cfg.output_features.get("action")
        if output is not None and getattr(output, "names", None):
            ordered = list(output.names)
        else:
            ordered = [f"{name}.pos" for name in JOINT_ORDER]
        return LoadedPolicy(
            path=path,
            device=device,
            task=task,
            policy=policy,
            preprocessor=preprocessor,
            postprocessor=postprocessor,
            dataset_features={
                ACTION: {"dtype": "float32", "shape": (len(ordered),), "names": ordered},
            },
            ordered_action_keys=ordered,
            robot_type=robot_type,
        )

    if cached_path is not None or is_local_source:
        with _prefer_hub_cache():
            return _load(local_only=True)
    try:
        with _prefer_hub_cache():
            return _load(local_only=True)
    except Exception as exc:
        logger.info("policy not fully cached (%s); allowing Hugging Face download for %s", exc, path)
        return _load(local_only=False)


@dataclass
class InferenceRobotAdapter:
    """Minimal robot metadata consumed by LeRobot's inference-engine factory."""

    robot_type: str
    action_features: dict[str, float]


def inference_config_from_extra(extra: Mapping[str, str] | None):
    """Parse monitor ``inference.*`` extras into LeRobot's engine config."""
    ensure_lerobot_on_path()
    from lerobot.policies.rtc.configuration_rtc import RTCConfig
    from lerobot.rollout.inference import RTCInferenceConfig, SyncInferenceConfig

    values = {
        str(key).removeprefix("--"): str(value)
        for key, value in (extra or {}).items()
        if str(key).strip()
    }
    if values.get("inference.type", "sync").strip().lower() != "rtc":
        return SyncInferenceConfig()

    defaults = RTCConfig()
    rtc_values: dict[str, Any] = {}
    for field in fields(RTCConfig):
        key = f"inference.rtc.{field.name}"
        if key in values:
            rtc_values[field.name] = _coerce_override(values[key], getattr(defaults, field.name))
    queue_threshold = _coerce_override(
        values.get("inference.queue_threshold", "30"),
        30,
    )
    return RTCInferenceConfig(
        rtc=RTCConfig(**rtc_values),
        queue_threshold=int(queue_threshold),
    )


def create_monitor_inference_engine(
    loaded: LoadedPolicy,
    *,
    inference_config: Any,
    hw_features: dict,
    task: str,
    fps: float,
    shutdown_event: Any | None = None,
):
    """Build LeRobot's sync or RTC inference engine for the monitor rollout."""
    ensure_lerobot_on_path()
    from lerobot.rollout.inference import RTCInferenceConfig, create_inference_engine
    from lerobot.rollout.inference.rtc import supports_rtc_inference

    if isinstance(inference_config, RTCInferenceConfig):
        if not supports_rtc_inference(loaded.policy):
            raise ValueError(
                "RTC inference is not supported by this policy: predict_action_chunk() must accept "
                "inference_delay and prev_chunk_left_over."
            )
        loaded.policy.config.rtc_config = inference_config.rtc
        if hasattr(loaded.policy, "init_rtc_processor"):
            loaded.policy.init_rtc_processor()
    else:
        if hasattr(loaded.policy.config, "rtc_config"):
            loaded.policy.config.rtc_config = None
        if hasattr(loaded.policy, "init_rtc_processor"):
            loaded.policy.init_rtc_processor()

    robot = InferenceRobotAdapter(
        robot_type=loaded.robot_type,
        action_features={name: float for name in loaded.ordered_action_keys},
    )
    return create_inference_engine(
        inference_config,
        policy=loaded.policy,
        preprocessor=loaded.preprocessor,
        postprocessor=loaded.postprocessor,
        robot_wrapper=robot,
        hw_features=hw_features,
        dataset_features=loaded.dataset_features,
        ordered_action_keys=loaded.ordered_action_keys,
        task=task,
        fps=fps,
        device=loaded.device,
        shutdown_event=shutdown_event,
    )


def pose_from_action_tensor(
    loaded: LoadedPolicy,
    action: Any,
    fallback_joints: Mapping[str, float],
) -> dict[str, float]:
    """Map an inference-engine action tensor to a complete monitor pose."""
    return _action_pose(action, loaded, fallback_joints)


def rtc_leftover_poses(
    engine: Any,
    loaded: LoadedPolicy,
    fallback_joints: Mapping[str, float],
) -> list[dict[str, float]]:
    """Read the processed future actions already held by LeRobot's RTC queue."""
    queue = getattr(engine, "action_queue", None)
    if queue is None:
        return []
    try:
        leftover = queue.get_processed_left_over()
    except Exception as exc:  # noqa: BLE001 - chart telemetry must not affect control
        logger.debug("could not read RTC leftover actions: %s", exc)
        return []
    if leftover is None:
        return []
    return [_action_pose(action, loaded, fallback_joints) for action in leftover]


def _policy_action_queue(policy: Any) -> deque | None:
    """Return the action queue used by synchronous chunking policies."""
    for attr in getattr(policy, "_action_queue_attrs", ("_queues", "_action_queue")):
        queue = getattr(policy, attr, None)
        if isinstance(queue, Mapping):
            queue = queue.get("action")
        if isinstance(queue, deque):
            return queue
    return None


def action_queue_state(target: Any) -> tuple[int | None, int | None]:
    """Return ``(remaining, index)`` for a policy or inference-engine queue."""
    try:
        queue = getattr(target, "action_queue", None)
        if queue is None:
            queue = _policy_action_queue(target)
        if queue is None:
            return (None, None)
        try:
            qsize_method = getattr(queue, "qsize", None)
            qsize = int(qsize_method()) if callable(qsize_method) else len(queue)
        except Exception:  # noqa: BLE001 - telemetry must not affect control
            qsize = None
        get_index = getattr(queue, "get_action_index", None)
        try:
            index = int(get_index()) if callable(get_index) else None
        except Exception:  # noqa: BLE001 - telemetry must not affect control
            index = None
        return (qsize, index)
    except Exception as exc:  # noqa: BLE001 - telemetry must not affect control
        logger.debug("could not read action queue state: %s", exc)
        return (None, None)


def install_inference_timeline(
    policy: Any,
    timeline: RolloutTimeline,
    *,
    step_s: float,
) -> bool:
    """Wrap ``predict_action_chunk`` once and bind it to the active rollout."""
    try:
        chunk_method = getattr(policy, "predict_action_chunk", None)
        if not callable(chunk_method):
            return False
        try:
            normalized_step_s = float(step_s)
        except (TypeError, ValueError):
            normalized_step_s = 1.0 / 30.0
        if normalized_step_s <= 0.0:
            normalized_step_s = 1.0 / 30.0

        existing = getattr(policy, "_monitor_timeline_state", None)
        if getattr(policy, "_monitor_timeline_wrapped", False) and isinstance(existing, dict):
            existing["timeline"] = timeline
            existing["step_s"] = normalized_step_s
            return True

        signature = inspect.signature(chunk_method)
        state: dict[str, Any] = {"timeline": timeline, "step_s": normalized_step_s}

        @wraps(chunk_method)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            active_timeline = state["timeline"]
            try:
                token = active_timeline.note_inference_start(
                    kind="rtc",
                    step_s=float(state["step_s"]),
                )
            except Exception:  # noqa: BLE001 - telemetry must not affect control
                token = None
            try:
                result = chunk_method(*args, **kwargs)
            except Exception:
                try:
                    active_timeline.note_inference_end(token, ok=False, steps=None)
                except Exception:  # noqa: BLE001, S110 - telemetry must not affect control
                    pass
                raise
            try:
                active_timeline.note_inference_end(
                    token,
                    ok=True,
                    steps=tensor_steps(result),
                )
            except Exception:  # noqa: BLE001, S110 - telemetry must not affect control
                pass
            return result

        wrapped.__signature__ = signature  # type: ignore[attr-defined]
        wrapped._monitor_timeline_wrapped = True  # type: ignore[attr-defined]
        policy.predict_action_chunk = wrapped
        policy._monitor_timeline_state = state
        policy._monitor_timeline_wrapped = True
        return True
    except Exception as exc:  # noqa: BLE001 - optional telemetry must not block rollout
        logger.debug("could not install rollout inference timeline: %s", exc)
        return False


def temporal_ensemble_poses(
    loaded: LoadedPolicy,
    fallback_joints: Mapping[str, float],
) -> list[dict[str, float]]:
    """Project ACT's pending temporal-ensemble actions for the rollout chart.

    ``ACT.select_action`` bypasses ``_action_queue`` when temporal ensembling is
    enabled.  Its ensembler keeps the already-blended future actions, so reading
    that tensor avoids a second ``predict_action_chunk`` call during rollout.
    """
    ensembler = getattr(loaded.policy, "temporal_ensembler", None)
    actions = getattr(ensembler, "ensembled_actions", None)
    if actions is None or getattr(actions, "ndim", None) != 3 or actions.shape[0] < 1:
        return []

    projected: list[dict[str, float]] = []
    try:
        for index in range(actions.shape[1]):
            action = actions[:, index, :]
            projected.append(_action_pose(loaded.postprocessor(action), loaded, fallback_joints))
    except Exception as exc:  # noqa: BLE001 - chart telemetry must not affect control
        logger.debug("could not project ACT temporal-ensemble actions: %s", exc)
    return projected


def sync_leftover_poses(
    loaded: LoadedPolicy,
    fallback_joints: Mapping[str, float],
) -> list[dict[str, float]]:
    """Read unread sync-policy actions without running another inference."""
    queue = _policy_action_queue(loaded.policy)
    if queue is None:
        return []
    pending = list(queue)
    if not pending:
        return []

    actions: list[dict[str, float]] = []
    for action in pending:
        try:
            actions.append(_action_pose(loaded.postprocessor(action), loaded, fallback_joints))
        except Exception as exc:  # noqa: BLE001 - chart telemetry must not affect control
            logger.debug("could not project sync queued action for the rollout chart: %s", exc)
            break
    return actions


def inference_leftover_poses(
    engine: Any,
    loaded: LoadedPolicy,
    fallback_joints: Mapping[str, float],
) -> list[dict[str, float]]:
    """Return future actions from the active LeRobot engine, without extra inference."""
    return (
        rtc_leftover_poses(engine, loaded, fallback_joints)
        or temporal_ensemble_poses(loaded, fallback_joints)
        or sync_leftover_poses(loaded, fallback_joints)
    )


def _action_pose(
    action: Any,
    loaded: LoadedPolicy,
    fallback_joints: Mapping[str, float],
) -> dict[str, float]:
    """Map one policy action tensor or mapping to a complete joint pose."""
    try:
        from lerobot.policies.utils import make_robot_action

        robot_action = make_robot_action(action, loaded.dataset_features)
    except Exception:
        if isinstance(action, dict):
            robot_action = {str(key): float(value) for key, value in action.items()}
        else:
            flat = action.squeeze(0).detach().cpu().tolist()
            robot_action = {
                key: float(flat[index])
                for index, key in enumerate(loaded.ordered_action_keys)
                if index < len(flat)
            }
    pose = observation_to_pose(robot_action)
    merged = {name: float(fallback_joints[name]) for name in JOINT_ORDER if name in fallback_joints}
    merged.update(pose)
    return merged


def _as_action_chunk_tensor(value: Any) -> Any:
    """Normalize a policy chunk result to a batched ``(B, T, A)`` tensor."""
    import torch

    from lerobot.utils.constants import ACTION

    if isinstance(value, Mapping):
        value = value.get(ACTION, value.get("action"))
    tensor = torch.as_tensor(value)
    if tensor.ndim == 2:
        tensor = tensor.unsqueeze(0)
    if tensor.ndim != 3:
        raise ValueError(f"expected action chunk shape (B, T, A), got {tuple(tensor.shape)}")
    if tensor.shape[0] < 1 or tensor.shape[1] < 1 or tensor.shape[2] < 1:
        raise ValueError(f"action chunk must be non-empty, got {tuple(tensor.shape)}")
    return tensor


def tensor_steps(value: Any) -> int | None:
    """Count action steps in a policy chunk without leaking conversion errors."""
    try:
        return int(_as_action_chunk_tensor(value).shape[1])
    except Exception:  # noqa: BLE001 - telemetry must not affect inference
        return None


def predict_action_chunk(
    loaded: LoadedPolicy,
    joints: Mapping[str, float],
    images_rgb: Mapping[str, np.ndarray],
    chunk_size: int,
    *,
    reset: bool = True,
    allow_sequential: bool = True,
) -> ActionChunk:
    """Predict up to ``chunk_size`` actions from one observation.

    The native path calls ``policy.predict_action_chunk`` and expects a
    ``(B, T, A)`` tensor before the postprocessor. Policies without that API,
    or whose chunk result is malformed, fall back to repeated
    ``select_action`` calls and report ``degraded=True``.

    ``reset=False`` keeps a policy that is already executing (rollout) intact:
    clearing its action queue mid-episode would change the actions it sends.
    ``allow_sequential=False`` refuses the multi-step fallback, which is far too
    slow to run alongside a live control loop.
    """
    import torch

    from lerobot.policies.utils import prepare_observation_for_inference
    from lerobot.utils.constants import ACTION, OBS_STR

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    state = np.array([float(joints[name]) for name in JOINT_ORDER if name in joints], dtype=np.float32)
    observation: dict[str, np.ndarray] = {f"{OBS_STR}.state": state}
    for cam_name, rgb in images_rgb.items():
        observation[f"{OBS_STR}.images.{cam_name}"] = np.ascontiguousarray(rgb)

    device = torch.device(loaded.device if torch.cuda.is_available() or loaded.device == "cpu" else "cpu")
    warnings: list[str] = []
    with torch.inference_mode():
        if reset:
            loaded.reset()
        prepared = prepare_observation_for_inference(
            observation, device, loaded.task, loaded.robot_type
        )
        prepared = loaded.preprocessor(prepared)
        chunk_method = getattr(loaded.policy, "predict_action_chunk", None)
        if callable(chunk_method):
            try:
                raw_chunk = chunk_method(prepared)
                chunk = _as_action_chunk_tensor(raw_chunk)[:, :chunk_size, :]
                actions = [
                    _action_pose(loaded.postprocessor(chunk[:, index, :]), loaded, joints)
                    for index in range(chunk.shape[1])
                ]
                if actions:
                    return ActionChunk(
                        actions=actions,
                        strategy="policy_chunk",
                        degraded=False,
                        warnings=warnings,
                    )
                warnings.append("policy returned an empty action chunk")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"native action chunk unavailable: {exc}")
        else:
            warnings.append("policy does not implement predict_action_chunk")

        if not allow_sequential:
            raise RuntimeError("; ".join(warnings) or "no action chunk available")
        if reset:
            loaded.reset()
        actions = []
        for _ in range(chunk_size):
            action = loaded.policy.select_action(prepared)
            actions.append(_action_pose(loaded.postprocessor(action), loaded, joints))
    return ActionChunk(
        actions=actions,
        strategy="sequential_select_action",
        degraded=True,
        warnings=warnings,
    )
