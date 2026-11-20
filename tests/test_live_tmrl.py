"""No desktop, uinput, game, or optional live dependency is used by these tests."""

from __future__ import annotations

from collections import deque
import socket
import struct
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from trackmaniabench.live.controls import ControlError, GamepadController, KeyboardController
from trackmaniabench.live.tmrl import (
    MSSCapture,
    TMRLBackend,
    TMRLTelemetrySource,
    TelemetryError,
    TelemetryPacket,
)
from trackmaniabench.live.types import Action, ActionLeaseExpired


def values(*, speed=0.0, position=(10.0, 20.0, 30.0), finished=0.0):
    return (speed, 0.0, *position, 0.0, 0.0, 0.0, finished, 1.0, 1000.0)


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Timed out waiting for background operation")
        threading.Event().wait(0.005)


class FakeTelemetry:
    def __init__(self, rows=()):
        self.rows = deque(rows)
        self.last_sequence = 0
        self.latest_received_ns = time.monotonic_ns()
        self.failure = None
        self.closed = False
        self.read_calls = []

    def read(self, **kwargs):
        self.read_calls.append(kwargs)
        if self.failure:
            raise TelemetryError(str(self.failure))
        row = self.rows.popleft() if self.rows else values()
        if isinstance(row, Exception):
            raise row
        self.last_sequence += 1
        self.latest_received_ns = time.monotonic_ns()
        return TelemetryPacket(row, self.latest_received_ns, self.last_sequence)

    def close(self):
        self.closed = True


class FakeInput:
    action_profile = "test:quantized"

    def __init__(self):
        self.releases = 0
        self.resets = 0
        self.actions = []
        self.fail = False
        self.closed = False

    def release(self):
        self.releases += 1

    def reset(self):
        self.resets += 1

    def apply(self, action):
        self.actions.append(action)
        if self.fail:
            raise ControlError("partial input failure")
        return Action(round(action.steer), round(action.throttle), round(action.brake))

    def close(self):
        self.release()
        self.closed = True


class FakeCapture:
    def __init__(self):
        self.closed = False
        self.image = np.zeros((4, 6, 3), dtype=np.uint8)

    def capture(self):
        return self.image

    def close(self):
        self.closed = True


