#!/usr/bin/env python3
r"""DRIVES THE CAR: verify real finishes and resets using a reference path.

Load the matching map in a local solo race first. The reference JSON must have
``samples``, each with finite XYZ ``position`` and ``velocity`` vectors in game
world units and units/second. No map or ghost is bundled. This is a diagnostic
path follower, not a trained policy or a general Trackmania driving solution.

    python examples/verify_realtime_episodes.py \
      --backend-options backend.local.json --reference reference.local.json \
      --episodes 2 --adapter elemental --steer-deadzone 0.125 > episodes.json

The default controller was tested on one stadium circuit with variable timing
and capture latency. Other surfaces, jumps, shortcuts, or capture latency can
need different gains. ``control_hz`` sets the minimum action hold duration to
1 / control_hz; capture and inference also take time. Position differences use
measured telemetry receipt intervals.
The optional deadzone compensation must match the game's steering setting.
Stdout is a JSON report; failure or an unfinished race returns a nonzero code.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import time

import numpy as np


class PursuitController:
    """A small, importable position-only diagnostic controller.

    Progress searches the next 50 recorded samples, so references should be
    dense (the tested recording uses one sample per 50 ms). Heading and speed
    come from observed position changes; reference velocity is only used at
    rest and to extend the path past its last recorded sample at the finish.
    """

    def __init__(
        self, reference, *, gain=0.8, lookahead_m=10.0, lookahead_seconds=0.0,
        speed_cap_m_s=30.0, brake_margin_m_s=5.0, steer_deadzone=0.0,
    ):
        samples = reference.get("samples") if isinstance(reference, dict) else None
        if not isinstance(samples, list) or len(samples) < 2:
            raise ValueError("Reference must contain at least two samples")
        try:
            self.positions = np.asarray([sample["position"] for sample in samples], dtype=float)
            self.velocities = np.asarray([sample["velocity"] for sample in samples], dtype=float)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Each reference sample needs position and velocity vectors") from error
        for values in (self.positions, self.velocities):
            if values.shape != (len(samples), 3) or not np.isfinite(values).all():
                raise ValueError("Reference position/velocity must be finite XYZ vectors")
        for name, value, positive in (
            ("gain", gain, True), ("lookahead_m", lookahead_m, True),
            ("lookahead_seconds", lookahead_seconds, False),
            ("speed_cap_m_s", speed_cap_m_s, True),
            ("brake_margin_m_s", brake_margin_m_s, False),
        ):
            if not math.isfinite(value) or (value <= 0 if positive else value < 0):
                raise ValueError(f"Invalid {name}")
        if not math.isfinite(steer_deadzone) or not 0 <= steer_deadzone < 1:
            raise ValueError("steer_deadzone must be in [0, 1)")
        self.arc = np.r_[0, np.cumsum(np.linalg.norm(np.diff(self.positions[:, [0, 2]], axis=0), axis=1))]
        if self.arc[-1] <= 0:
            raise ValueError("Reference path must move horizontally")
        self.gain = gain
        self.lookahead_m = lookahead_m
        self.lookahead_seconds = lookahead_seconds
        self.speed_cap_m_s = speed_cap_m_s
        self.brake_margin_m_s = brake_margin_m_s
        self.steer_deadzone = steer_deadzone
        self.reset()

    def reset(self):
        self.index = 0
        self.previous = None

    def update(self, position, dt):
        position = np.asarray(position, dtype=float)
        if position.shape != (3,) or not np.isfinite(position).all():
            raise ValueError("Position must be a finite XYZ vector")
        if not math.isfinite(dt) or dt <= 0:
            raise ValueError("Observation dt must be positive and finite")
        nearby = self.positions[self.index:min(len(self.positions), self.index + 50)]
        distances = np.linalg.norm(nearby - position, axis=1)
        self.index += int(np.argmin(distances))
        velocity = np.zeros(2) if self.previous is None else (position - self.previous)[[0, 2]] / dt
        speed = float(np.linalg.norm(velocity))
        self.previous = position.copy()
        if speed > 0.2:
            heading = velocity / speed
        else:
            heading = self.velocities[min(self.index + 2, len(self.positions) - 1), [0, 2]].copy()
            if np.linalg.norm(heading) < 1e-6:
                heading = self.positions[min(self.index + 1, len(self.positions) - 1), [0, 2]] - position[[0, 2]]
            heading /= max(1e-6, float(np.linalg.norm(heading)))
        target_index = min(
            len(self.positions) - 1,
            int(np.searchsorted(self.arc, self.arc[self.index] + self.lookahead_m + self.lookahead_seconds * speed)),
        )
        target = self.positions[target_index, [0, 2]] - position[[0, 2]]
        if target_index == len(self.positions) - 1:
            target += self.velocities[-1, [0, 2]] * 0.3
        error = math.atan2(heading[0] * target[1] - heading[1] * target[0], float(heading @ target))
        desired_steer = float(np.clip(self.gain * error, -1, 1))
        steer = (
            math.copysign(self.steer_deadzone + (1 - self.steer_deadzone) * abs(desired_steer), desired_steer)
            if desired_steer else 0.0
        )
        action = [steer, float(speed < self.speed_cap_m_s), float(speed > self.speed_cap_m_s + self.brake_margin_m_s)]
        return action, {
            "reference_index": self.index, "path_error_m": float(distances.min()),
            "estimated_speed_m_s": speed, "heading_error_rad": error,
            "desired_steer": desired_steer, "observation_dt_s": dt,
        }


def _array(value):
    if hasattr(value, "detach"):
        return value.detach().cpu().numpy()[0]
    return np.asarray(value)


def _stamp(info):
    return int(info["backend"].get("telemetry_received_ns", info["captured_ns"]))


def _reset_verified(info):
    return bool(info["backend"].get("reset_verified")) and not info["backend"].get("control_fault")


def _capture(observation, directory, name):
    if directory is None or "image" not in observation:
        return None
    from PIL import Image

    values = _array(observation["image"])
    if values.dtype != np.uint8:
        values = np.rint(np.clip(values.transpose(1, 2, 0), 0, 1) * 255).astype(np.uint8)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.png"
    Image.fromarray(values).save(path)
    return str(path)


def verify(backend, controller, *, episodes=2, adapter="standalone", control_hz=20.0,
           max_episode_seconds=120.0, max_steps=4000, captures=None):
    """Own the backend, finish bounded episodes, verify each reset, and close."""
    from trackmaniabench.live import Action, LiveTrackManiaEnv

    result = {
        "schema_version": 1, "status": "fail", "clock_mode": "realtime", "adapter": adapter,
        "requested_episodes": episodes, "completed_finishes": 0, "verified_resets": 0,
        "checks": dict.fromkeys(("realtime_capability", "initial_reset", "all_finishes", "all_resets", "valid_transitions", "closed"), False),
        "episodes": [], "errors": [],
        "timing_basis": "host telemetry receipt intervals; no claim of simulation-clock synchronization",
    }
    checks = result["checks"]
    env = None
    started = time.monotonic()
    try:
        checks["realtime_capability"] = backend.capabilities.clock_mode == "realtime"
        if not checks["realtime_capability"]:
            raise ValueError("Episode verification requires a real-time backend")
        env = LiveTrackManiaEnv(backend, control_hz=control_hz, max_episode_seconds=max_episode_seconds, max_steps=max_steps)
        if adapter == "elemental":
            import torch

            from trackmaniabench.live.elemental import ElementalTrackManiaEnv

            env = ElementalTrackManiaEnv(env=env)
            observation = env.reset()
            info = env.reset_info
        elif adapter == "standalone":
            observation, info = env.reset()
        else:
            raise ValueError("adapter must be standalone or elemental")
        checks["initial_reset"] = _reset_verified(info)
        if not checks["initial_reset"]:
            raise RuntimeError("Initial reset was not verified")
        result["verified_resets"] += 1
        _capture(observation, captures, "initial-reset")
        for episode_index in range(episodes):
            controller.reset()
            previous_stamp = None
            episode_started = time.monotonic()
            record = {"episode": episode_index + 1, "finished": False, "reset_verified": False, "steps": []}
            result["episodes"].append(record)
            for _ in range(max_steps):
                if time.monotonic() - episode_started > max_episode_seconds + 5:
                    raise RuntimeError("Episode exceeded its wall-time limit")
                stamp = _stamp(info)
                dt = 1 / control_hz if previous_stamp is None else (stamp - previous_stamp) / 1e9
                previous_stamp = stamp
                action, diagnostic = controller.update(_array(observation["telemetry"])[1:4], dt)
                if adapter == "elemental":
                    observation, _, done, step_info = env.step(torch.tensor([action], dtype=torch.float32))
                    info = step_info["raw_info"][0]
                    terminated = bool(step_info["terminated"].item())
                    truncated = bool(step_info["truncated"].item())
                    terminal_observation = step_info.get("final_observation", observation)
                    if bool(done.item()) != bool(terminated or truncated):
                        raise RuntimeError("Elemental done flag disagrees with termination/truncation")
                else:
                    observation, _, terminated, truncated, info = env.step(Action.from_array(action))
                    terminal_observation = observation
                record["steps"].append({
                    **diagnostic, "action": action, "position": _array(terminal_observation["telemetry"])[1:4].tolist(),
                    "sequence": info["sequence"], "wall_dt_s": info["wall_dt_s"],
                    "reported_controls": {key: info["telemetry_extra"].get(f"reported_{key}") for key in ("steer", "throttle", "brake")},
                })
                if not info.get("transition_valid", True) or info["backend"].get("control_fault"):
                    raise RuntimeError("Invalid control transition")
                if terminated or truncated:
                    record.update(
                        finished=bool(terminated and info["success"]), terminal_reason=info.get("terminal_reason"),
                        episode_seconds=info["episode_seconds"], step_count=info["episode_steps"],
                        terminal_position=_array(terminal_observation["telemetry"])[1:4].tolist(),
                    )
                    record["terminal_capture"] = _capture(terminal_observation, captures, f"episode-{episode_index + 1}-terminal")
                    if not record["finished"]:
                        raise RuntimeError(f"Episode {episode_index + 1} did not finish: {record['terminal_reason']}")
                    result["completed_finishes"] += 1
                    if adapter == "elemental":
                        info = step_info["reset_info"]
                    else:
                        observation, info = env.reset()
                    record["reset_verified"] = _reset_verified(info)
                    if not record["reset_verified"]:
                        raise RuntimeError("Reset after finish was not verified")
                    result["verified_resets"] += 1
                    record["reset_capture"] = _capture(observation, captures, f"episode-{episode_index + 1}-reset")
                    break
            else:
                raise RuntimeError("Step limit reached without a finish")
        checks["all_finishes"] = result["completed_finishes"] == episodes
        checks["all_resets"] = result["verified_resets"] == episodes + 1
        checks["valid_transitions"] = True
    except (Exception, KeyboardInterrupt) as error:
        result["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        try:
            (env if env is not None else backend).close()
            checks["closed"] = True
        except Exception as error:
            result["errors"].append(f"Cleanup close: {type(error).__name__}: {error}")
    result["wall_seconds"] = time.monotonic() - started
    if all(checks.values()) and not result["errors"]:
        result["status"] = "pass"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend-options", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--adapter", choices=("standalone", "elemental"), default="standalone")
    parser.add_argument("--episodes", type=int, default=2)
    parser.add_argument("--control-hz", type=float, default=20.0)
    parser.add_argument("--max-episode-seconds", type=float, default=120.0)
    parser.add_argument("--max-steps", type=int, default=4000)
    parser.add_argument("--gain", type=float, default=0.8)
    parser.add_argument("--lookahead-m", type=float, default=10.0)
    parser.add_argument("--lookahead-seconds", type=float, default=0.0)
    parser.add_argument("--speed-cap-m-s", type=float, default=30.0)
    parser.add_argument("--steer-deadzone", type=float, default=0.0)
    parser.add_argument("--captures", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.episodes <= 100 or not 1 <= args.max_steps <= 100000:
        parser.error("episodes must be in [1, 100] and max-steps in [1, 100000]")
    for name, low, high in (("control_hz", 1, 100), ("max_episode_seconds", 1, 3600)):
        value = getattr(args, name)
        if not math.isfinite(value) or not low <= value <= high:
            parser.error(f"{name} must be finite and in [{low}, {high}]")
    try:
        options = json.loads(args.backend_options.read_text())
        reference = json.loads(args.reference.read_text())
        controller = PursuitController(
            reference, gain=args.gain, lookahead_m=args.lookahead_m,
            lookahead_seconds=args.lookahead_seconds, speed_cap_m_s=args.speed_cap_m_s,
            steer_deadzone=args.steer_deadzone,
        )
        if not isinstance(options, dict) or options.get("expected_start_position") is None:
            raise ValueError("Backend options must contain expected_start_position")
        if math.dist(options["expected_start_position"], controller.positions[0]) > 2:
            raise ValueError("Reference start differs from the configured map start by more than 2 units")
    except (OSError, TypeError, ValueError) as error:
        parser.error(str(error))
    # Load optional heavy dependencies before owning active input devices.
    if args.adapter == "elemental":
        try:
            import torch  # noqa: F401
        except ImportError:
            parser.error("--adapter elemental requires the optional torch package")
    from trackmaniabench.live.tmrl import TMRLBackend

    try:
        backend = TMRLBackend(**options)
    except Exception as error:
        print(json.dumps({"status": "fail", "errors": [f"Backend construction: {type(error).__name__}: {error}"]}))
        return 1
    result = verify(
        backend, controller, episodes=args.episodes, adapter=args.adapter,
        control_hz=args.control_hz, max_episode_seconds=args.max_episode_seconds,
        max_steps=args.max_steps, captures=args.captures,
    )
    result["reference"] = {key: reference.get(key) for key in ("map_uid", "record_id", "record_finish_ms")}
    result["controller_options"] = {
        key: getattr(args, key) for key in ("gain", "lookahead_m", "lookahead_seconds", "speed_cap_m_s", "steer_deadzone")
    }
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
