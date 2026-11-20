"""Small, framework-independent contracts for live Trackmania backends."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Protocol

import numpy as np


class ActionLeaseExpired(RuntimeError):
    """The previous action expired and controls were successfully neutralized."""


@dataclass(frozen=True)
class Action:
    """Requested controls: steering [-1, 1], accelerator/brake [0, 1]."""

    steer: float = 0.0
    throttle: float = 0.0
    brake: float = 0.0

    def __post_init__(self):
        for name, low, high in (("steer", -1, 1), ("throttle", 0, 1), ("brake", 0, 1)):
            value = getattr(self, name)
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{name} must be finite and in [{low}, {high}]")

    @classmethod
    def from_array(cls, values):
        array = np.asarray(values, dtype=np.float64)
        if array.shape != (3,):
            raise ValueError("An action must have shape (3,): steer, throttle, brake")
        return cls(*(float(v) for v in array))

    def array(self):
        return np.array((self.steer, self.throttle, self.brake), dtype=np.float32)


@dataclass(frozen=True)
class Capabilities:
    clock_mode: str
    rgb: bool
    action_profile: str
    tick_seconds: float | None = None

    def __post_init__(self):
        if self.clock_mode not in {"realtime", "lockstep"}:
            raise ValueError("Unknown clock mode")
        if self.clock_mode == "lockstep" and (
            self.tick_seconds is None or not math.isfinite(self.tick_seconds) or self.tick_seconds <= 0
        ):
            raise ValueError("Lockstep requires a positive simulation tick duration")


@dataclass(frozen=True)
class Telemetry:
    speed: float
    position: tuple[float, float, float]
    finished: bool = False
    race_time_s: float | None = None
    checkpoint_count: int | None = None
    gear: int | None = None
    rpm: float | None = None
    extra: dict = field(default_factory=dict)
    speed_unit: str = "unspecified"


@dataclass(frozen=True)
class Sample:
    """One observation. Host monotonic time is not a simulation timestamp.

    RGB and telemetry may have different sampling times. Backends preserve this
    skew and their source limitations in metadata rather than fabricate sync.
    """

    telemetry: Telemetry
    captured_ns: int
    sequence: int
    image: np.ndarray | None = None
    metadata: dict = field(default_factory=dict)


class Backend(Protocol):
    capabilities: Capabilities

    def reset(self, seed: int | None = None) -> Sample: ...
    def observe(self) -> Sample: ...
    def apply_action(self, action: Action) -> Action: ...
    def advance(self, action: Action, ticks: int) -> tuple[Sample, Action]: ...
    def close(self) -> None: ...