class TelemetryTests(unittest.TestCase):
    def make_source(self):
        client, server = socket.socketpair()
        source = TMRLTelemetrySource(timeout=0.25, sock=client)
        self.addCleanup(server.close)
        self.addCleanup(source.close)
        return source, server

    def test_partial_and_coalesced_packets_keep_framing_and_only_latest(self):
        source, server = self.make_source()
        raw = struct.pack("<11f", *values(speed=7.5))
        server.sendall(raw[:13])
        with self.assertRaises(TelemetryError):
            source.read(timeout=0.02)
        server.sendall(raw[13:])
        first = source.read()
        self.assertEqual(first.sequence, 1)
        self.assertEqual(first.values[0], 7.5)
        server.sendall(struct.pack("<11f", *values(speed=9.0)) + struct.pack("<11f", *values(speed=12.0)))
        wait_until(lambda: source.last_sequence == 3)
        packet = source.read(after_sequence=1)
        self.assertEqual(packet.sequence, 3)
        self.assertEqual(packet.values[0], 12.0)
        self.assertGreaterEqual(packet.received_ns, first.received_ns)
        telemetry = packet.as_telemetry()
        self.assertEqual(telemetry.speed_unit, "unspecified")
        self.assertIsNone(telemetry.race_time_s)
        self.assertEqual(telemetry.position, (10.0, 20.0, 30.0))

    def test_closed_connection_invalidates_previously_received_packet(self):
        source, server = self.make_source()
        server.sendall(struct.pack("<11f", *values()))
        source.read()
        server.close()
        wait_until(lambda: source.failure is not None)
        with self.assertRaisesRegex(TelemetryError, "disconnected"):
            source.read()

    def test_partial_eof_is_a_protocol_failure(self):
        source, server = self.make_source()
        server.sendall(b"\x00" * 7)
        server.close()
        wait_until(lambda: source.failure is not None)
        with self.assertRaisesRegex(TelemetryError, "partial"):
            source.read()

    def test_invalid_nan_and_boolean_records_are_rejected(self):
        for field, bad in ((0, float("nan")), (8, 0.5), (9, 1.5)):
            with self.subTest(field=field):
                source, server = self.make_source()
                row = list(values())
                row[field] = bad
                server.sendall(struct.pack("<11f", *row))
                wait_until(lambda: source.failure is not None)
                with self.assertRaises(TelemetryError):
                    source.read()

    def test_receipt_and_sequence_barriers_require_new_data(self):
        source, server = self.make_source()
        server.sendall(struct.pack("<11f", *values()))
        p = source.read()
        with self.assertRaises(TelemetryError):
            source.read(after_sequence=p.sequence, timeout=0.02)
        with self.assertRaises(TelemetryError):
            source.read(min_received_ns=p.received_ns + 1, timeout=0.02)

    def test_finish_event_is_preserved_when_newer_packet_is_not_finished(self):
        source, server = self.make_source()
        server.sendall(struct.pack("<11f", *values(finished=1)) + struct.pack("<11f", *values()))
        wait_until(lambda: source.last_sequence == 2)
        packet = source.read()
        self.assertFalse(packet.as_telemetry().finished)
        self.assertEqual(packet.finish_sequence, 1)

    def test_old_latest_record_is_not_fresh(self):
        source, server = self.make_source()
        server.sendall(struct.pack("<11f", *values()))
        p = source.read()
        with source._condition:
            source._latest = TelemetryPacket(p.values, time.monotonic_ns() - 10**9, p.sequence)
        with self.assertRaisesRegex(TelemetryError, "fresh"):
            source.read(timeout=0.02)

    def test_close_unblocks_waiters(self):
        source, _ = self.make_source()
        failures = []
        started = threading.Event()

        def read():
            started.set()
            try:
                source.read(timeout=2)
            except Exception as exc:
                failures.append(exc)

        thread = threading.Thread(target=read)
        thread.start()
        started.wait(1)
        source.close()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(failures[0], TelemetryError)
        source.close()


