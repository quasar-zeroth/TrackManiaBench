#!/usr/bin/env python3
"""DRIVES THE CAR: verify accelerator, steering, brake, movement, and reset.

The checker creates one persistent controller, resets the configured map,
drives four short phases, and resets to the configured starting anchor again.
Default phases last 4/2/2/2 seconds, with a maximum configured total of 12
seconds. Input leases are capped at one second. Network/capture/reset delays
can extend wall runtime; this is not a hard real-time control guarantee.

--wait-for-ready keeps the same virtual pad alive while you manually enter the
race. The script never clicks menus or chooses a map. Use a local solo map.
It checks fresh reported controls and actual position movement: merely
finding a stationary car at the starting anchor does not pass this test.

    python examples/verify_live_controls.py \
      --backend-options local-backend.json --wait-for-ready

The backend JSON requires capture_region and expected_start_position, along
with any local telemetry/display/input settings. Stdout is one JSON summary;
status is nonzero if any check fails. Timing uses host monotonic timestamps,
not a game clock or proof of input-to-physics latency. Torch is not required.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
import sys
import time


def _stats(values):
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(values),
        "min_ms": min(values),
        "mean_ms": statistics.mean(values),
        "median_ms": statistics.median(values),
        "p95_ms": ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)],
        "max_ms": max(values),
    }


def verify(
    backend,
    *,
    phase_seconds=(4.0, 2.0, 2.0, 2.0),
    sample_hz=10.0,
    min_movement=0.25,
    now=time.monotonic,
    sleep=time.sleep,
):
    """Run the bounded phase schedule using one already-created backend."""
    from trackmaniabench.live.types import Action

    phases = (
        ("accelerator", Action(0.0, 1.0, 0.0)),
        ("left", Action(-0.75, 1.0, 0.0)),
        ("right", Action(0.75, 1.0, 0.0)),
        ("brake", Action(0.0, 0.0, 1.0)),
    )
    result = {
        "schema_version": 1,
        "status": "fail",
        "clock_mode": "realtime",
        "configured_drive_seconds": sum(phase_seconds),
        "timing_basis": "host monotonic device-write/receipt/capture; not simulation time",
        "control_checks_basis": "plugin-reported controls; steering sign does not prove physical turn direction",
        "checks": {},
        "phases": [],
        "errors": [],
    }
    metrics = {"observe_call": [], "capture": [], "receipt_to_capture_end": [], "write_to_capture_end": []}
    max_displacement = 0.0
    last_sequence = None
    drive_started = None
    try:
        initial = backend.reset()
        origin = initial.telemetry.position
        result["initial_reset"] = {"position": list(origin), "raw_speed": initial.telemetry.speed}
        result["checks"]["initial_reset_verified"] = bool(initial.metadata.get("reset_verified", False))
        if not result["checks"]["initial_reset_verified"]:
            raise RuntimeError("Backend did not report a verified initial reset")
        last_sequence = initial.sequence
        drive_started = now()
        drive_deadline = drive_started + 12.0
        for (name, requested), duration in zip(phases, phase_seconds, strict=True):
            phase_started = now()
            phase_deadline = min(phase_started + duration, drive_deadline)
            row = {
                "name": name,
                "requested_action": requested.array().tolist(),
                "samples": 0,
                "matching_control_samples": 0,
                "reported_controls": [],
                "max_displacement": 0.0,
            }
            result["phases"].append(row)
            while now() < phase_deadline:
                sample_started = now()
                applied = backend.apply_action(requested)
                observed_started = now()
                sample = backend.observe()
                observed_ended = now()
                metrics["observe_call"].append((observed_ended - observed_started) * 1000.0)
                if sample.sequence <= last_sequence:
                    raise RuntimeError("Observation sequence did not advance")
                last_sequence = sample.sequence
                metadata = sample.metadata
                if not metadata.get("transition_valid", False) or metadata.get("control_fault"):
                    raise RuntimeError(f"Invalid control transition: {metadata.get('control_fault')}")
                receipt = metadata.get("telemetry_received_ns")
                written = metadata.get("action_reported_ns")
                if receipt is None or written is None or receipt < written:
                    raise RuntimeError("Telemetry receipt did not follow the current device write")
                capture_start = metadata.get("capture_started_ns")
                capture_end = metadata.get("capture_ended_ns")
                if capture_start is not None and capture_end is not None:
                    metrics["capture"].append((capture_end - capture_start) / 1e6)
                    metrics["receipt_to_capture_end"].append((capture_end - receipt) / 1e6)
                    metrics["write_to_capture_end"].append((capture_end - written) / 1e6)
                extra = sample.telemetry.extra
                controls = {key: float(extra[f"reported_{key}"]) for key in ("steer", "throttle", "brake")}
                if not all(math.isfinite(value) for value in controls.values()):
                    raise RuntimeError("Reported game controls are not finite")
                matches = {
                    "accelerator": controls["throttle"] > 0.1 and controls["brake"] < 0.5,
                    "left": controls["steer"] < -0.1 and controls["throttle"] > 0.1,
                    "right": controls["steer"] > 0.1 and controls["throttle"] > 0.1,
                    "brake": controls["brake"] > 0.5 and controls["throttle"] < 0.1,
                }[name]
                displacement = math.dist(origin, sample.telemetry.position)
                max_displacement = max(max_displacement, displacement)
                row["max_displacement"] = max(row["max_displacement"], displacement)
                row["samples"] += 1
                row["matching_control_samples"] += int(matches)
                row["reported_controls"].append(controls)
                row["last_applied_action"] = applied.array().tolist()
                row["last_position"] = list(sample.telemetry.position)
                row["last_raw_speed"] = sample.telemetry.speed
                if sample.telemetry.finished:
                    raise RuntimeError(
                        "Race finished before the control check completed; choose a longer map"
                    )
                if observed_ended >= drive_deadline:
                    raise RuntimeError("Control verification exceeded its 12-second driving budget")
                sleep(max(0.0, min(1.0 / sample_hz - (now() - sample_started), phase_deadline - now())))
            row["elapsed_wall_seconds"] = now() - phase_started
            result["checks"][f"{name}_reported"] = row["matching_control_samples"] >= 2
        result["drive_elapsed_wall_seconds"] = now() - drive_started
    except Exception as error:
        result["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        # reset() releases controls before attempting its verified reset, even
        # if an expired lease prevents the initial explicit neutral command.
        if drive_started is None:
            result["checks"]["final_reset_verified"] = False
            result["final_reset"] = {"attempted": False, "reason": "initial reset failed; no driving started"}
        else:
            try:
                backend.apply_action(Action())
            except Exception as error:
                result["errors"].append(f"Neutral command failed: {type(error).__name__}: {error}")
            try:
                final = backend.reset()
                result["final_reset"] = {
                    "position": list(final.telemetry.position),
                    "raw_speed": final.telemetry.speed,
                }
                result["checks"]["final_reset_verified"] = bool(final.metadata.get("reset_verified", False))
            except Exception as error:
                result["checks"]["final_reset_verified"] = False
                result["errors"].append(f"Final reset failed: {type(error).__name__}: {error}")
    result["max_displacement_game_world_units"] = max_displacement
    result["minimum_displacement_game_world_units"] = min_movement
    result["checks"]["position_moved"] = max_displacement >= min_movement
    for name, _ in phases:
        result["checks"].setdefault(f"{name}_reported", False)
    result["latency"] = {name: _stats(values) for name, values in metrics.items()}
    if not result["errors"] and all(result["checks"].values()):
        result["status"] = "pass"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--backend-options", type=Path, required=True, help="JSON arguments for TMRLBackend")
    parser.add_argument(
        "--wait-for-ready",
        action="store_true",
        help="Create the controller, then wait for Enter before resetting/driving",
    )
    parser.add_argument(
        "--phase-seconds",
        nargs=4,
        type=float,
        default=(4.0, 2.0, 2.0, 2.0),
        metavar=("ACCEL", "LEFT", "RIGHT", "BRAKE"),
        help="Phase durations; positive, total <=12 seconds",
    )
    parser.add_argument(
        "--sample-hz", type=float, default=10.0, help="Maximum control/observation sample rate (default: 10)"
    )
    parser.add_argument(
        "--min-movement",
        type=float,
        default=0.25,
        help="Required displacement in game-world units (default: 0.25)",
    )
    args = parser.parse_args(argv)
    if (
        any(not math.isfinite(value) or value <= 0 for value in args.phase_seconds)
        or sum(args.phase_seconds) > 12
    ):
        parser.error("--phase-seconds must be positive and finite, with total <=12 seconds")
    if not math.isfinite(args.sample_hz) or not 0 < args.sample_hz <= 100:
        parser.error("--sample-hz must be finite and in (0,100]")
    if not math.isfinite(args.min_movement) or args.min_movement <= 0:
        parser.error("--min-movement must be positive and finite")
    try:
        options = json.loads(args.backend_options.read_text())
    except (OSError, ValueError) as error:
        parser.error(f"Cannot read backend options: {error}")
    if not isinstance(options, dict) or options.get("expected_start_position") is None:
        parser.error("Backend options must be a JSON object with expected_start_position")
    lease = options.get("action_lease_seconds", 1.0)
    if (
        isinstance(lease, bool)
        or not isinstance(lease, (float, int))
        or not math.isfinite(lease)
        or lease <= 0
    ):
        parser.error("action_lease_seconds must be positive and finite")
    options["action_lease_seconds"] = min(lease, 1.0)

    from trackmaniabench.live.tmrl import TMRLBackend

    result = {"schema_version": 1, "status": "fail", "errors": []}
    try:
        with TMRLBackend(**options) as backend:
            if args.wait_for_ready:
                print(
                    "Controller created. Manually enter the race with this controller and choose the gameplay camera. Press Enter here when ready; this checker will then reset and DRIVE THE CAR.",
                    file=sys.stderr,
                    flush=True,
                )
                input()
            result = verify(
                backend,
                phase_seconds=args.phase_seconds,
                sample_hz=args.sample_hz,
                min_movement=args.min_movement,
            )
    except (Exception, KeyboardInterrupt) as error:
        result["status"] = "fail"
        result["errors"].append(f"{type(error).__name__}: {error}")
    result["action_lease_seconds"] = options["action_lease_seconds"]
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
