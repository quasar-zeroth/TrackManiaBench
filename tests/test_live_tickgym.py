"""Offline contract tests for the optional tickgym lockstep adapter."""

from enum import IntFlag
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from trackmaniabench.live.tickgym_backend import TickGymBackend
from trackmaniabench.live.types import Action


def telemetry(tick=-1, *, finished=False, race_time_ms=None):
    return SimpleNamespace(
        race_tick=tick,
        finished=finished,
        race=SimpleNamespace(
            race_tick=tick,
            race_time_ms=tick * 10 if race_time_ms is None else race_time_ms,
            checkpoints_taken=2 if finished else 0,
            lap=1,
        ),
        vehicle=SimpleNamespace(speed=5.0, position=np.array([1.0, 2.0, 3.0])),
        drivetrain=SimpleNamespace(gear=2, steer=-64, held=1),
    )


class FakeGym:
    def __init__(self):
        self.calls = []
        self.close_count = 0
        self.reset_answer = telemetry()
        self.step_answer = None
        self.step_error = None
        self.reset_error = None
        self.close_error = None
        self.tick = -1

    def reset(self, *, seed):
        self.calls.append(("reset", seed))
        if self.reset_error:
            raise self.reset_error
        self.tick = self.reset_answer.race_tick
        return self.reset_answer

    def step(self, ticks, **kwargs):
        self.calls.append(("step", ticks, kwargs))
        if self.step_error:
            raise self.step_error
        answer = self.step_answer or telemetry(self.tick + ticks)
        self.tick = answer.race_tick
        return answer

    def close(self):
        self.close_count += 1
        if self.close_error:
            raise self.close_error


class FakeSession:
    def __init__(self):
        self.gym = FakeGym()
        self.calls = []
        self.close_count = 0
        self.connect_error = None
        self.close_error = None

    def connect(self, **kwargs):
        self.calls.append(kwargs)
        if self.connect_error:
            raise self.connect_error
        return self.gym

    def close(self):
        self.close_count += 1
        if self.close_error:
            raise self.close_error


class FakeClient:
    def __init__(self):
        self.session = FakeSession()
        self.calls = []

    def open_map_bytes(self, data):
        self.calls.append(data)
        return self.session


class TickGymBackendTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.map_path = Path(directory.name) / "arbitrary-local.Map.Gbx"
        self.map_path.write_bytes(b"GBX local map bytes")
        self.client = FakeClient()
        self.session = self.client.session
        self.gym = self.session.gym

    def backend(self, **kwargs):
        backend = TickGymBackend(self.map_path, client=self.client, **kwargs)
        self.addCleanup(backend.close)
        return backend

    def test_uploads_bytes_and_subscribes_exactly_required_groups(self):
        backend = self.backend(timeout=7.5)
        self.assertEqual(self.client.calls, [b"GBX local map bytes"])
        self.assertEqual(self.session.calls, [{"telemetry": 7, "timeout": 7.5}])
        self.assertEqual(self.gym.calls, [])
        self.assertEqual(backend.capabilities.clock_mode, "lockstep")
        self.assertEqual(backend.capabilities.tick_seconds, 0.01)
        self.assertEqual(backend.capabilities.action_profile, "tick-steer-int8-digital-pedals")
        self.assertFalse(backend.capabilities.rgb)

    def test_injected_client_needs_no_optional_import(self):
        with patch.dict("sys.modules", {"tickgym": None}):
            backend = self.backend()
            backend.reset()
            backend.advance(Action(), 1)

    def test_real_constructor_import_is_lazy_and_dependency_error_is_clear(self):
        with patch.dict("sys.modules", {"tickgym": None}):
            with self.assertRaisesRegex(ImportError, "tickgym==1.1.0"):
                TickGymBackend(self.map_path)

    def test_real_client_arguments_and_enum_mask(self):
        class Groups(IntFlag):
            RACE = 1
            VEHICLE = 2
            DRIVETRAIN = 4

        calls = []

        def factory(**kwargs):
            calls.append(kwargs)
            return self.client

        module = SimpleNamespace(__version__="1.1.0", Client=factory, TelemetryGroup=Groups)
        with patch.dict("sys.modules", {"tickgym": module}):
            with TickGymBackend(self.map_path, origin="http://tick-host:46323", timeout=3.0):
                pass
        self.assertEqual(calls, [{"origin": "http://tick-host:46323", "timeout": 3.0}])
        self.assertIsInstance(self.session.calls[0]["telemetry"], Groups)

    def test_requires_explicit_reset_and_separate_advance(self):
        backend = self.backend()
        with self.assertRaisesRegex(RuntimeError, "reset"):
            backend.observe()
        with self.assertRaisesRegex(RuntimeError, "reset"):
            backend.advance(Action(), 1)
        with self.assertRaisesRegex(NotImplementedError, "advance"):
            backend.apply_action(Action())
        self.assertEqual(self.gym.calls, [])

    def test_reset_preserves_negative_tick_and_actual_race_time(self):
        backend = self.backend()
        sample = backend.reset(seed=123)
        self.assertEqual(self.gym.calls, [("reset", 123)])
        self.assertEqual(sample.metadata["race_tick"], -1)
        self.assertEqual(sample.metadata["simulation_dt_s"], 0.0)
        self.assertEqual(sample.metadata["simulation_elapsed_s"], 0.0)
        self.assertEqual(sample.telemetry.race_time_s, -0.01)
        self.assertEqual(sample.telemetry.speed, 5.0)
        self.assertEqual(sample.telemetry.speed_unit, "m/s")
        self.assertEqual(sample.telemetry.position, (1.0, 2.0, 3.0))
        self.assertEqual(sample.telemetry.gear, 2)
        self.assertIsNone(sample.telemetry.rpm)
        self.assertIsNone(sample.image)

    def test_default_seed_zero_and_reset_restarts_baseline(self):
        backend = self.backend()
        first = backend.reset()
        backend.advance(Action(), 5)
        reset = backend.reset(seed=np.int64(0xFFFFFFFF))
        self.assertEqual(self.gym.calls[0], ("reset", 0))
        self.assertEqual(self.gym.calls[-1], ("reset", 0xFFFFFFFF))
        self.assertEqual(reset.metadata["race_tick"], -1)
        self.assertGreater(reset.sequence, first.sequence)
        sample, _ = backend.advance(Action(), 1)
        self.assertEqual(sample.metadata["race_tick"], 0)
        self.assertEqual(sample.metadata["simulation_dt_s"], 0.01)

    def test_observe_returns_same_cached_sample_without_advancing(self):
        backend = self.backend()
        sample = backend.reset()
        for _ in range(5):
            self.assertIs(backend.observe(), sample)
        self.assertEqual(self.gym.calls, [("reset", 0)])

    def test_quantized_actions_and_first_window_start_at_tick_zero(self):
        backend = self.backend()
        initial = backend.reset()
        sample, applied = backend.advance(Action(steer=-0.5, throttle=0.5, brake=0.4999), 5)
        self.assertEqual(self.gym.calls[-1], ("step", 5, {"steer": -64, "accelerate": True, "brake": False}))
        self.assertEqual(applied, Action(steer=-64 / 127, throttle=1.0, brake=0.0))
        self.assertEqual(sample.metadata["previous_race_tick"], -1)
        self.assertEqual(sample.metadata["race_tick"], 4)
        self.assertEqual(sample.metadata["simulation_ticks"], 5)
        self.assertEqual(sample.metadata["simulation_dt_s"], 0.05)
        self.assertGreater(sample.sequence, initial.sequence)
        self.assertGreaterEqual(sample.captured_ns, initial.captured_ns)

    def test_full_range_steering_and_digital_pedal_boundaries(self):
        backend = self.backend()
        backend.reset()
        for value in (-1.0, 0.0, 1.0):
            with self.subTest(value=value):
                _, applied = backend.advance(Action(steer=value, throttle=0.499, brake=0.5), 1)
                self.assertEqual(applied, Action(value, 0.0, 1.0))
                self.assertEqual(self.gym.calls[-1][2]["steer"], round(value * 127))

    def test_early_finish_reports_actual_tick_duration_and_exact_finish_time(self):
        backend = self.backend()
        backend.reset()
        self.gym.step_answer = telemetry(2, finished=True, race_time_ms=23)
        sample, _ = backend.advance(Action(throttle=1.0), 20)
        self.assertTrue(sample.telemetry.finished)
        self.assertEqual(sample.telemetry.race_time_s, 0.023)
        self.assertEqual(sample.metadata["requested_ticks"], 20)
        self.assertEqual(sample.metadata["simulation_ticks"], 3)
        self.assertEqual(sample.metadata["simulation_dt_s"], 0.03)
        self.assertEqual(sample.telemetry.checkpoint_count, 2)
        before = list(self.gym.calls)
        with self.assertRaisesRegex(RuntimeError, "finished"):
            backend.advance(Action(), 1)
        self.assertEqual(self.gym.calls, before)

    def test_ticks_require_integer_protocol_range_and_do_not_damage_session(self):
        backend = self.backend()
        backend.reset()
        for invalid in (0, -1, 60001, 1.0, True, np.bool_(True), None):
            with self.subTest(invalid=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    backend.advance(Action(), invalid)
        self.assertEqual(self.gym.calls, [("reset", 0)])
        self.assertEqual(self.gym.close_count, 0)
        sample, _ = backend.advance(Action(), np.int64(60000))
        self.assertEqual(sample.metadata["race_tick"], 59999)

    def test_seed_range_is_checked_before_requests(self):
        backend = self.backend()
        for invalid in (-1, 0x100000000, 2.5, True):
            with self.subTest(invalid=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    backend.reset(invalid)
        self.assertEqual(self.gym.calls, [])
        backend.reset(3)

    def test_timeout_is_finite_positive_and_map_exists_before_acquisition(self):
        for timeout in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    self.backend(timeout=timeout)
        with self.assertRaises(FileNotFoundError):
            TickGymBackend(self.map_path.parent / "missing.Map.Gbx", client=self.client)
        self.assertEqual(self.client.calls, [])

    def test_partial_init_failure_closes_created_session(self):
        failure = ConnectionError("gym handshake failed")
        self.session.connect_error = failure
        with self.assertRaises(ConnectionError) as raised:
            self.backend()
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.session.close_count, 1)
        self.assertEqual(self.gym.close_count, 0)

    def test_close_attempts_both_resources_and_remains_idempotent_after_error(self):
        backend = self.backend()
        self.gym.close_error = OSError("gym close failed")
        self.session.close_error = OSError("session close failed")
        with self.assertRaisesRegex(OSError, "gym close failed") as raised:
            backend.close()
        self.assertIn("session close failed", " ".join(raised.exception.__notes__))
        backend.close()
        self.assertEqual(self.gym.close_count, 1)
        self.assertEqual(self.session.close_count, 1)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            backend.observe()

    def test_step_error_preserves_original_exception_and_never_retries(self):
        backend = self.backend()
        backend.reset()
        failure = ConnectionError("step outcome unknown")
        self.gym.step_error = failure
        self.gym.close_error = OSError("close failed")
        with self.assertRaises(ConnectionError) as raised:
            backend.advance(Action(), 5)
        self.assertIs(raised.exception, failure)
        self.assertIn("close failed", " ".join(failure.__notes__))
        self.assertEqual(self.gym.close_count, 1)
        self.assertEqual(self.session.close_count, 1)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            backend.advance(Action(), 5)
        self.assertEqual(len(self.gym.calls), 2)

    def test_reset_failure_closes_both_resources(self):
        backend = self.backend()
        self.gym.reset_error = ConnectionError("reset outcome unknown")
        with self.assertRaisesRegex(ConnectionError, "reset outcome unknown"):
            backend.reset()
        self.assertEqual(self.gym.close_count, 1)
        self.assertEqual(self.session.close_count, 1)

    def test_unexpected_reset_tick_is_rejected_without_synthetic_baseline(self):
        backend = self.backend()
        self.gym.reset_answer = telemetry(0)
        with self.assertRaisesRegex(RuntimeError, "outside"):
            backend.reset()
        self.assertEqual(self.gym.close_count, 1)

    def test_shortened_unfinished_step_and_clock_regression_fail_closed(self):
        for tick in (-1, 0, 5):
            with self.subTest(tick=tick):
                self.client = FakeClient()
                self.gym = self.client.session.gym
                backend = self.backend()
                backend.reset()
                backend.advance(Action(), 2)
                self.gym.step_answer = telemetry(tick)
                with self.assertRaisesRegex(RuntimeError, "race tick|shortened step"):
                    backend.advance(Action(), 2)
                self.assertEqual(self.gym.close_count, 1)

    def test_missing_required_telemetry_fails_closed(self):
        backend = self.backend()
        self.gym.reset_answer.vehicle = None
        with self.assertRaisesRegex(RuntimeError, "telemetry group"):
            backend.reset()
        self.assertEqual(self.session.close_count, 1)

    @unittest.skipUnless(importlib.util.find_spec("tickgym"), "optional tickgym is not installed")
    def test_actual_tickgym_110_telemetry_views_are_compatible(self):
        # Exercise the real 1.1.0 telemetry layout and properties without a server.
        import tickgym
        from tickgym.telemetry import Layout, Telemetry as TickTelemetry

        self.assertEqual(tickgym.__version__, "1.1.0")
        groups = (
            tickgym.TelemetryGroup.RACE | tickgym.TelemetryGroup.VEHICLE | tickgym.TelemetryGroup.DRIVETRAIN
        )
        layout = Layout.of(groups, 0)
        block = np.zeros(1, dtype=layout.dtype)
        block["race"]["race_tick"] = -1
        block["race"]["race_time_ms"] = -10
        block["vehicle"]["linear_velocity"] = (3, 4, 0)
        block["vehicle"]["position"] = (10, 20, 30)
        block["drivetrain"]["gear"] = 3
        self.gym.reset_answer = TickTelemetry(memoryview(block.tobytes()), layout, -1, False, None)
        sample = self.backend().reset(0)
        self.assertEqual(sample.telemetry.speed, 5.0)
        self.assertEqual(sample.telemetry.position, (10.0, 20.0, 30.0))
        self.assertEqual(sample.telemetry.gear, 3)
        self.assertEqual(sample.telemetry.race_time_s, -0.01)


if __name__ == "__main__":
    unittest.main()
