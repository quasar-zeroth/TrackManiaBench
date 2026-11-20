"""Framework-independent environments with explicit control-clock semantics."""

from __future__ import annotations

from dataclasses import dataclass
import math
import time

import numpy as np

from .types import Action, ActionLeaseExpired, Backend, Sample


@dataclass(frozen=True)
class FinishReward:
    """Sparse finish reward with elapsed-time cost; no invented route progress."""

    finish_bonus: float = 1.0
    time_cost: float = 0.01

    def __call__(self, previous: Sample, current: Sample, info: dict) -> float:
        elapsed = info["game_dt_s"] if info["game_dt_s"] is not None else info["wall_dt_s"]
        return float(self.finish_bonus * info["success"] - self.time_cost * elapsed)


class LiveTrackManiaEnv:
    """One game/session, returning a Gym-style five-tuple without a Gym dependency.

    Realtime keeps the previous controls held while Python/the policy is busy.
    ``control_hz`` specifies the minimum hold after sending the new controls; it
    does not change Trackmania's clock. Lockstep advances a whole number of
    acknowledged simulation ticks and never sleeps to simulate a game clock.
    """

    def __init__(
        self,
        backend: Backend,
        control_hz: float = 20.0,
        max_episode_seconds: float = 120.0,
        max_steps: int | None = None,
        reward_fn=None,
    ):
        if not math.isfinite(control_hz) or not 0 < control_hz <= 1000:
            raise ValueError("control_hz must be finite and in (0, 1000]")
        if not math.isfinite(max_episode_seconds) or max_episode_seconds <= 0:
            raise ValueError("max_episode_seconds must be positive and finite")
        if max_steps is not None and (type(max_steps) is not int or max_steps <= 0):
            raise ValueError("max_steps must be a positive integer")
        self.backend = backend
        self.control_hz = float(control_hz)
        self.max_episode_seconds = float(max_episode_seconds)
        self.max_steps = max_steps
        self.reward_fn = FinishReward() if reward_fn is None else reward_fn
        self.mode = backend.capabilities.clock_mode
        self.ticks_per_step = None
        if self.mode == "lockstep":
            ticks = 1.0 / (control_hz * backend.capabilities.tick_seconds)
            if not math.isclose(ticks, round(ticks), rel_tol=0, abs_tol=1e-8):
                raise ValueError("control_hz must correspond to a whole number of simulation ticks")
            self.ticks_per_step = round(ticks)
            if not 1 <= self.ticks_per_step <= 60000:
                raise ValueError("Step must contain 1..60000 simulation ticks")
        self._last = None
        self._action = Action()
        self._episode_seconds = 0.0
        self._steps = 0
        self._done = False
        self._closed = False

    def reset(self, seed: int | None = None):
        self._require_open()
        try:
            sample = self.backend.reset(seed=seed)
            self._validate(sample)
            self._require_released_controls(sample)
            if self._transition_invalid(sample):
                raise RuntimeError("Backend reset returned an invalid control state")
            if sample.telemetry.finished:
                raise RuntimeError("Backend reset returned a finished race")
            self._last = sample
            self._action = Action()
            self._episode_seconds = 0.0
            self._steps = 0
            self._done = False
            info = self._info(
                sample, 0.0, 0.0 if self.mode == "lockstep" else None, 0.0, Action(), Action(), Action()
            )
            return self._observation(sample, info), info
        except BaseException as error:
            self._close_after_failure(error)
            raise

    def step(self, action):
        self._require_open()
        if self._last is None or self._done:
            raise RuntimeError("Call reset before stepping a new/finished episode")
        previous = self._last
        previous_action = self._action
        sent_ns = time.monotonic_ns()
        try:
            # Validation is part of the protected control operation: a bad
            # policy output must not leave the previous action held in game.
            requested = action if isinstance(action, Action) else Action.from_array(action)
            if self.mode == "realtime":
                try:
                    applied = self.backend.apply_action(requested)
                except ActionLeaseExpired:
                    # The watchdog already released the previous controls.
                    # Return its invalid transition without applying a new one.
                    applied, written_ns = Action(), None
                else:
                    written_ns = time.monotonic_ns()
                    deadline = time.monotonic() + 1.0 / self.control_hz
                    while (remaining := deadline - time.monotonic()) > 0:
                        time.sleep(remaining)
                sample = self.backend.observe()
                if written_ns is None and not sample.metadata.get("action_lease_expired"):
                    raise RuntimeError("Backend omitted the expired action lease fault")
            else:
                sample, applied = self.backend.advance(requested, self.ticks_per_step)
                written_ns = sent_ns
            if not isinstance(applied, Action):
                raise RuntimeError("Backend must report the controls it actually requested from the device")
            self._validate(sample)
            self._require_released_controls(sample)
            if sample.sequence <= previous.sequence or sample.captured_ns < previous.captured_ns:
                raise RuntimeError("Backend returned a stale/out-of-order observation")
            wall_dt = (sample.captured_ns - previous.captured_ns) / 1e9
            game_dt = None
            if self.mode == "lockstep":
                game_dt = sample.metadata.get("simulation_dt_s")
                if game_dt is None:
                    before, after = previous.telemetry.race_time_s, sample.telemetry.race_time_s
                    if before is None or after is None:
                        raise RuntimeError("Lockstep backend omitted acknowledged simulation timing")
                    game_dt = after - before
                game_dt = float(game_dt)
                requested_dt = self.ticks_per_step * self.backend.capabilities.tick_seconds
                if not math.isfinite(game_dt) or not 0 <= game_dt <= requested_dt + 1e-8:
                    raise RuntimeError("Invalid acknowledged simulation duration")
                if game_dt < requested_dt - 1e-8 and not sample.telemetry.finished:
                    raise RuntimeError("Nonterminal step advanced fewer ticks than requested")
            delay = (
                max(0.0, (written_ns - previous.captured_ns) / 1e9)
                if self.mode == "realtime" and written_ns is not None
                else 0.0
            )
            info = self._info(sample, wall_dt, game_dt, delay, requested, applied, previous_action)
            info.update(
                input_send_started_ns=sent_ns,
                input_write_completed_ns=written_ns,
                requested_action_written=written_ns is not None,
                previous_capture_ns=previous.captured_ns,
                input_timing_basis="host device write, not physics acknowledgement"
                if self.mode == "realtime"
                else "acknowledged simulation ticks",
            )
            self._steps += 1
            self._episode_seconds += game_dt if game_dt is not None else wall_dt
            lease_expired = bool(sample.metadata.get("action_lease_expired", False))
            control_fault = sample.metadata.get("control_fault")
            invalid_transition = self._transition_invalid(sample)
            terminated = bool(sample.telemetry.finished) and not invalid_transition
            truncated = not terminated and (
                self._episode_seconds >= self.max_episode_seconds
                or (self.max_steps is not None and self._steps >= self.max_steps)
                or lease_expired
                or invalid_transition
            )
            reason = (
                "finish"
                if terminated
                else (
                    "action_lease_expired"
                    if lease_expired
                    else str(control_fault or "invalid_transition")
                    if invalid_transition
                    else "time_limit"
                    if self._episode_seconds >= self.max_episode_seconds
                    else "step_limit"
                    if truncated
                    else None
                )
            )
            info.update(
                terminated=terminated,
                truncated=truncated,
                terminal_reason=reason,
                episode_seconds=self._episode_seconds,
                episode_steps=self._steps,
                success=terminated,
                transition_valid=not invalid_transition,
            )
            reward = float(self.reward_fn(previous, sample, info))
            if not math.isfinite(reward):
                raise ValueError("Reward must be finite")
            self._last, self._action, self._done = sample, applied, terminated or truncated
            # Stop a real car during policy optimization/terminal handling.
            if self._done and self.mode == "realtime" and not control_fault:
                self.backend.apply_action(Action())
            return self._observation(sample, info), reward, terminated, truncated, info
        except BaseException as error:
            self._close_after_failure(error)
            raise

    def _info(self, sample, wall_dt, game_dt, delay, requested, applied, previous):
        return {
            "clock_mode": self.mode,
            "success": bool(sample.telemetry.finished),
            "sequence": sample.sequence,
            "captured_ns": sample.captured_ns,
            "wall_dt_s": wall_dt,
            "game_dt_s": game_dt,
            "action_delay_s": delay,
            "requested_action": requested.array().tolist(),
            "applied_action": applied.array().tolist(),
            "previous_action": previous.array().tolist(),
            "action_profile": self.backend.capabilities.action_profile,
            "speed_unit": sample.telemetry.speed_unit,
            "backend": dict(sample.metadata),
            "telemetry_extra": dict(sample.telemetry.extra),
        }

    @staticmethod
    def _observation(sample, info):
        t = sample.telemetry
        obs = {
            "telemetry": np.array(
                [
                    t.speed,
                    *t.position,
                    t.race_time_s if t.race_time_s is not None else 0.0,
                    t.checkpoint_count if t.checkpoint_count is not None else 0,
                    t.gear if t.gear is not None else 0,
                    t.rpm if t.rpm is not None else 0,
                    t.finished,
                    t.race_time_s is not None,
                    t.checkpoint_count is not None,
                    0,
                ],
                dtype=np.float32,
            ),
            "timing": np.array(
                [
                    info["wall_dt_s"],
                    info["game_dt_s"] or 0.0,
                    info["action_delay_s"],
                    info["game_dt_s"] is not None,
                ],
                dtype=np.float32,
            ),
            "previous_action": np.array(info["previous_action"], dtype=np.float32),
            "applied_action": np.array(info["applied_action"], dtype=np.float32),
        }
        if sample.image is not None:
            obs["image"] = np.array(sample.image, copy=True, order="C")
        return obs

    @staticmethod
    def _transition_invalid(sample):
        # A fault or expired lease independently invalidates the transition,
        # even if a backend accidentally omits its redundant validity flag.
        return (
            not bool(sample.metadata.get("transition_valid", True))
            or bool(sample.metadata.get("action_lease_expired", False))
            or bool(sample.metadata.get("control_fault"))
        )

    @staticmethod
    def _require_released_controls(sample):
        release_error = sample.metadata.get("control_release_error")
        if release_error:
            raise RuntimeError(f"Backend could not release controls: {release_error}")

    @staticmethod
    def _validate(sample):
        if not isinstance(sample, Sample) or sample.captured_ns < 0 or sample.sequence < 0:
            raise RuntimeError("Invalid backend sample")
        for name in ("transition_valid", "action_lease_expired"):
            if name in sample.metadata and not isinstance(sample.metadata[name], (bool, np.bool_)):
                raise RuntimeError(f"Backend {name} must be a boolean")
        t = sample.telemetry
        values = [t.speed, *t.position]
        values.extend(v for v in (t.race_time_s, t.gear, t.rpm, t.checkpoint_count) if v is not None)
        if len(t.position) != 3 or not all(math.isfinite(v) for v in values):
            raise RuntimeError("Invalid/nonfinite telemetry")
        if sample.image is not None:
            image = sample.image
            if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3 or min(image.shape) < 1:
                raise RuntimeError("RGB image must be nonempty uint8 HWC")

    def _require_open(self):
        if self._closed:
            raise RuntimeError("Environment is closed")

    def close(self):
        if not self._closed:
            self._closed = True
            self.backend.close()

    def _close_after_failure(self, error):
        try:
            self.close()
        except BaseException as cleanup_error:
            error.add_note(f"Backend cleanup also failed: {cleanup_error}")

    def __enter__(self):
        self._require_open()
        return self

    def __exit__(self, exc_type, error, traceback):
        if error is None:
            self.close()
        else:
            self._close_after_failure(error)
