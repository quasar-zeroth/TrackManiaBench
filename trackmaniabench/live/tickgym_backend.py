"""Optional, telemetry-only lockstep adapter for the tickgym 1.1.0 API.

This requires a separately provisioned, compatible TICK server. Installing the
Python client does not install that server, modify Trackmania, or provide a
rendering API. The native Linux TICK 3.0.1 optimizer has been validated with a
Steam/Proton game's resources. No failure switches to wall-clock play.
"""

from __future__ import annotations

import math
from numbers import Integral
from pathlib import Path
import time
from typing import Any

from .types import Action, Capabilities, Sample, Telemetry


# Public TelemetryGroup flags in tickgym 1.1.0: RACE | VEHICLE | DRIVETRAIN.
# Keeping the mask here lets injected clients run without the optional package.
_TELEMETRY_GROUPS = 1 | 2 | 4
_TICK_SECONDS = 0.01


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise TypeError(f"{name} must be an integer")
    result = int(value)
    if not low <= result <= high:
        raise ValueError(f"{name} must be in [{low}, {high}]")
    return result


class TickGymBackend:
    """Drive a TICK session in integer 10 ms simulation steps.

    ``map_path`` is read on the Python host and uploaded with ``open_map_bytes``;
    it does not need a matching Windows/Wine pathname. The constructor opens a
    dedicated session and gym, but callers must explicitly call :meth:`reset`
    before observing or advancing. ``client`` is an optional compatible client
    for tests; the backend owns and closes the session/gym it creates, not the
    supplied client.

    Steering is rounded to the nearest integer in [-127, 127] (ties to even).
    Throttle and brake are digital, pressed at values >= 0.5. ``advance`` returns
    those quantized controls alongside the sample. The returned ``race_tick``
    and ``race_time_s`` come from the server. ``simulation_dt_s`` is computed
    from the actual tick difference, so early finishes do not inflate time.
    Reset is at tick -1 and consumes no controller step.

    Samples have no image: tickgym 1.1.0 does not expose RGB rendering. This
    adapter is implemented against that client's public API and validated with
    the native Linux TICK 3.0.1 optimizer, including finish and reset episodes.
    Methods must be called serially by a single owner.
    """

    capabilities = Capabilities(
        clock_mode="lockstep",
        rgb=False,
        action_profile="tick-steer-int8-digital-pedals",
        tick_seconds=_TICK_SECONDS,
    )

    def __init__(
        self,
        map_path: str | Path,
        origin: str = "http://127.0.0.1:46323",
        timeout: float = 120.0,
        *,
        client: Any = None,
    ) -> None:
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self._closed = False
        self._session = None
        self._gym = None
        self._last: Sample | None = None
        self._sequence = 0
        data = Path(map_path).read_bytes()
        groups = _TELEMETRY_GROUPS
        if client is None:
            try:
                import tickgym
            except ImportError as error:
                raise ImportError(
                    "TickGymBackend requires the optional tickgym==1.1.0 package "
                    "and a compatible, separately installed TICK server"
                ) from error
            if tickgym.__version__ != "1.1.0":
                raise RuntimeError("TickGymBackend is validated against the tickgym 1.1.0 client API")
            client = tickgym.Client(origin=origin, timeout=timeout)
            groups = (
                tickgym.TelemetryGroup.RACE
                | tickgym.TelemetryGroup.VEHICLE
                | tickgym.TelemetryGroup.DRIVETRAIN
            )
        try:
            self._session = client.open_map_bytes(data)
            self._gym = self._session.connect(telemetry=groups, timeout=timeout)
        except BaseException as error:
            self._close_after_failure(error)
            raise

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("TickGymBackend is closed; create a new backend")

    def _require_sample(self) -> Sample:
        self._require_open()
        if self._last is None:
            raise RuntimeError("Call reset() before observing or advancing TickGymBackend")
        return self._last

    def _sample(self, raw: Any, *, previous_tick: int, requested_ticks: int) -> Sample:
        tick = _integer(raw.race_tick, "returned race_tick", -1, 0x7FFFFFFF)
        advanced = tick - previous_tick
        if not 0 <= advanced <= requested_ticks:
            raise RuntimeError("TICK returned a race tick outside the requested simulation window")
        if requested_ticks and advanced != requested_ticks and not raw.finished:
            raise RuntimeError("TICK returned a shortened step without a finished race")
        race, vehicle, drivetrain = raw.race, raw.vehicle, raw.drivetrain
        if race is None or vehicle is None or drivetrain is None:
            raise RuntimeError("TICK omitted a requested telemetry group")
        if race.race_tick != tick:
            raise RuntimeError("TICK race telemetry and sample disagree on the race tick")
        position = tuple(float(value) for value in vehicle.position)
        speed = float(vehicle.speed)
        race_time_s = float(race.race_time_ms) / 1000.0
        if len(position) != 3 or not all(math.isfinite(value) for value in (*position, speed, race_time_s)):
            raise RuntimeError("TICK returned invalid vehicle or race telemetry")
        self._sequence += 1
        return Sample(
            telemetry=Telemetry(
                speed=speed,
                speed_unit="m/s",
                position=position,
                finished=bool(raw.finished),
                race_time_s=race_time_s,
                checkpoint_count=int(race.checkpoints_taken),
                gear=int(drivetrain.gear),
                extra={
                    "race_tick": tick,
                    "lap": int(race.lap),
                    "tickgym_steer": int(drivetrain.steer),
                    "tickgym_held_input": int(drivetrain.held),
                },
            ),
            captured_ns=time.monotonic_ns(),
            sequence=self._sequence,
            image=None,
            metadata={
                "backend": "tickgym",
                "clock_mode": "lockstep",
                "race_tick": tick,
                "previous_race_tick": previous_tick,
                "requested_ticks": requested_ticks,
                "simulation_ticks": advanced,
                "simulation_dt_s": advanced * _TICK_SECONDS,
                "simulation_elapsed_s": (tick + 1) * _TICK_SECONDS,
                "tick_seconds": _TICK_SECONDS,
                "rgb_available": False,
            },
        )

    def reset(self, seed: int | None = None) -> Sample:
        """Reset to the server's tick -1 baseline; ``None`` uses seed zero."""
        self._require_open()
        seed = _integer(0 if seed is None else seed, "seed", 0, 0xFFFFFFFF)
        try:
            raw = self._gym.reset(seed=seed)
            self._last = self._sample(raw, previous_tick=-1, requested_ticks=0)
            if self._last.telemetry.finished:
                raise RuntimeError("TICK returned a finished race immediately after reset")
            return self._last
        except BaseException as error:
            self._close_after_failure(error)
            raise

    def observe(self) -> Sample:
        """Return the last sample without sending a request or advancing time."""
        return self._require_sample()

    def apply_action(self, action: Action) -> Action:
        raise NotImplementedError("TickGymBackend requires advance(action, ticks) for lockstep control")

    def advance(self, action: Action, ticks: int) -> tuple[Sample, Action]:
        """Apply quantized controls for 1..60000 ticks, stopping at finish."""
        previous = self._require_sample()
        ticks = _integer(ticks, "ticks", 1, 60000)
        if not isinstance(action, Action):
            raise TypeError("action must be a trackmaniabench.live.Action")
        if previous.telemetry.finished:
            raise RuntimeError("The race has finished; reset() before advancing again")
        steer = int(round(float(action.steer) * 127))
        accelerate, brake = action.throttle >= 0.5, action.brake >= 0.5
        applied = Action(steer=steer / 127.0, throttle=float(accelerate), brake=float(brake))
        try:
            raw = self._gym.step(ticks, steer=steer, accelerate=accelerate, brake=brake)
            self._last = self._sample(
                raw, previous_tick=previous.metadata["race_tick"], requested_ticks=ticks
            )
            return self._last, applied
        except BaseException as error:
            self._close_after_failure(error)
            raise

    def _close_after_failure(self, error: BaseException) -> None:
        try:
            self.close()
        except BaseException as cleanup_error:
            error.add_note(f"TICK resource cleanup also failed: {cleanup_error}")

    def close(self) -> None:
        """Close the owned gym and session, attempting both even if one fails."""
        if self._closed:
            return
        self._closed = True
        self._last = None
        first_error = None
        for resource in (self._gym, self._session):
            if resource is not None:
                try:
                    resource.close()
                except BaseException as error:
                    if first_error is None:
                        first_error = error
                    else:
                        first_error.add_note(f"Additional TICK resource cleanup failure: {error}")
        self._gym = None
        self._session = None
        if first_error is not None:
            raise first_error

    def __enter__(self) -> TickGymBackend:
        self._require_open()
        return self

    def __exit__(self, exception_type, error, traceback) -> None:
        if error is None:
            self.close()
        else:
            self._close_after_failure(error)
