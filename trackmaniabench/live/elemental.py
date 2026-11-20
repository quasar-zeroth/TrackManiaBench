"""Optional Torch adapter for Elemental's ``BaseVecEnv`` contract.

Elemental does not need to be installed to use this module. The adapter follows
its reset/step/specification protocol, and imports Torch only on construction.
There is one independently controlled game instance per adapter.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .types import Action


class ElementalTrackManiaEnv:
    """Expose a live environment as a single-element Torch vector environment.

    Elemental's PPO collector expects automatic episode resets and combines
    termination and truncation in ``done``. Terminal observations and the two
    individual flags are preserved in ``info``. Its stock GAE discounts each
    decision equally; the timing observations do not change that calculation.

    Actions are ``[1, 3]`` in steering, throttle, brake order. Controls must
    already be in their supported ranges. No clipping or Gaussian-policy
    transformation is hidden in this adapter.
    """

    num_envs = 1
    action_dim = 3
    num_action_chunks = 1

    def __init__(
        self,
        env=None,
        *,
        backend=None,
        backend_kind: str = "realtime",
        backend_kwargs: dict[str, Any] | None = None,
        env_kwargs: dict[str, Any] | None = None,
        text_prompt: str = "Drive the Trackmania circuit to the finish.",
        image_size: tuple[int, int] = (224, 224),
        num_envs: int = 1,
        device="cpu",
    ):
        try:
            import torch
        except ImportError as error:
            raise ImportError("ElementalTrackManiaEnv requires the optional 'torch' package") from error
        if num_envs != 1:
            raise ValueError("Each adapter controls one game instance; num_envs must be 1")
        if len(image_size) != 2 or any(
            isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in image_size
        ):
            raise ValueError("image_size must contain positive integer height and width")
        if not isinstance(text_prompt, str):
            raise TypeError("text_prompt must be a string")
        if env is not None and (backend is not None or backend_kwargs or env_kwargs):
            raise ValueError("Pass an existing env or backend construction options, not both")
        if backend is not None and backend_kwargs:
            raise ValueError("backend_kwargs cannot be used with an existing backend")

        if env is None:
            from .environment import LiveTrackManiaEnv

            if backend is None:
                if backend_kind == "realtime":
                    from .tmrl import TMRLBackend

                    backend = TMRLBackend(**dict(backend_kwargs or {}))
                elif backend_kind == "tickgym":
                    from .tickgym_backend import TickGymBackend

                    backend = TickGymBackend(**dict(backend_kwargs or {}))
                else:
                    raise ValueError("backend_kind must be 'realtime' or 'tickgym'")
            try:
                env = LiveTrackManiaEnv(backend, **dict(env_kwargs or {}))
            except BaseException:
                backend.close()
                raise

        self.env = env
        self._torch = torch
        self.device = torch.device(device)
        self.image_size = tuple(image_size)
        self.text_prompt = text_prompt
        self.capabilities = env.backend.capabilities
        self.observation_spec = {
            "telemetry": {"shape": (12,), "dtype": torch.float32},
            "timing": {"shape": (4,), "dtype": torch.float32},
            "previous_action": {"shape": (3,), "dtype": torch.float32},
            "applied_action": {"shape": (3,), "dtype": torch.float32},
            "text": {"dtype": "str"},
        }
        if self.capabilities.rgb:
            self.observation_spec["image"] = {"shape": (3, *self.image_size), "dtype": torch.float32}
        self.action_spec = {
            "shape": (3,),
            "dtype": torch.float32,
            "low": (-1.0, 0.0, 0.0),
            "high": (1.0, 1.0, 1.0),
            "names": ("steer", "throttle", "brake"),
        }
        self.reset_info: dict[str, Any] | None = None
        self._last_image = None
        self._closed = False

    def _observation(self, observation):
        torch = self._torch
        result = {}
        for name, length in (("telemetry", 12), ("timing", 4), ("previous_action", 3), ("applied_action", 3)):
            values = np.asarray(observation[name])
            if values.shape != (length,) or not np.isfinite(values).all():
                raise ValueError(f"{name} must be a finite vector of length {length}")
            # torch.tensor makes an owning copy: the Elemental rollout buffer
            # stores observations by reference until its minibatch conversion.
            result[name] = torch.tensor(values, dtype=torch.float32, device=self.device).unsqueeze(0)
        result["text"] = [self.text_prompt]
        image = observation.get("image")
        if self.capabilities.rgb:
            if image is None:
                raise ValueError("RGB backend returned no image")
            image = np.asarray(image)
            if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3 or min(image.shape[:2]) <= 0:
                raise ValueError("RGB image must be a non-empty uint8 HWC array with three channels")
            tensor = (
                torch.tensor(image, dtype=torch.float32, device=self.device).permute(2, 0, 1).unsqueeze(0)
            )
            tensor = tensor / 255.0
            if tuple(tensor.shape[-2:]) != self.image_size:
                tensor = torch.nn.functional.interpolate(
                    tensor, size=self.image_size, mode="bilinear", align_corners=False
                )
            result["image"] = tensor.contiguous()
            self._last_image = result["image"].clone()
        else:
            self._last_image = None
        return result

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("Environment is closed")

    def reset(self):
        """Reset the game and return a fresh batched observation dictionary."""
        self._ensure_open()
        try:
            observation, info = self.env.reset()
            self.reset_info = dict(info)
            return self._observation(observation)
        except BaseException as error:
            self._close_after_failure(error)
            raise

    def step(self, actions):
        """Apply one control vector and automatically reset completed episodes."""
        self._ensure_open()
        torch = self._torch
        try:
            if not isinstance(actions, torch.Tensor):
                raise TypeError("Elemental actions must be a torch.Tensor of shape [1, 3]")
            if tuple(actions.shape) != (1, 3):
                raise ValueError("Elemental actions must have shape [1, 3]; execute a single chunk row")
            if not actions.is_floating_point():
                raise TypeError("Elemental actions must have a real floating-point dtype")
            action = Action.from_array(actions.detach().to(device="cpu", dtype=torch.float64).numpy()[0])
            observation, reward, terminated, truncated, raw_info = self.env.step(action)
            if raw_info.get("transition_valid", True) is False:
                raise RuntimeError(
                    "Invalid control transition: refusing insertion into Elemental's rollout buffer"
                )
            if not np.isfinite(reward):
                raise ValueError("Environment returned a non-finite reward")
            terminal_observation = self._observation(observation)
        except BaseException as error:
            self._close_after_failure(error)
            raise
        done = bool(terminated or truncated)
        info = dict(raw_info)
        info["raw_info"] = [dict(raw_info)]
        info["success"] = torch.tensor(
            [bool(raw_info.get("success", False))], dtype=torch.bool, device=self.device
        )
        info["terminated"] = torch.tensor([bool(terminated)], dtype=torch.bool, device=self.device)
        info["truncated"] = torch.tensor([bool(truncated)], dtype=torch.bool, device=self.device)
        next_observation = terminal_observation
        if done:
            info["final_observation"] = terminal_observation
            info["final_info"] = dict(raw_info)
            next_observation = self.reset()
            info["reset_info"] = dict(self.reset_info or {})
        return (
            next_observation,
            torch.tensor([float(reward)], dtype=torch.float32, device=self.device),
            torch.tensor([done], dtype=torch.bool, device=self.device),
            info,
        )

    def render(self):
        """Return the last observation image as NCHW, or None without RGB."""
        self._ensure_open()
        return None if self._last_image is None else self._last_image.to(self.device).clone()

    def close(self):
        """Release controls and close the owned environment, once."""
        if not self._closed:
            self._closed = True
            self._last_image = None
            self.env.close()

    def __enter__(self):
        self._ensure_open()
        return self

    def _close_after_failure(self, error):
        try:
            self.close()
        except BaseException as cleanup_error:
            error.add_note(f"Environment cleanup also failed: {cleanup_error}")

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            self.close()
        else:
            self._close_after_failure(exc)
