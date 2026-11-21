"""Real-time local Trackmania via the unchanged signed TMRL GrabData plugin.

The plugin's 44-byte records do not contain a game clock, sequence, or map ID.
All timing supplied here is host receipt/capture timing, never simulation time.
The game continues to run while the caller predicts its next action.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import socket
import struct
import threading
import time

import numpy as np
from PIL import Image

from .controls import ControlError, GamepadController, KeyboardController
from .types import Action, ActionLeaseExpired, Capabilities, Sample, Telemetry

_PACKET = struct.Struct("<11f")


class TelemetryError(RuntimeError):
    """Telemetry is unavailable, disconnected, malformed, or stale."""


@dataclass(frozen=True)
class TelemetryPacket:
    values: tuple[float, ...]
    received_ns: int
    sequence: int
    finish_sequence: int = 0

    def as_telemetry(self):
        v = self.values
        return Telemetry(
            speed=v[0],
            position=(v[2], v[3], v[4]),
            finished=bool(v[8]),
            gear=int(v[9]),
            rpm=v[10],
            speed_unit="unspecified",
            extra={
                "distance": v[1],
                "distance_unit": "unspecified",
                "reported_steer": v[5],
                "reported_throttle": v[6],
                "reported_brake": v[7],
                "source": "TMRL_GrabData:CSmScriptPlayer",
                "position_unit": "game_world_units",
            },
        )


class TMRLTelemetrySource:
    """Continuously drain TCP into one latest packet, with bounded memory.

    The single receiver owns the byte framing. Sequence numbers count received
    records, including those superseded before the policy observed them; they
    are not game-frame numbers. A disconnected stream is never retried silently.
    """

    def __init__(self, host="127.0.0.1", port=9000, *, timeout=2.0, sock=None):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self.timeout = timeout
        self._condition = threading.Condition()
        self._latest = None
        self._sequence = 0
        self._finish_sequence = 0
        self._failure = None
        self._closed = False
        try:
            self._socket = sock if sock is not None else socket.create_connection((host, port), timeout)
            self._socket.settimeout(min(timeout, 0.2))
        except OSError as exc:
            raise TelemetryError(
                f"Cannot connect to TMRL GrabData at {host}:{port}. Load the unchanged "
                "signed plugin in Openplanet Regular mode and enter a playable map."
            ) from exc
        self._thread = threading.Thread(target=self._receive, name="tmbench-telemetry", daemon=True)
        self._thread.start()

    @property
    def failure(self):
        with self._condition:
            return self._failure

    @property
    def last_sequence(self):
        with self._condition:
            return self._sequence

    @property
    def latest_received_ns(self):
        with self._condition:
            return None if self._latest is None else self._latest.received_ns

    @property
    def last_finish_sequence(self):
        """Last observed finish, including packets superseded before a read."""
        with self._condition:
            return self._finish_sequence

    def _receive(self):
        pending = bytearray()
        try:
            while True:
                with self._condition:
                    if self._closed:
                        return
                try:
                    block = self._socket.recv(65536)
                except socket.timeout:
                    continue
                if not block:
                    raise TelemetryError(
                        "TMRL telemetry disconnected" + (" during a partial packet" if pending else "")
                    )
                received_ns = time.monotonic_ns()
                pending.extend(block)
                count = len(pending) // _PACKET.size
                if not count:
                    continue
                latest = None
                for offset in range(0, count * _PACKET.size, _PACKET.size):
                    values = _PACKET.unpack_from(pending, offset)
                    if not all(math.isfinite(v) for v in values):
                        raise TelemetryError("TMRL telemetry contains non-finite values")
                    if values[7] not in (0.0, 1.0) or values[8] not in (0.0, 1.0):
                        raise TelemetryError("TMRL boolean fields are invalid; check the plugin protocol")
                    if values[9] != int(values[9]) or not 0 <= values[9] <= 20:
                        raise TelemetryError("TMRL gear field is invalid; check the plugin protocol")
                    latest = values
                    if values[8]:
                        self._finish_sequence = self._sequence + offset // _PACKET.size + 1
                del pending[: count * _PACKET.size]
                with self._condition:
                    self._sequence += count
                    self._latest = TelemetryPacket(latest, received_ns, self._sequence, self._finish_sequence)
                    self._condition.notify_all()
        except Exception as exc:
            with self._condition:
                if not self._closed:
                    self._failure = exc
                self._condition.notify_all()

    def read(self, *, after_sequence=0, min_received_ns=0, timeout=None):
        limit = self.timeout if timeout is None else timeout
        if not math.isfinite(limit) or limit <= 0:
            raise ValueError("read timeout must be positive and finite")
        deadline = time.monotonic() + limit
        with self._condition:
            while True:
                if self._closed:
                    raise TelemetryError("Telemetry source is closed")
                if self._failure is not None:
                    raise TelemetryError(str(self._failure)) from self._failure
                p = self._latest
                now_ns = time.monotonic_ns()
                if (
                    p is not None
                    and p.sequence > after_sequence
                    and p.received_ns >= min_received_ns
                    and now_ns - p.received_ns <= self.timeout * 1e9
                ):
                    return p
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TelemetryError("Timed out waiting for fresh TMRL telemetry")
                self._condition.wait(remaining)

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._socket.close()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)


class MSSCapture:
    """Capture an explicitly selected desktop rectangle as RGB uint8.

    No window is moved, resized, or chosen implicitly. The caller is responsible
    for keeping the game visible and setting its intended chase camera.
    """

    def __init__(self, region, image_size=(224, 224), *, display=None):
        if not isinstance(region, dict) or set(region) != {"left", "top", "width", "height"}:
            raise ValueError("capture_region must contain exactly left, top, width, height")
        if any(isinstance(v, bool) or not isinstance(v, int) for v in region.values()):
            raise ValueError("Capture rectangle coordinates must be integers")
        if region["width"] <= 0 or region["height"] <= 0:
            raise ValueError("Capture rectangle width and height must be positive")
        if len(image_size) != 2 or any(
            isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in image_size
        ):
            raise ValueError("image_size must be two positive integers (width, height)")
        try:
            import mss
        except ImportError as exc:
            raise RuntimeError("RGB capture requires TrackManiaBench[live] (mss)") from exc
        self._mss = mss
        self._region = dict(region)
        self._image_size = tuple(image_size)
        self._display = display

    def capture(self):
        # mss keeps thread-local X11 handles. A per-call context also permits
        # independent observe/reset callers without sharing those handles.
        kwargs = {} if self._display is None else {"display": self._display}
        with self._mss.mss(**kwargs) as screen:
            bgra = np.asarray(screen.grab(self._region))
            rgb = np.ascontiguousarray(bgra[:, :, [2, 1, 0]], dtype=np.uint8)
        image = Image.fromarray(rgb)
        if image.size != self._image_size:
            image = image.resize(self._image_size, Image.Resampling.BILINEAR)
        return np.asarray(image, dtype=np.uint8).copy()

    def close(self):
        pass


class TMRLBackend:
    """A real-time backend; actions stay held while the policy is busy.

    A reset requires a supplied, independently established start position. Its
    acknowledgment is several fresh low-speed, unfinished observations near
    that anchor. This verifies the starting pose, not a deterministic seed or
    exact race-clock boundary (neither exists in the signed plugin protocol).

    A control lease prevents an abandoned policy from holding an action forever.
    Its expiry or stale/disconnected telemetry neutralizes controls and latches
    a fault until a verified reset. Metadata marks such transitions invalid.
    """

    def __init__(
        self,
        *,
        telemetry_host="127.0.0.1",
        telemetry_port=9000,
        control="gamepad",
        capture_region=None,
        image_size=(224, 224),
        display=None,
        timeout=2.0,
        reset_timeout=10.0,
        expected_start_position=None,
        start_position_tolerance=2.0,
        start_speed_threshold=1.0,
        reset_stable_samples=3,
        reset_settle_seconds=1.5,
        finish_popup_delay_seconds=0.5,
        action_lease_seconds=10.0,
        window_id=None,
        class_pattern="Trackmania",
        reset_key="Delete",
        reset_button=0x2000,
        gamepad_connect_settle_seconds=2.0,
        telemetry_source=None,
        input_device=None,
        capture=None,
    ):
        for name, value in (
            ("timeout", timeout),
            ("reset_timeout", reset_timeout),
            ("start_position_tolerance", start_position_tolerance),
            ("action_lease_seconds", action_lease_seconds),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not math.isfinite(start_speed_threshold) or start_speed_threshold < 0:
            raise ValueError("start_speed_threshold must be finite and non-negative")
        for name, value in (
            ("reset_settle_seconds", reset_settle_seconds),
            ("finish_popup_delay_seconds", finish_popup_delay_seconds),
            ("gamepad_connect_settle_seconds", gamepad_connect_settle_seconds),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if (
            isinstance(reset_stable_samples, bool)
            or not isinstance(reset_stable_samples, int)
            or reset_stable_samples < 2
        ):
            raise ValueError("reset_stable_samples must be an integer >= 2")
        if control not in {"gamepad", "keyboard"}:
            raise ValueError("control must be gamepad or keyboard")
        anchor = (
            None if expected_start_position is None else np.asarray(expected_start_position, dtype=np.float64)
        )
        if anchor is not None and (anchor.shape != (3,) or not np.all(np.isfinite(anchor))):
            raise ValueError("expected_start_position must contain three finite coordinates")
        if capture is None and capture_region is None:
            raise ValueError("Provide an explicit capture_region; the desktop is never selected implicitly")
        if input_device is None and control == "keyboard" and window_id is None:
            raise ValueError("Keyboard control requires an explicit Trackmania window_id")
        self.timeout = float(timeout)
        self.reset_timeout = float(reset_timeout)
        self._anchor = None if anchor is None else anchor.copy()
        self._position_tolerance = start_position_tolerance
        self._speed_threshold = start_speed_threshold
        self._stable_samples = reset_stable_samples
        self._reset_settle_seconds = float(reset_settle_seconds)
        self._finish_popup_delay_seconds = float(finish_popup_delay_seconds)
        self._lease_seconds = float(action_lease_seconds)
        self._control_lock = threading.RLock()
        self._observation_lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = False
        self._fault = None
        self._fault_ns = None
        self._release_error = None
        self._lease_deadline_ns = None
        self._last_action_ns = None
        self._last_action = Action()
        self._action_sequence = 0
        self._last_observed_sequence = 0
        self._reset_finish_sequence = 0
        self._finish_observed = False
        self._reset_ready_after_ns = None
        self._finish_popup_dismissed = False
        self._seed_requested = None
        self._reset_verified = False
        self._telemetry = None
        self._input = None
        self._capture = None
        try:
            self._capture = (
                capture if capture is not None else MSSCapture(capture_region, image_size, display=display)
            )
            self._input = (
                input_device
                if input_device is not None
                else (
                    GamepadController(
                        reset_button=reset_button, connect_settle_seconds=gamepad_connect_settle_seconds
                    )
                    if control == "gamepad"
                    else KeyboardController(
                        window_id, class_pattern=class_pattern, display=display, reset_key=reset_key
                    )
                )
            )
            self._telemetry = (
                telemetry_source
                if telemetry_source is not None
                else TMRLTelemetrySource(telemetry_host, telemetry_port, timeout=timeout)
            )
        except Exception:
            for component in (self._input, self._telemetry, self._capture):
                if component is not None:
                    try:
                        component.close()
                    except Exception:
                        pass
            raise
        self.capabilities = Capabilities("realtime", True, self._input.action_profile)
        self._watchdog = threading.Thread(target=self._watch, name="tmbench-control-watchdog", daemon=True)
        self._watchdog.start()

    def _check_open(self):
        if self._closed:
            raise RuntimeError("TMRL backend is closed")

    def _neutralize(self, reason):
        with self._control_lock:
            if self._fault is None:
                self._fault = reason
                self._fault_ns = time.monotonic_ns()
            self._lease_deadline_ns = None
            try:
                self._input.release()
                self._last_action = Action()
                self._release_error = None
            except Exception as exc:
                self._release_error = str(exc)

    def _watch(self):
        while not self._stop.wait(0.02):
            with self._control_lock:
                if self._closed:
                    continue
                if self._release_error is not None:
                    self._neutralize(self._fault or "control_release_failed")
                    continue
                if self._lease_deadline_ns is None:
                    continue
                now = time.monotonic_ns()
                if self._telemetry.failure is not None:
                    self._neutralize("telemetry_disconnected")
                    continue
                received = self._telemetry.latest_received_ns
                since = self._last_action_ns if received is None else received
                if now - since > self.timeout * 1e9:
                    self._neutralize("telemetry_stale")
                elif now >= self._lease_deadline_ns:
                    self._neutralize("action_lease_expired")

    def apply_action(self, action: Action) -> Action:
        if not isinstance(action, Action):
            raise TypeError("apply_action expects an Action")
        with self._control_lock:
            self._check_open()
            if self._fault is not None:
                if self._fault == "action_lease_expired" and self._release_error is None:
                    raise ActionLeaseExpired("Previous action lease expired; controls are neutral")
                raise ControlError(f"Control fault {self._fault}; a verified reset is required")
            if not self._reset_verified:
                raise ControlError("A verified reset is required before applying policy actions")
            if self._telemetry.failure is not None:
                self._neutralize("telemetry_disconnected")
                raise TelemetryError("Telemetry disconnected before action")
            received = self._telemetry.latest_received_ns
            if received is None or time.monotonic_ns() - received > self.timeout * 1e9:
                self._neutralize("telemetry_stale")
                raise TelemetryError("Fresh telemetry is required before an action")
            try:
                actual = self._input.apply(action)
            except Exception:
                self._neutralize("action_application_failed")
                raise
            if not isinstance(actual, Action):
                self._neutralize("action_application_failed")
                raise ControlError("Input device did not return its actual quantized Action")
            self._last_action_ns = time.monotonic_ns()
            self._lease_deadline_ns = self._last_action_ns + int(self._lease_seconds * 1e9)
            self._last_action = actual
            self._action_sequence += 1
            return actual

    def _sample(self, packet):
        capture_started_ns = time.monotonic_ns()
        try:
            image = self._capture.capture()
            if (
                not isinstance(image, np.ndarray)
                or image.dtype != np.uint8
                or image.ndim != 3
                or image.shape[2] != 3
            ):
                raise ValueError("Capture must produce an H x W x 3 RGB uint8 array")
            image = image.copy()
        except Exception:
            self._neutralize("capture_failed")
            raise
        capture_ended_ns = time.monotonic_ns()
        if self._telemetry.failure is not None:
            self._neutralize("telemetry_disconnected")
            raise TelemetryError("Telemetry disconnected during image capture")
        if capture_ended_ns - packet.received_ns > self.timeout * 1e9:
            self._neutralize("telemetry_stale")
            raise TelemetryError("Telemetry became stale during image capture")
        with self._control_lock:
            metadata = {
                "clock_mode": "realtime",
                "timing_source": "host_monotonic",
                "simulation_time_available": False,
                "telemetry_received_ns": packet.received_ns,
                "capture_started_ns": capture_started_ns,
                "capture_ended_ns": capture_ended_ns,
                "telemetry_to_capture_end_ns": capture_ended_ns - packet.received_ns,
                "telemetry_sequence_is_host_generated": True,
                "action_sequence": self._action_sequence,
                "action_reported_ns": self._last_action_ns,
                "action_lease_deadline_ns": self._lease_deadline_ns,
                "applied_action": self._last_action.array().tolist(),
                "control_fault": self._fault,
                "control_fault_ns": self._fault_ns,
                "control_release_error": self._release_error,
                "action_lease_expired": self._fault == "action_lease_expired",
                "transition_valid": self._fault is None and self._reset_verified,
                "reset_verified": self._reset_verified,
                "reset_verification": "start_pose_low_speed_unfinished",
                "reset_binding": getattr(self._input, "reset_binding", "custom"),
                "race_clock_reset_verified": False,
                "reset_settle_seconds": self._reset_settle_seconds,
                "reset_ready_after_ns": self._reset_ready_after_ns,
                "finish_popup_dismissed": self._finish_popup_dismissed,
                "race_ready_verified": False,
                "seed_requested": self._seed_requested,
                "seed_applied": False,
                "map_identity_available": False,
            }
        telemetry = packet.as_telemetry()
        if packet.finish_sequence > self._reset_finish_sequence:
            telemetry = replace(telemetry, finished=True)
        if telemetry.finished:
            with self._control_lock:
                self._finish_observed = True
        metadata["finish_event_latched"] = telemetry.finished and not bool(packet.values[8])
        return Sample(telemetry, capture_ended_ns, packet.sequence, image, metadata)

    def observe(self) -> Sample:
        self._check_open()
        with self._observation_lock:
            try:
                packet = self._telemetry.read(
                    after_sequence=self._last_observed_sequence,
                    min_received_ns=self._last_action_ns or 0,
                    timeout=self.timeout,
                )
            except Exception:
                self._neutralize("telemetry_unavailable")
                raise
            sample = self._sample(packet)
            self._last_observed_sequence = packet.sequence
            return sample

    def reset(self, seed=None) -> Sample:
        self._check_open()
        if self._anchor is None:
            raise ValueError(
                "Verified reset requires expected_start_position from an independently "
                "confirmed map start. Observe after a manual reset to establish the anchor."
            )
        with self._observation_lock:
            with self._control_lock:
                self._reset_verified = False
                self._lease_deadline_ns = None
                self._seed_requested = seed
                self._last_action = Action()
                self._reset_ready_after_ns = None
                self._finish_popup_dismissed = False
                finish_observed = self._finish_observed or (
                    getattr(self._telemetry, "last_finish_sequence", 0) > self._reset_finish_sequence
                )
                try:
                    self._input.release()
                    if finish_observed and not callable(
                        getattr(self._input, "dismiss_finish_popup", None)
                    ):
                        raise ControlError(
                            "A finish was observed but this input device cannot dismiss its popup. "
                            "Provide input_device.dismiss_finish_popup(), or close the popup "
                            "manually and create a new backend."
                        )
                    self._input.reset()
                except Exception:
                    self._neutralize("reset_input_failed")
                    raise
            try:
                if finish_observed:
                    if self._stop.wait(self._finish_popup_delay_seconds):
                        raise ControlError("Backend closed during finish-popup reset")
                    with self._control_lock:
                        self._check_open()
                        self._input.dismiss_finish_popup()
                        self._finish_popup_dismissed = True
                # Starting pose alone can match during countdown or behind a
                # menu. Wait for the configured countdown interval, then demand
                # new packets. This does not claim to identify menu/race state.
                if self._stop.wait(self._reset_settle_seconds):
                    raise ControlError("Backend closed while waiting for reset to settle")
                with self._control_lock:
                    self._check_open()
                    self._reset_ready_after_ns = time.monotonic_ns()
                    after = self._telemetry.last_sequence
                deadline = time.monotonic() + self.reset_timeout
                stable = 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TelemetryError("Reset was not verified near the configured starting position")
                    packet = self._telemetry.read(
                        after_sequence=after,
                        min_received_ns=self._reset_ready_after_ns,
                        timeout=min(self.timeout, remaining),
                    )
                    after = packet.sequence
                    telemetry = packet.as_telemetry()
                    ready = (
                        not telemetry.finished
                        and abs(telemetry.speed) <= self._speed_threshold
                        and np.linalg.norm(np.asarray(telemetry.position) - self._anchor)
                        <= self._position_tolerance
                    )
                    stable = stable + 1 if ready else 0
                    if stable >= self._stable_samples:
                        break
                with self._control_lock:
                    self._fault = None
                    self._fault_ns = None
                    self._release_error = None
                    self._reset_verified = True
                    self._reset_finish_sequence = packet.finish_sequence
                    self._finish_observed = False
                sample = self._sample(packet)
                self._last_observed_sequence = packet.sequence
                return sample
            except Exception:
                self._neutralize("reset_verification_failed")
                raise

    def advance(self, action: Action, ticks: int):
        raise NotImplementedError(
            "TMRL is a real-time backend and cannot advance an exact number of game ticks. "
            "Use the optional tickgym backend for controller-paced telemetry."
        )

    def close(self):
        with self._control_lock:
            if self._closed:
                return
            self._closed = True
            self._lease_deadline_ns = None
            self._stop.set()
        self._watchdog.join(timeout=1.0)
        errors = []
        # Neutralize before shutting down observation resources. Always attempt
        # every close, even when a device is no longer available.
        for component in (self._input, self._telemetry, self._capture):
            try:
                component.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError("Backend closed with a resource-release failure") from errors[0]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
