#!/usr/bin/env python3
"""DRIVES THE CAR: check real-time inference delay, lease expiry, and reset.

The checker uses the real TMRLBackend and LiveTrackManiaEnv. It accelerates for
two seconds, pauses inference for 0.5 seconds without writing controls, then
deliberately expires a 1.0-second action lease. A passing report requires car
movement, reported controls, host timing, invalid truncation without a new
control write, neutral controls after expiry, and recovery through reset.

The driving schedule is bounded to 12 seconds; capture, reset, and cleanup
can extend wall runtime. The lease bounds abandoned input while this process
remains alive, not a hard real-time guarantee. Choose a local solo map with
enough clear track for the accelerator-only test. This does not test finishing.

    python examples/verify_realtime_timing.py \
      --backend-options backend.local.json --wait-for-ready > timing.json

The JSON supplies TMRLBackend constructor options, including capture_region
and expected_start_position. This check explicitly sets action_lease_seconds
to 1.0. Stdout is one JSON report; exit status is nonzero unless all checks pass.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import time


LEASE_SECONDS = 1.0
INFERENCE_SECONDS = 0.5
EXPIRY_WAIT_SECONDS = 1.2
DRIVING_BUDGET_SECONDS = 12.0


def _controls(extra):
    values = {key: float(extra[f"reported_{key}"]) for key in ("steer", "throttle", "brake")}
    if not all(math.isfinite(value) for value in values.values()):
        raise RuntimeError("Nonfinite reported controls")
    return values


def _neutral(controls):
    return all(abs(value) < 0.1 for value in controls.values())


def _snapshot(observation, info):
    return {
        "position": observation["telemetry"][1:4].tolist(),
        "raw_speed": float(observation["telemetry"][0]),
        "sequence": info["sequence"],
        "captured_ns": info["captured_ns"],
        "reported_controls": _controls(info["telemetry_extra"]),
        "wall_dt_s": info["wall_dt_s"],
        "action_delay_s": info["action_delay_s"],
        "previous_action": info["previous_action"],
        "applied_action": info["applied_action"],
        "requested_action_written": info.get("requested_action_written"),
        "input_write_completed_ns": info.get("input_write_completed_ns"),
        "action_sequence": info["backend"].get("action_sequence"),
        "control_fault": info["backend"].get("control_fault"),
        "transition_valid": info.get("transition_valid"),
    }


def verify(backend, *, control_hz=20.0, warmup_seconds=2.0, min_movement=0.25):
    """Own, exercise, and close an already-created real-time backend."""
    from trackmaniabench.live import Action, LiveTrackManiaEnv
    from trackmaniabench.live.types import ActionLeaseExpired

    names = (
        "realtime_capability", "initial_reset", "warmup_movement", "warmup_throttle",
        "inference_movement", "inference_throttle_held", "inference_no_control_write",
        "inference_transition_valid", "wall_dt_includes_inference", "action_delay_includes_inference",
        "previous_action_retained", "lease_invalid_truncation", "lease_no_requested_write",
        "lease_neutral_reported", "lease_no_control_write", "reset_recovery",
        "post_reset_action_accepted", "driving_budget", "final_reset", "closed",
    )
    result = {
        "schema_version": 1,
        "status": "fail",
        "clock_mode": "realtime",
        "action_lease_seconds": LEASE_SECONDS,
        "inference_pause_seconds": INFERENCE_SECONDS,
        "expiry_pause_seconds": EXPIRY_WAIT_SECONDS,
        "timing_basis": "host monotonic receipt/capture and device-write timing; not simulation ticks",
        "checks": dict.fromkeys(names, False),
        "errors": [],
        "events": {},
    }
    checks = result["checks"]
    env = None
    drive_started = None

    def budget():
        if drive_started is not None and time.monotonic() - drive_started >= DRIVING_BUDGET_SECONDS:
            raise RuntimeError("Timing check exceeded its 12-second driving schedule")

    def normal_step(action):
        budget()
        observation, _, terminated, truncated, info = env.step(action)
        if terminated or truncated or not info["transition_valid"]:
            raise RuntimeError("Unexpected terminal/invalid transition; use a longer playable map")
        budget()
        return observation, info

    try:
        checks["realtime_capability"] = backend.capabilities.clock_mode == "realtime"
        if not checks["realtime_capability"]:
            raise RuntimeError("This check requires the real-time TMRL backend")
        env = LiveTrackManiaEnv(backend, control_hz=control_hz, max_episode_seconds=30)
        observation, info = env.reset()
        checks["initial_reset"] = bool(info["backend"].get("reset_verified"))
        if not checks["initial_reset"]:
            raise RuntimeError("Initial starting pose was not verified")
        origin = observation["telemetry"][1:4].tolist()
        drive_started = time.monotonic()
        warmup_samples = 0
        throttle_samples = 0
        max_displacement = 0.0
        while time.monotonic() - drive_started < warmup_seconds:
            observation, info = normal_step(Action(throttle=1))
            controls = _controls(info["telemetry_extra"])
            throttle_samples += int(controls["throttle"] > 0.1 and controls["brake"] < 0.1)
            warmup_samples += 1
            max_displacement = max(max_displacement, math.dist(origin, observation["telemetry"][1:4]))
        checks["warmup_movement"] = max_displacement >= min_movement
        checks["warmup_throttle"] = throttle_samples >= 2
        result["warmup"] = {
            "samples": warmup_samples, "matching_throttle_samples": throttle_samples,
            "max_displacement_game_world_units": max_displacement,
        }
        before_pause = _snapshot(observation, info)
        result["events"]["before_inference"] = before_pause
        pause_started = time.monotonic()
        time.sleep(INFERENCE_SECONDS)
        actual_pause = time.monotonic() - pause_started
        budget()
        # Observe without sending another action: this distinguishes actual
        # held controls from inputs reapplied by the next environment step.
        held = backend.observe()
        held_controls = _controls(held.telemetry.extra)
        pause_displacement = math.dist(before_pause["position"], held.telemetry.position)
        checks["inference_movement"] = pause_displacement >= min_movement
        checks["inference_throttle_held"] = held_controls["throttle"] > 0.1 and held_controls["brake"] < 0.1
        checks["inference_no_control_write"] = held.metadata.get("action_sequence") == before_pause["action_sequence"]
        checks["inference_transition_valid"] = bool(held.metadata.get("transition_valid")) and not held.metadata.get("control_fault")
        result["events"]["after_inference_without_write"] = {
            "position": list(held.telemetry.position), "sequence": held.sequence,
            "captured_ns": held.captured_ns, "reported_controls": held_controls,
            "action_sequence": held.metadata.get("action_sequence"),
            "actual_pause_seconds": actual_pause,
            "displacement_game_world_units": pause_displacement,
        }
        # A different next action makes previous_action unambiguous.
        observation, info = normal_step(Action(brake=1))
        result["events"]["after_delayed_step"] = _snapshot(observation, info)
        checks["wall_dt_includes_inference"] = info["wall_dt_s"] >= actual_pause
        checks["action_delay_includes_inference"] = info["action_delay_s"] >= actual_pause
        checks["previous_action_retained"] = info["previous_action"] == [0.0, 1.0, 0.0]

        observation, info = normal_step(Action(throttle=1))
        before_expiry = _snapshot(observation, info)
        result["events"]["before_expiry"] = before_expiry
        time.sleep(EXPIRY_WAIT_SECONDS)
        budget()
        observation, _, terminated, truncated, info = env.step(Action(steer=0.5, throttle=1))
        result["events"]["after_expiry"] = _snapshot(observation, info)
        checks["lease_invalid_truncation"] = (
            truncated and not terminated and not info["transition_valid"]
            and info["terminal_reason"] == "action_lease_expired"
            and info["backend"].get("control_release_error") is None
        )
        checks["lease_no_requested_write"] = (
            info["requested_action_written"] is False and info["input_write_completed_ns"] is None
        )
        checks["lease_neutral_reported"] = _neutral(_controls(info["telemetry_extra"]))
        checks["lease_no_control_write"] = info["backend"].get("action_sequence") == before_expiry["action_sequence"]
        budget()
        observation, info = env.reset()
        checks["reset_recovery"] = bool(info["backend"].get("reset_verified")) and info["backend"].get("control_fault") is None
        observation, info = normal_step(Action())
        result["events"]["after_recovery"] = _snapshot(observation, info)
        checks["post_reset_action_accepted"] = (
            info["requested_action_written"] is True and info["transition_valid"]
            and _neutral(_controls(info["telemetry_extra"]))
        )
        result["drive_schedule_wall_seconds"] = time.monotonic() - drive_started
        checks["driving_budget"] = result["drive_schedule_wall_seconds"] < DRIVING_BUDGET_SECONDS
    except (Exception, KeyboardInterrupt) as error:
        result["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        if drive_started is not None:
            try:
                backend.apply_action(Action())
            except ActionLeaseExpired:
                pass  # This typed fault guarantees successful neutralization.
            except Exception as error:
                result["errors"].append(f"Cleanup neutral: {type(error).__name__}: {error}")
            try:
                final = backend.reset()
                checks["final_reset"] = bool(final.metadata.get("reset_verified")) and not final.metadata.get("control_fault")
            except Exception as error:
                result["errors"].append(f"Cleanup reset: {type(error).__name__}: {error}")
        try:
            (env if env is not None else backend).close()
            checks["closed"] = True
        except Exception as error:
            result["errors"].append(f"Cleanup close: {type(error).__name__}: {error}")
    if all(checks.values()) and not result["errors"]:
        result["status"] = "pass"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend-options", required=True, type=Path)
    parser.add_argument("--wait-for-ready", action="store_true")
    parser.add_argument("--control-hz", type=float, default=20.0)
    parser.add_argument("--warmup-seconds", type=float, default=2.0)
    parser.add_argument("--min-movement", type=float, default=0.25)
    args = parser.parse_args(argv)
    # The held step plus the 0.5 s inference pause must fit inside the 1 s
    # lease, with some room for capture. Slower rates cannot pass this test.
    for name, low, high in (("control_hz", 3.0, 100.0), ("warmup_seconds", 0.5, 4.0), ("min_movement", 0.001, 100.0)):
        value = getattr(args, name)
        if not math.isfinite(value) or not low <= value <= high:
            parser.error(f"--{name.replace('_', '-')} must be finite and in [{low}, {high}]")
    try:
        options = json.loads(args.backend_options.read_text())
    except (OSError, ValueError) as error:
        parser.error(f"Cannot read backend options: {error}")
    if not isinstance(options, dict) or options.get("expected_start_position") is None:
        parser.error("Backend options must be an object containing expected_start_position")
    options["action_lease_seconds"] = LEASE_SECONDS
    from trackmaniabench.live.tmrl import TMRLBackend

    result = {"schema_version": 1, "status": "fail", "errors": []}
    try:
        with TMRLBackend(**options) as backend:
            if args.wait_for_ready:
                print("Controller created. Enter the race and select the gameplay camera. Press Enter here to reset and DRIVE THE CAR.", file=sys.stderr, flush=True)
                input()
            result = verify(backend, control_hz=args.control_hz, warmup_seconds=args.warmup_seconds, min_movement=args.min_movement)
    except (Exception, KeyboardInterrupt) as error:
        result["status"] = "fail"
        result["errors"].append(f"{type(error).__name__}: {error}")
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