class BackendTests(unittest.TestCase):
    def make_backend(self, *, telemetry=None, capture=None, **kwargs):
        self.source = telemetry or FakeTelemetry()
        self.input = FakeInput()
        self.capture = capture or FakeCapture()
        opts = dict(
            telemetry_source=self.source,
            input_device=self.input,
            capture=self.capture,
            expected_start_position=(10, 20, 30),
            reset_stable_samples=2,
            timeout=0.3,
        )
        opts.update(kwargs)
        backend = TMRLBackend(**opts)
        self.addCleanup(backend.close)
        return backend

    def test_reset_requires_anchor_and_actions_require_verified_reset(self):
        backend = self.make_backend(expected_start_position=None)
        self.assertFalse(backend.observe().metadata["reset_verified"])
        with self.assertRaisesRegex(ValueError, "expected_start_position"):
            backend.reset()
        with self.assertRaisesRegex(ControlError, "verified reset"):
            backend.apply_action(Action(throttle=1))
        self.assertEqual(self.input.actions, [])

    def test_reset_requires_consecutive_fresh_start_samples(self):
        backend = self.make_backend(
            telemetry=FakeTelemetry(
                [
                    values(position=(100, 20, 30)),
                    values(),
                    values(speed=20),
                    values(),
                    values(finished=1),
                    values(),
                    values(),
                ]
            )
        )
        sample = backend.reset(seed=42)
        self.assertEqual(sample.sequence, 7)
        self.assertEqual(self.input.resets, 1)
        self.assertTrue(sample.metadata["reset_verified"])
        self.assertFalse(sample.metadata["seed_applied"])
        self.assertEqual(sample.metadata["seed_requested"], 42)
        self.assertGreater(self.source.read_calls[0]["min_received_ns"], 0)
        self.assertTrue(
            all(
                a["after_sequence"] < b["after_sequence"]
                for a, b in zip(self.source.read_calls, self.source.read_calls[1:])
            )
        )

    def test_reset_error_releases_and_blocks_actions(self):
        backend = self.make_backend(telemetry=FakeTelemetry([TelemetryError("no packets")]))
        with self.assertRaises(TelemetryError):
            backend.reset()
        self.assertGreaterEqual(self.input.releases, 2)
        with self.assertRaises(ControlError):
            backend.apply_action(Action(throttle=1))

    def test_reset_cannot_complete_at_wrong_position(self):
        class WrongStart(FakeTelemetry):
            def read(self, **kwargs):
                self.rows.append(values(position=(1000, 0, 0)))
                return super().read(**kwargs)

        backend = self.make_backend(telemetry=WrongStart(), reset_timeout=0.02)
        with self.assertRaisesRegex(TelemetryError, "Reset was not verified"):
            backend.reset()

    def test_observation_carries_real_receipt_capture_times_and_quantized_action(self):
        backend = self.make_backend()
        first = backend.reset()
        actual = backend.apply_action(Action(0.7, 0.7, 0.2))
        self.assertEqual(actual, Action(1, 1, 0))
        sample = backend.observe()
        self.assertGreater(sample.sequence, first.sequence)
        self.assertTrue(sample.metadata["transition_valid"])
        self.assertIsNone(sample.telemetry.race_time_s)
        self.assertLessEqual(sample.metadata["telemetry_received_ns"], sample.metadata["capture_started_ns"])
        self.assertLessEqual(sample.metadata["capture_started_ns"], sample.captured_ns)
        self.assertEqual(sample.metadata["capture_ended_ns"], sample.captured_ns)
        self.assertEqual(sample.metadata["applied_action"], [1, 1, 0])
        self.assertEqual(self.source.read_calls[-1]["min_received_ns"], backend._last_action_ns)
        self.capture.image[:] = 255
        self.assertEqual(int(sample.image.max()), 0)

    def test_lease_expires_without_caller_activity_and_requires_reset(self):
        backend = self.make_backend(action_lease_seconds=0.03)
        backend.reset()
        backend.apply_action(Action(throttle=1))
        previous = self.input.releases
        wait_until(lambda: self.input.releases > previous)
        sample = backend.observe()
        self.assertTrue(sample.metadata["action_lease_expired"])
        self.assertFalse(sample.metadata["transition_valid"])
        with self.assertRaises(ActionLeaseExpired):
            backend.apply_action(Action(throttle=1))
        backend.reset()
        self.assertEqual(backend.apply_action(Action()), Action())

    def test_disconnect_releases_even_without_observe(self):
        backend = self.make_backend()
        backend.reset()
        backend.apply_action(Action(throttle=1))
        previous = self.input.releases
        self.source.failure = TelemetryError("peer gone")
        wait_until(lambda: self.input.releases > previous)
        with self.assertRaises(ControlError):
            backend.apply_action(Action(throttle=1))

    def test_failed_watchdog_release_retries_until_controls_are_neutral(self):
        backend = self.make_backend(action_lease_seconds=0.03)
        backend.reset()
        original_release = self.input.release
        attempts = []

        def transient_release():
            attempts.append(True)
            if len(attempts) == 1:
                raise OSError("transient input failure")
            original_release()

        self.input.release = transient_release
        backend.apply_action(Action(throttle=1))
        wait_until(lambda: len(attempts) >= 2)
        sample = backend.observe()
        self.assertIsNone(sample.metadata["control_release_error"])
        self.assertFalse(sample.metadata["transition_valid"])
        self.assertEqual(sample.metadata["applied_action"], [0, 0, 0])

    def test_environment_can_reset_after_lease_expires_during_inference(self):
        from trackmaniabench.live import LiveTrackManiaEnv

        backend = self.make_backend(action_lease_seconds=0.05)
        env = LiveTrackManiaEnv(backend, control_hz=100)
        env.reset()
        env.step(Action(throttle=1))
        wait_until(lambda: backend._fault == "action_lease_expired")
        calls = len(self.input.actions)
        _, _, term, trunc, info = env.step(Action(throttle=1))
        self.assertFalse(term)
        self.assertTrue(trunc)
        self.assertFalse(info["transition_valid"])
        self.assertFalse(info["requested_action_written"])
        self.assertIsNone(info["input_write_completed_ns"])
        self.assertEqual(len(self.input.actions), calls)
        env.reset()
        self.assertTrue(backend._reset_verified)

    def test_observe_rejects_packet_received_before_action_write(self):
        client, server = socket.socketpair()
        source = TMRLTelemetrySource(timeout=0.1, sock=client)
        self.addCleanup(server.close)
        backend = self.make_backend(telemetry=source, timeout=0.1)
        # Establish the already-verified reset boundary independently of this
        # packet. No subsequent producer packet arrives after applying input.
        backend._reset_verified = True
        server.sendall(struct.pack("<11f", *values()))
        source.read()
        backend.apply_action(Action(throttle=1))
        with self.assertRaisesRegex(TelemetryError, "fresh"):
            backend.observe()

    def test_latched_finish_survives_latest_packet_and_clears_on_reset(self):
        backend = self.make_backend()
        backend.reset()
        original_read = self.source.read

        def read_with_finish(**kwargs):
            p = original_read(**kwargs)
            return TelemetryPacket(p.values, p.received_ns, p.sequence, 3)

        self.source.read = read_with_finish
        sample = backend.observe()
        self.assertTrue(sample.telemetry.finished)
        self.assertTrue(sample.metadata["finish_event_latched"])
        self.assertFalse(backend.reset().telemetry.finished)
        self.assertFalse(backend.observe().telemetry.finished)

    def test_stale_telemetry_releases_without_model_return(self):
        backend = self.make_backend(timeout=0.03)
        backend.reset()
        backend.apply_action(Action(throttle=1))
        previous = self.input.releases
        wait_until(lambda: self.input.releases > previous)
        sample = backend.observe()
        self.assertEqual(sample.metadata["control_fault"], "telemetry_stale")
        self.assertFalse(sample.metadata["transition_valid"])

    def test_partial_action_is_not_retried(self):
        backend = self.make_backend()
        backend.reset()
        self.input.fail = True
        with self.assertRaises(ControlError):
            backend.apply_action(Action(throttle=1))
        with self.assertRaises(ControlError):
            backend.apply_action(Action(throttle=1))
        self.assertEqual(len(self.input.actions), 1)
        self.assertFalse(backend.observe().metadata["transition_valid"])

    def test_bad_capture_neutralizes_held_controls(self):
        backend = self.make_backend()
        backend.reset()
        backend.apply_action(Action(throttle=1))
        self.capture.image = np.zeros((4, 6, 4), dtype=np.uint8)
        previous = self.input.releases
        with self.assertRaisesRegex(ValueError, "RGB"):
            backend.observe()
        self.assertGreater(self.input.releases, previous)

    def test_close_releases_and_closes_all_components(self):
        backend = self.make_backend()
        backend.reset()
        backend.apply_action(Action(throttle=1))
        backend.close()
        self.assertTrue(self.input.closed)
        self.assertTrue(self.capture.closed)
        self.assertTrue(self.source.closed)
        self.assertFalse(backend._watchdog.is_alive())
        with self.assertRaisesRegex(RuntimeError, "closed"):
            backend.observe()
        backend.close()

    def test_no_fake_exact_clock(self):
        backend = self.make_backend()
        with self.assertRaises(NotImplementedError):
            backend.advance(Action(), 10)

    def test_desktop_region_is_never_implicitly_selected(self):
        with self.assertRaisesRegex(ValueError, "capture_region"):
            TMRLBackend(input_device=FakeInput(), telemetry_source=FakeTelemetry())


