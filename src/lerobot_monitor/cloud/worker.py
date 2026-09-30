"""An isolated, serialized model worker with bounded JSON-only IPC."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import subprocess
import sys
import struct
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, BinaryIO

MAX_MESSAGE_BYTES = 40 * 1024 * 1024
PROTOCOL_VERSION = 1


def worker_code_hash() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


WORKER_CODE_HASH = worker_code_hash()
WORKER_BOOTSTRAP = (
    "import importlib.util,sys,runpy; "
    "p=sys.argv[1]; "
    "s=importlib.util.spec_from_file_location('lerobot_monitor',p+'/__init__.py',submodule_search_locations=[p]); "
    "m=importlib.util.module_from_spec(s); sys.modules['lerobot_monitor']=m; s.loader.exec_module(m); "
    "sys.argv=['lerobot_monitor.cloud.worker']; runpy.run_module('lerobot_monitor.cloud.worker',run_name='__main__')"
)


class WorkerError(RuntimeError):
    """A failed or unavailable isolated policy process."""


class SubprocessWorker:
    """Only this process owns its child; CUDA visibility is set before imports."""

    def __init__(self, path: str, gpu_uuid: str, device: str, log_path: Path,
                 *, startup_timeout: float = 600.0, command: list[str] | None = None,
                 python: str | None = None, hf_home: str | None = None) -> None:
        self._lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=2)
        self._log: BinaryIO = log_path.open("ab", buffering=0)
        environment = os.environ.copy()
        environment["CUDA_VISIBLE_DEVICES"] = gpu_uuid if device == "cuda" else ""
        environment["PYTHONUNBUFFERED"] = "1"
        environment["LEROBOT_CLOUD_PARENT_PID"] = str(os.getpid())
        package_path = str(Path(__file__).resolve().parents[1])
        python = environment.get("LEROBOT_CLOUD_RUNTIME_PYTHON") or python or sys.executable
        if hf_home:
            environment["HF_HOME"] = hf_home
        try:
            self.process = subprocess.Popen(
                command or [python, "-u", "-c", WORKER_BOOTSTRAP, package_path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log,
                env=environment, bufsize=0,
            )
        except BaseException:
            self._log.close()
            raise
        threading.Thread(target=self._read, name="cloud-worker-output", daemon=True).start()
        try:
            self.metadata = self.call("load", {"path": path, "device": device}, timeout=startup_timeout)
        except BaseException:
            self.stop()
            raise

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            while line := self.process.stdout.readline(MAX_MESSAGE_BYTES + 1):
                if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
                    raise WorkerError("worker response exceeds message limit")
                value = json.loads(line)
                self._responses.put_nowait(value)
        except Exception as exc:
            try:
                self._responses.put_nowait({"ok": False, "error": f"worker protocol failed: {type(exc).__name__}"})
            except queue.Full:
                pass
        finally:
            try:
                self._responses.put_nowait({"ok": False, "error": "model worker exited"})
            except queue.Full:
                pass

    def alive(self) -> bool:
        return self.process.poll() is None

    def call(self, operation: str, payload: dict[str, Any], *, timeout: float = 120.0) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise WorkerError("model worker already has an in-flight operation")
        try:
            if not self.alive():
                raise WorkerError("model worker is not running")
            began = time.perf_counter()
            encoded = (json.dumps({"operation": operation, "payload": payload}, allow_nan=False) + "\n").encode()
            if len(encoded) > MAX_MESSAGE_BYTES:
                raise ValueError("worker request exceeds message limit")
            assert self.process.stdin is not None
            # Writing a large camera frame can itself block if the child died or hung.
            sent: queue.Queue[Exception | None] = queue.Queue(maxsize=1)

            def send() -> None:
                try:
                    assert self.process.stdin is not None
                    self.process.stdin.write(encoded)
                    self.process.stdin.flush()
                    sent.put_nowait(None)
                except Exception as exc:
                    sent.put_nowait(exc)

            deadline = time.monotonic() + timeout
            threading.Thread(target=send, name="cloud-worker-send", daemon=True).start()
            try:
                error = sent.get(timeout=max(0.001, deadline - time.monotonic()))
                if error is not None:
                    raise WorkerError("model worker input closed") from error
                response = self._responses.get(timeout=max(0.001, deadline - time.monotonic()))
            except queue.Empty as exc:
                self.stop()
                raise WorkerError("model worker timed out and was terminated") from exc
            if not response.get("ok"):
                raise WorkerError(str(response.get("error", "model operation failed")))
            if response.get("protocol_version") != PROTOCOL_VERSION or response.get("worker_code_hash") != WORKER_CODE_HASH:
                self.stop()
                raise WorkerError("incompatible worker protocol/build; update the cloud runtime")
            result = response["result"]
            if operation == "infer" and isinstance(result, dict):
                # Serializing the payload, writing it through the pipe and reading the
                # answer back: the parent's share of one worker round trip.
                timings = result.setdefault("timings", {})
                timings["ipc"] = round((time.perf_counter() - began) * 1000.0, 3)
            return result
        finally:
            self._lock.release()

    def stop(self) -> None:
        """Terminate only the child created by this instance; never search by name."""
        with self._stop_lock:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            self._log.close()


def decode_images(values: dict[str, str]) -> dict[str, Any]:
    """Decode lossless PNGs into RGB arrays [H,W,3], bounded before decompression."""
    import cv2
    import numpy as np

    result: dict[str, Any] = {}
    pixels = 0
    for name, value in values.items():
        raw = base64.b64decode(value, validate=True)
        if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) < 33 or raw[12:16] != b"IHDR":
            raise ValueError("images must use PNG encoding")
        width, height = struct.unpack(">II", raw[16:24])
        pixels += width * height
        if not width or not height or width > 4096 or height > 4096 or pixels > 16_777_216:
            raise ValueError("images exceed decoded pixel limit")
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.shape[:2] != (height, width):
            raise ValueError("invalid PNG image")
        result[name] = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    return result


class NativePolicyBackend:
    """Preserve native processors and policy state without owning an action queue."""

    def __init__(self) -> None:
        self.loaded: Any = None
        self.session: dict[str, Any] | None = None
        self.metadata: dict[str, Any] = {}

    def load(self, payload: dict[str, Any]) -> dict[str, Any]:
        from lerobot_monitor.policy import load_policy

        self.loaded = load_policy(payload["path"], device=payload["device"])
        from lerobot.rollout.inference.rtc import supports_rtc_inference
        from lerobot.processor import RelativeActionsProcessorStep
        import importlib.metadata

        config = self.loaded.policy.config
        action_feature = config.output_features.get("action")
        action_dim = int(action_feature.shape[-1]) if action_feature is not None else len(self.loaded.ordered_action_keys)
        names = getattr(config, "action_feature_names", None) or self.loaded.ordered_action_keys
        if len(names) != action_dim:
            names = [f"action_{index}" for index in range(action_dim)]
        state_feature = config.input_features.get("observation.state")
        state_dim = int(state_feature.shape[-1]) if state_feature is not None else action_dim
        state_names = getattr(state_feature, "names", None)
        if not state_names:
            state_names = names if state_dim == action_dim else [f"state_{index}" for index in range(state_dim)]
        try:
            version = importlib.metadata.version("lerobot")
        except importlib.metadata.PackageNotFoundError:
            version = "source-checkout"
        relative_actions = any(isinstance(step, RelativeActionsProcessorStep) and step.enabled
                               for step in self.loaded.preprocessor.steps)
        self.metadata = {
            "policy_type": config.type, "lerobot_version": version,
            "action_keys": list(names), "action_dim": action_dim,
            "state_keys": list(state_names), "state_dim": state_dim,
            "image_keys": [name for name in config.input_features if name.startswith("observation.images.")],
            "wire_dtype": "float32", "relative_actions": relative_actions,
            "rtc_training_max_delay": int(getattr(config, "rtc_training_max_delay", 0)),
            "capabilities": {"select_action": callable(getattr(self.loaded.policy, "select_action", None)) and not relative_actions,
                             "debug_chunk": callable(getattr(self.loaded.policy, "predict_action_chunk", None)),
                             "rtc_chunk": supports_rtc_inference(self.loaded.policy)},
        }
        return self.metadata

    def open(self, payload: dict[str, Any]) -> dict[str, Any]:
        from lerobot_monitor.policy import apply_requested_overrides, inference_config_from_extra

        if not self.metadata["capabilities"].get(payload["mode"]):
            raise ValueError(f"policy does not support {payload['mode']}")
        allowed = {"policy.n_action_steps", "policy.num_steps", "policy.num_inference_steps",
                   "policy.temporal_ensemble_coeff", "policy.use_amp"}
        overrides = payload.get("overrides", {})
        for key in overrides:
            if key not in allowed and not key.startswith("inference.rtc."):
                raise ValueError(f"unsupported session override: {key}")
        apply_requested_overrides(self.loaded, overrides)
        config = self.loaded.policy.config
        if payload["mode"] == "rtc_chunk":
            rtc = inference_config_from_extra({**overrides, "inference.type": "rtc"}).rtc
            if not 1 <= rtc.execution_horizon <= 1024:
                raise ValueError("RTC execution_horizon must be between 1 and 1024")
            if rtc.mode == "trained" and self.metadata["rtc_training_max_delay"] <= 0:
                raise ValueError("trained RTC requires a checkpoint trained with rtc_training_max_delay > 0")
            config.rtc_config = rtc
        elif hasattr(config, "rtc_config"):
            config.rtc_config = None
        if hasattr(self.loaded.policy, "init_rtc_processor"):
            self.loaded.policy.init_rtc_processor()
        self.loaded.reset()
        keys = payload.get("state_keys") or self.metadata["state_keys"]
        if len(keys) != self.metadata["state_dim"]:
            raise ValueError("state_keys length differs from model observation.state")
        self.session = {**payload, "state_keys": keys}
        return {**self.metadata, "state_keys": keys}

    def reset(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.loaded.reset()
        return {}

    def close(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.loaded.reset()
        self.session = None
        return {}

    def infer(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Input state [S] and RGB [H,W,3]; raw/absolute outputs [T,A] on CPU."""
        import numpy as np
        import torch
        from lerobot.policies.utils import prepare_observation_for_inference

        from lerobot_monitor.timing import Timing

        if self.session is None:
            raise ValueError("no active worker session")
        timings = Timing()
        started = time.monotonic()
        state_keys = self.session["state_keys"]
        if set(payload["state"]) != set(state_keys):
            raise ValueError("state keys must exactly match session state_keys")
        # Everything the request body costs on this side: state array plus base64/PNG
        # decoding of each camera frame.
        with timings.span("decode"):
            state = np.array([payload["state"][key] for key in state_keys], dtype=np.float32)
            if not np.isfinite(state).all():
                raise ValueError("state must remain finite in float32")
            observation: dict[str, Any] = {"observation.state": state}
            for name, value in decode_images(payload.get("images", {})).items():
                key = name if name.startswith("observation.images.") else f"observation.images.{name}"
                observation[key] = value
        missing = set(self.metadata["image_keys"]) - observation.keys()
        if missing:
            raise ValueError(f"missing required images: {sorted(missing)}")
        task = payload.get("task") if payload.get("task") is not None else self.session["task"]
        mode = self.session["mode"]
        if mode == "debug_chunk":
            self.loaded.reset()
        device = torch.device(self.loaded.device)
        amp = torch.autocast(device_type=device.type) if device.type == "cuda" and getattr(self.loaded.policy.config, "use_amp", False) else nullcontext()
        # Guided RTC temporarily enables autograd for prefix corrections. Unlike
        # inference_mode, no_grad allows the native RTC processor to do that.
        gradients = torch.no_grad() if mode == "rtc_chunk" else torch.inference_mode()
        with gradients, amp:
            if mode == "select_action" and task != self.session["task"]:
                self.loaded.policy.drop_queued_actions()
            with timings.span("prepare"):
                prepared = prepare_observation_for_inference(observation, device, task, self.loaded.robot_type)
                prepared = self.loaded.preprocessor(prepared)
            with timings.span("policy"):
                if mode == "select_action":
                    # Native select_action retains temporal ensemble/history across requests.
                    raw = self.loaded.policy.select_action(prepared)
                    if raw.ndim != 2 or raw.shape[0] != 1:
                        raise ValueError("selected action must have shape [1,A]")
                    original = raw.clone()
                    processed = self.loaded.postprocessor(raw)
                    raw = original.reshape(1, 1, -1)
                    processed = processed.reshape(1, 1, -1)
                    queued_actions = self._queued_actions(torch)
                else:
                    kwargs: dict[str, Any] = {}
                    if mode == "rtc_chunk":
                        prefix = self._rtc_prefix(payload, device)
                        delay = payload["inference_delay"]
                        rtc = self.loaded.policy.config.rtc_config
                        if rtc.mode == "trained" and (delay > self.metadata["rtc_training_max_delay"]
                                or delay > (len(payload.get("prefix_raw") or []))):
                            raise ValueError("trained RTC inference_delay exceeds checkpoint or available prefix")
                        kwargs = {"inference_delay": delay, "prev_chunk_left_over": prefix}
                    raw = self.loaded.policy.predict_action_chunk(prepared, **kwargs)
                    if isinstance(raw, dict):
                        raw = raw["action"]
                    if raw.ndim != 3 or raw.shape[0] != 1:
                        raise ValueError("policy chunk must have shape [1,T,A]")
                    if mode == "debug_chunk":
                        generated_steps = int(raw.shape[1])
                        raw = raw[:, :payload["chunk_size"], :]
                    original = raw.clone()
                    processed = self.loaded.postprocessor(raw)
                    raw = original
            with timings.span("emit"):
                for tensor in (raw, processed):
                    if tensor.ndim != 3 or tensor.shape[0] != 1 or not 1 <= tensor.shape[1] <= 1024:
                        raise ValueError("actions must have shape [1,T,A], 1 <= T <= 1024")
                    if tensor.shape[2] != self.metadata["action_dim"] or not torch.isfinite(tensor).all().item():
                        raise ValueError("actions must be finite and match model action_dim")
                if raw.shape != processed.shape:
                    raise ValueError("raw and processed action shapes differ")
                raw_array = raw.squeeze(0).detach().to(device="cpu", dtype=torch.float32).tolist()
                actions = processed.squeeze(0).detach().to(device="cpu", dtype=torch.float32).tolist()
            self.session["task"] = task
        result = {"raw_actions": raw_array, "actions": actions, "action_keys": self.metadata["action_keys"],
                  "shape": [len(actions), self.metadata["action_dim"]], "compute_seconds": time.monotonic() - started,
                  "timings": timings.as_dict()}
        if mode == "select_action":
            result["queued_actions"] = queued_actions
        elif mode == "debug_chunk":
            # Steps the policy generated before the request's chunk_size truncated them.
            result["generated_steps"] = generated_steps
        return result

    def _queued_actions(self, torch: Any) -> list[list[float]]:
        """Processed actions the policy still holds after ``select_action``, for chart previews.

        The client only sees one action per request, so it cannot see the rest of
        the server-side queue that a local rollout would draw as the dashed line.
        Failures here must never affect control, so they yield an empty preview.
        """
        policy = self.loaded.policy
        try:
            ensembled = getattr(getattr(policy, "temporal_ensembler", None), "ensembled_actions", None)
            if getattr(ensembled, "ndim", None) == 3 and ensembled.shape[0] >= 1:
                pending = [ensembled[:, index, :] for index in range(ensembled.shape[1])]
            else:
                pending = []
                for attr in getattr(policy, "_action_queue_attrs", ("_queues", "_action_queue")):
                    queue_ = getattr(policy, attr, None)
                    if isinstance(queue_, dict):
                        queue_ = queue_.get("action")
                    if isinstance(queue_, (list, tuple)) or hasattr(queue_, "popleft"):
                        pending = list(queue_)
                        break
            if not pending:
                # Policies with n_action_steps=1 (e.g. SmolVLA checkpoints) drain the
                # queue with the very pop that returned the current action, so the
                # preview can only come from the recorded chunk producer output.
                from lerobot_monitor.policy import predicted_chunk_tail

                tail = predicted_chunk_tail(policy)
                if tail is not None:
                    pending = [tail[:, index, :] for index in range(int(tail.shape[1]))]
            rows: list[list[float]] = []
            for action in pending[:1024]:
                processed = self.loaded.postprocessor(action.clone())
                row = processed.detach().to(device="cpu", dtype=torch.float32).reshape(-1).tolist()
                if len(row) != self.metadata["action_dim"]:
                    break
                rows.append(row)
            return rows
        except Exception as exc:  # noqa: BLE001 - chart telemetry must not affect control
            print(f"queued action preview skipped: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return []

    def _rtc_prefix(self, payload: dict[str, Any], device: Any) -> Any:
        """Re-anchor absolute prefix [T,A] to current state, then normalize length."""
        import torch
        from lerobot.policies.rtc import reanchor_relative_rtc_prefix
        from lerobot.processor import NormalizerProcessorStep, RelativeActionsProcessorStep
        from lerobot.rollout.inference.rtc import _normalize_prev_actions_length

        if payload.get("prefix_raw") is None:
            return None
        prefix = torch.tensor(payload["prefix_raw"], dtype=torch.float32, device=device)
        absolute = torch.tensor(payload["prefix_absolute"], dtype=torch.float32, device=device)
        if prefix.shape[1] != self.metadata["action_dim"]:
            raise ValueError("RTC prefix action dimension differs from model")
        steps = self.loaded.preprocessor.steps
        relative = next((step for step in steps if isinstance(step, RelativeActionsProcessorStep) and step.enabled), None)
        normalizer = next((step for step in steps if isinstance(step, NormalizerProcessorStep)), None)
        if relative is not None:
            if relative.action_names is None:
                relative.action_names = self.metadata["action_keys"]
            state = relative.get_cached_state()
            if state is None:
                raise ValueError("relative action processor did not cache current observation")
            prefix = reanchor_relative_rtc_prefix(prev_actions_absolute=absolute, current_state=state,
                relative_step=relative, normalizer_step=normalizer, policy_device=device)
        return _normalize_prev_actions_length(prefix, target_steps=self.loaded.policy.config.rtc_config.execution_horizon)


def _watch_parent() -> None:
    """Exit when the parent disappears, including while native GPU code is blocked."""
    expected = int(os.environ.get("LEROBOT_CLOUD_PARENT_PID", "0"))
    if not expected:
        return
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        handle = kernel.OpenProcess(0x00100000, False, expected)
        if not handle:
            os._exit(1)
        kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
        os._exit(1)
    while True:
        if os.getppid() != expected:
            os._exit(1)
        time.sleep(1)


def main() -> None:
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    # Libraries frequently print during loading. Keep protocol stdout pristine.
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    threading.Thread(target=_watch_parent, name="parent-watchdog", daemon=True).start()
    backend = NativePolicyBackend()
    while line := sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1):
        if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
            break
        try:
            message = json.loads(line)
            operation = message["operation"]
            if operation not in {"load", "open", "reset", "close", "infer"}:
                raise ValueError("unknown worker operation")
            result = getattr(backend, operation)(message["payload"])
            response = {"ok": True, "result": result}
        except Exception as exc:
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        response.update(protocol_version=PROTOCOL_VERSION, worker_code_hash=WORKER_CODE_HASH)
        protocol.write(json.dumps(response, allow_nan=False) + "\n")
        protocol.flush()


if __name__ == "__main__":
    main()
