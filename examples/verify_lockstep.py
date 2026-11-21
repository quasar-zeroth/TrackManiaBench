#!/usr/bin/env python3
"""Verify a real TICK server, simulation clock, repeatability and API episodes.

Requires a running TICK 3.0 server and the tickgym extra. This opens independent
headless simulations; it does not send controls to the desktop game. A finish
input file is TICK InputText containing steer/accel/brake and an optional seed
at tick zero. It must complete this map using the current physics.

    python examples/verify_lockstep.py /data/circuit.Map.Gbx \
        --finish-inputs /data/known-finish.txt > lockstep-verification.json

Without --finish-inputs, successful mechanics checks report "partial" and exit
2: finish and reset-after-finish were not verified. No result implies RGB
support. Stdout is JSON; unavailable servers and failed checks exit 1.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import math
from pathlib import Path
import sys
import time
import json

import numpy as np


def _state(t):
    """Copy telemetry now: the client may reuse backing storage later."""
    return {
        "tick": int(t.race_tick),
        "race_time_ms": int(t.race.race_time_ms),
        "finished": bool(t.finished),
        "checkpoints": int(t.race.checkpoints_taken),
        "position": [float(v) for v in t.vehicle.position],
        "speed_m_s": float(t.vehicle.speed),
        "steer": int(t.drivetrain.steer),
        "held_inputs": int(t.drivetrain.held),
        "gear": int(t.drivetrain.gear),
    }


def _finish_controls(path, seed):
    from tickgym import InputText, InputActionKind

    records = InputText.read(path)
    allowed = {
        InputActionKind.SEED, InputActionKind.STEER,
        InputActionKind.ACCELERATE, InputActionKind.BRAKE,
    }
    grouped = {}
    previous = -1
    for record in records:
        if record.kind not in allowed:
            raise ValueError(f"Finish verifier does not support {record.kind.name}")
        if record.tick < previous or record.tick < 0:
            raise ValueError("Finish inputs must use ordered nonnegative ticks")
        previous = record.tick
        if record.kind == InputActionKind.SEED:
            if record.tick != 0:
                raise ValueError("A finish seed must be at tick zero")
            seed = record.value
        else:
            grouped.setdefault(record.tick, []).append(record)
    if not grouped:
        raise ValueError("Finish input file contains no driving inputs")
    return seed, grouped


def verify(map_path, *, origin, timeout, seed, pause_seconds, finish_inputs=None, finish_ticks=6000):
    from tickgym import Client, TelemetryGroup, InputActionKind
    from trackmaniabench.live import Action, LiveTrackManiaEnv
    from trackmaniabench.live.tickgym_backend import TickGymBackend

    result = {
        "schema_version": 1, "clock_mode": "lockstep", "status": "fail",
        "rgb_available": False, "checks": {}, "errors": [],
        "finish_validation": "requested" if finish_inputs else "not_requested",
    }
    checks = result["checks"]
    client = Client(origin=origin, timeout=timeout)
    session_id = None
    try:
        data = Path(map_path).read_bytes()
        result["map_sha256"] = hashlib.sha256(data).hexdigest()
        result["host"] = asdict(client.status())
        groups = TelemetryGroup.RACE | TelemetryGroup.VEHICLE | TelemetryGroup.DRIVETRAIN
        with client.open_map_bytes(data) as session:
            session_id = session.id
            result["map"] = asdict(session.info.map)
            with session.connect(groups, timeout=timeout) as gym:
                result["server"] = asdict(gym.info)
                first = _state(gym.reset(seed=seed))
                checks["reset_tick_minus_one"] = first["tick"] == -1 and not first["finished"]
                before = gym.snapshot()
                started = time.monotonic()
                time.sleep(pause_seconds)
                after = gym.snapshot()  # This is a server acknowledgement, not a cached observation.
                result["pause"] = {
                    "wall_seconds": time.monotonic() - started,
                    "before_tick": before.tick, "after_tick": after.tick,
                }
                checks["server_clock_frozen_during_pause"] = before.tick == after.tick == -1
                gym.discard(before)
                gym.discard(after)

                schedule = [(1, 0, True, False), (5, 0, True, False), (20, 0, True, False),
                            (200, 0, True, False), (20, -70, True, False),
                            (20, 70, True, False), (30, 0, False, True)]
                trials = []
                durations = []
                exact_ticks = True
                inputs_match = True
                for delayed in (False, True):
                    gym.reset(seed=seed)
                    previous_tick = -1
                    states = []
                    started = time.monotonic()
                    for index, (ticks, steer, accelerate, brake) in enumerate(schedule):
                        if delayed:
                            time.sleep(pause_seconds * ((index % 3) + 1) / 10)
                        state = _state(gym.step(ticks, steer=steer, accelerate=accelerate, brake=brake))
                        exact_ticks &= state["tick"] == previous_tick + ticks
                        inputs_match &= state["steer"] == steer
                        inputs_match &= state["held_inputs"] & 3 == int(accelerate) | (int(brake) << 1)
                        previous_tick = state["tick"]
                        states.append(state)
                    trials.append(states)
                    durations.append(time.monotonic() - started)
                checks["exact_requested_tick_counts"] = bool(exact_ticks)
                checks["reported_controls_match"] = bool(inputs_match)
                checks["same_seed_same_inputs_same_states_despite_delays"] = trials[0] == trials[1]
                displacement = max(math.dist(first["position"], s["position"]) for s in trials[0])
                checks["car_moved"] = displacement > 0.25
                checks["brake_reduced_speed"] = trials[0][-1]["speed_m_s"] < trials[0][-2]["speed_m_s"]
                result["trials"] = {"states": trials, "wall_seconds": durations,
                                     "max_displacement_m": displacement}
                before = gym.snapshot()
                time.sleep(pause_seconds)
                after = gym.snapshot()
                checks["server_clock_frozen_after_driving"] = before.tick == after.tick == trials[-1][-1]["tick"]
                result["pause_after_driving"] = {"before_tick": before.tick, "after_tick": after.tick}
                gym.discard(before)
                gym.discard(after)
                checks["reset_restores_state"] = _state(gym.reset(seed=seed)) == first
        checks["session_closed"] = all(s.session_id != session_id for s in client.sessions())

        with LiveTrackManiaEnv(
            TickGymBackend(map_path, origin=origin, timeout=timeout), control_hz=20,
        ) as env:
            obs, info = env.reset(seed=seed)
            obs, _, terminated, truncated, info = env.step(Action(throttle=1))
            metadata = info["backend"]
            checks["environment_step_is_five_ticks"] = (
                metadata["race_tick"] == 4 and metadata["simulation_ticks"] == 5
                and not terminated and not truncated and "image" not in obs
            )
            result["environment_first_step"] = {
                key: metadata[key] for key in ("race_tick", "simulation_ticks", "simulation_dt_s")
            }

        if finish_inputs:
            finish_seed, grouped = _finish_controls(finish_inputs, seed)
            if max(grouped) >= finish_ticks:
                raise ValueError("--finish-ticks must exceed the last finish input tick")
            with TickGymBackend(map_path, origin=origin, timeout=timeout) as backend:
                initial = backend.reset(seed=finish_seed)
                controls = {"steer": 0.0, "throttle": 0.0, "brake": 0.0}
                current_tick = 0
                sample = initial
                for next_tick in sorted(set(grouped) | {finish_ticks}):
                    if next_tick > current_tick:
                        sample, _ = backend.advance(Action(**controls), next_tick - current_tick)
                        if sample.telemetry.finished:
                            break
                    for record in grouped.get(next_tick, ()):
                        field = {InputActionKind.STEER: "steer", InputActionKind.ACCELERATE: "throttle",
                                 InputActionKind.BRAKE: "brake"}[record.kind]
                        controls[field] = record.value / 127 if field == "steer" else float(record.value)
                    current_tick = next_tick
                checks["known_inputs_finish"] = sample.telemetry.finished
                result["finish"] = {
                    "race_tick": sample.metadata["race_tick"],
                    "race_time_s": sample.telemetry.race_time_s,
                    "checkpoints": sample.telemetry.checkpoint_count,
                    "position": list(sample.telemetry.position),
                    "inputs_sha256": hashlib.sha256(Path(finish_inputs).read_bytes()).hexdigest(),
                }
                reset = backend.reset(seed=finish_seed)
                checks["reset_after_finish"] = (
                    sample.telemetry.finished and not reset.telemetry.finished
                    and reset.metadata["race_tick"] == -1
                    and np.array_equal(initial.telemetry.position, reset.telemetry.position)
                )
        if all(checks.values()):
            result["status"] = "pass" if finish_inputs else "partial"
    except Exception as error:
        result["errors"].append(f"{type(error).__name__}: {error}")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("map", type=Path)
    parser.add_argument("--origin", default="http://127.0.0.1:46323")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--pause-seconds", type=float, default=1)
    parser.add_argument("--finish-inputs", type=Path)
    parser.add_argument("--finish-ticks", type=int, default=6000)
    args = parser.parse_args(argv)
    if not 0 <= args.seed <= 0xFFFFFFFF:
        parser.error("--seed must be in [0, 2**32-1]")
    if not 1 <= args.finish_ticks <= 60000:
        parser.error("--finish-ticks must be in [1,60000]")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be positive and finite")
    if not math.isfinite(args.pause_seconds) or not 0.1 <= args.pause_seconds <= 30:
        parser.error("--pause-seconds must be in [0.1,30]")
    result = verify(args.map, origin=args.origin, timeout=args.timeout, seed=args.seed,
                    pause_seconds=args.pause_seconds, finish_inputs=args.finish_inputs,
                    finish_ticks=args.finish_ticks)
    print(json.dumps(result, indent=2, allow_nan=False))
    return {"pass": 0, "fail": 1, "partial": 2}[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