class FakePad:
    def __init__(self):
        self.calls = []
        self.fail_update = False

    def __getattr__(self, name):
        def invoke(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "update" and self.fail_update:
                self.fail_update = False
                raise OSError("device disconnected")

        return invoke


class InputTests(unittest.TestCase):
    def test_gamepad_reports_actual_integer_quantization_and_b_reset(self):
        pad = FakePad()
        control = GamepadController(gamepad=pad, sleep=lambda _: None)
        action = control.apply(Action(0.1234, 0.51, 0.1))
        self.assertEqual(action.steer, round(0.1234 * 32767) / 32767)
        self.assertEqual(action.throttle, 130 / 255)
        self.assertEqual(action.brake, 26 / 255)
        control.reset()
        self.assertIn(("press_button", (), {"button": 0x2000}), pad.calls)
        self.assertIn(("release_button", (), {"button": 0x2000}), pad.calls)
        control.close()

    def test_gamepad_update_failure_neutralizes_without_retry(self):
        pad = FakePad()
        control = GamepadController(gamepad=pad, sleep=lambda _: None)
        pad.fail_update = True
        with self.assertRaises(ControlError):
            control.apply(Action(throttle=1))
        names = [x[0] for x in pad.calls]
        self.assertEqual(names.count("right_trigger"), 1)
        self.assertEqual(names[-2:], ["reset", "update"])

    def make_keyboard(self):
        calls = []
        state = {"focused": "123", "fail_key": None}

        def run(argv, **kwargs):
            calls.append(argv)
            command = argv[1]
            if command == "search":
                return subprocess.CompletedProcess(argv, 0, "123\n")
            if command == "getwindowfocus":
                return subprocess.CompletedProcess(argv, 0, state["focused"])
            if command == "keydown" and argv[-1] == state["fail_key"]:
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 0, "")

        control = KeyboardController(123, runner=run, sleep=lambda _: None)
        return control, calls, state

    def test_keyboard_quantizes_and_checks_focus(self):
        control, calls, state = self.make_keyboard()
        self.assertEqual(control.apply(Action(-0.8, 0.7, 0.2)), Action(-1, 1, 0))
        self.assertIn(["xdotool", "keydown", "Left"], calls)
        self.assertIn(["xdotool", "keyup", "Right"], calls)
        state["focused"] = "456"
        start = len(calls)
        with self.assertRaises(ControlError):
            control.apply(Action(throttle=1))
        self.assertFalse(any(c[1] == "keydown" for c in calls[start:]))
        self.assertEqual(len([c for c in calls[start:] if c[1] == "keyup"]), 5)

    def test_keyboard_partial_command_failure_releases_every_key(self):
        control, calls, state = self.make_keyboard()
        state["fail_key"] = "Up"
        with self.assertRaises(ControlError):
            control.apply(Action(throttle=1))
        self.assertEqual(calls.count(["xdotool", "keydown", "Up"]), 1)
        released = {c[-1] for c in calls[-5:]}
        self.assertEqual(released, {"Up", "Down", "Left", "Right", "BackSpace"})

    def test_keyboard_reset_uses_backspace(self):
        control, calls, _ = self.make_keyboard()
        control.reset()
        self.assertIn(["xdotool", "keydown", "BackSpace"], calls)
        self.assertIn(["xdotool", "keyup", "BackSpace"], calls)

    def test_window_id_cannot_inject_options(self):
        for value in ("--sync", "12; touch pwn", "0", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                KeyboardController(value)

    def test_capture_bgra_is_converted_to_rgb(self):
        class FakeScreen:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def grab(self, region):
                return np.array([[[1, 2, 3, 255]]], dtype=np.uint8)

        fake_mss = type("MSS", (), {"mss": staticmethod(lambda **kwargs: FakeScreen())})
        with patch.dict("sys.modules", {"mss": fake_mss}):
            capture = MSSCapture({"left": 0, "top": 0, "width": 1, "height": 1}, (1, 1))
            self.assertEqual(capture.capture().tolist(), [[[3, 2, 1]]])


if __name__ == "__main__":
    unittest.main()
