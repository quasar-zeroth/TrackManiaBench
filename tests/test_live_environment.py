"""Clock/action attribution regressions; no game process is involved."""

import unittest
from unittest.mock import patch
import numpy as np

from trackmaniabench.live import Action, Capabilities, LiveTrackManiaEnv, Sample, Telemetry


class Clock:
    def __init__(self):
        self.ns = 100_000_000_000

    def monotonic_ns(self):
        return self.ns

    def monotonic(self):
        return self.ns / 1e9

    def sleep(self, seconds):
        self.ns += round(seconds * 1e9)


class FakeBackend:
    def __init__(self, clock, mode="realtime"):
        self.clock = clock
        self.capabilities = Capabilities(
            mode, mode == "realtime", "test", 0.01 if mode == "lockstep" else None
        )
        self.sequence = 0
        self.game_time = 0
        self.closed = False
        self.actions = []
        self.finished = False
        self.duration = None
        self.stale = False
        self.lease = False
        self.metadata = {}
        self.close_failure = None
        self.close_calls = 0

    def sample(self, dt=None):
        if not self.stale:
            self.sequence += 1
        return Sample(
            Telemetry(
                3,
                (0, 1, 2),
                finished=self.finished,
                race_time_s=self.game_time if self.capabilities.clock_mode == "lockstep" else None,
            ),
            self.clock.ns,
            self.sequence,
            np.zeros((8, 10, 3), np.uint8) if self.capabilities.rgb else None,
            {"simulation_dt_s": dt, "action_lease_expired": self.lease, **self.metadata},
        )

    def reset(self, seed=None):
        self.game_time = 0
        self.finished = False
        return self.sample()

    def apply_action(self, action):
        self.actions.append(action)
        return action

    def observe(self):
        return self.sample()

    def advance(self, action, ticks):
        dt = ticks * 0.01 if self.duration is None else self.duration
        self.game_time += dt
        self.actions.append(action)
        return self.sample(dt), action

    def close(self):
        self.closed = True
        self.close_calls += 1
        if self.close_failure is not None:
            raise self.close_failure


class LiveEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.patch = patch("trackmaniabench.live.environment.time", self.clock)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_realtime_inference_delay_keeps_previous_action_explicit(self):
        backend = FakeBackend(self.clock)
        env = LiveTrackManiaEnv(backend)
        env.reset()
        action = Action(0.3, 1, 0)
        env.step(action)
        self.clock.sleep(0.2)  # Time used by the next model inference.
        obs, reward, term, trunc, info = env.step(Action(-0.4, 1, 0))
        self.assertAlmostEqual(info["wall_dt_s"], 0.25)
        self.assertAlmostEqual(info["action_delay_s"], 0.2)
        self.assertIsNone(info["game_dt_s"])
        np.testing.assert_allclose(obs["previous_action"], action.array())
        self.assertEqual(obs["timing"][3], 0)
        self.assertFalse(term or trunc)
        self.assertAlmostEqual(reward, -0.0025)
        env.close()
        self.assertTrue(backend.closed)

    def test_lockstep_ignores_model_wall_latency_and_advances_exact_ticks(self):
        backend = FakeBackend(self.clock, "lockstep")
        env = LiveTrackManiaEnv(backend, control_hz=20)
        obs, _ = env.reset()
        self.assertNotIn("image", obs)
        self.clock.sleep(5)
        obs, reward, _, _, info = env.step(Action(0, 1, 0))
        self.assertAlmostEqual(info["game_dt_s"], 0.05)
        self.assertAlmostEqual(info["wall_dt_s"], 5)
        self.assertEqual(info["action_delay_s"], 0)
        self.assertEqual(self.clock.ns, 105_000_000_000)  # No sleeps in lockstep.
        self.assertAlmostEqual(reward, -0.0005)
        self.assertEqual(obs["timing"][3], 1)

    def test_nonintegral_simulation_frequency_rejected(self):
        with self.assertRaisesRegex(ValueError, "whole number"):
            LiveTrackManiaEnv(FakeBackend(self.clock, "lockstep"), control_hz=60)

    def test_short_step_only_valid_at_finish(self):
        backend = FakeBackend(self.clock, "lockstep")
        env = LiveTrackManiaEnv(backend)
        env.reset()
        backend.duration = 0.02
        with self.assertRaisesRegex(RuntimeError, "fewer ticks"):
            env.step(Action())
        self.assertTrue(backend.closed)
        backend = FakeBackend(self.clock, "lockstep")
        env = LiveTrackManiaEnv(backend)
        env.reset()
        backend.duration, backend.finished = 0.02, True
        _, reward, terminal, truncated, info = env.step(Action())
        self.assertTrue(terminal)
        self.assertFalse(truncated)
        self.assertAlmostEqual(reward, 0.9998)
        self.assertEqual(info["terminal_reason"], "finish")
        with self.assertRaisesRegex(RuntimeError, "reset"):
            env.step(Action())

    def test_terminal_and_timeout_neutralize_realtime_controls(self):
        for finished in (True, False):
            backend = FakeBackend(self.clock)
            env = LiveTrackManiaEnv(backend, max_steps=1)
            env.reset()
            backend.finished = finished
            _, _, terminal, truncated, _ = env.step(Action(1, 1, 0))
            self.assertEqual((terminal, truncated), (finished, not finished))
            self.assertEqual(backend.actions[-1], Action())

    def test_stale_observation_closes_backend(self):
        backend = FakeBackend(self.clock)
        env = LiveTrackManiaEnv(backend)
        env.reset()
        backend.stale = True
        with self.assertRaisesRegex(RuntimeError, "stale"):
            env.step(Action())
        self.assertTrue(backend.closed)

    def test_observations_do_not_alias_mutable_buffers(self):
        backend = FakeBackend(self.clock)
        env = LiveTrackManiaEnv(backend)
        original, _ = env.reset()
        following, *_ = env.step(Action(1, 1, 0))
        following["image"][:] = 255
        following["telemetry"][:] = 99
        self.assertEqual(original["image"].max(), 0)
        self.assertEqual(original["telemetry"][0], 3)

    def test_watchdog_expiry_truncates(self):
        backend = FakeBackend(self.clock)
        env = LiveTrackManiaEnv(backend)
        env.reset()
        backend.lease = True
        _, _, term, trunc, info = env.step(Action())
        self.assertFalse(term)
        self.assertTrue(trunc)
        self.assertEqual(info["terminal_reason"], "action_lease_expired")

    def test_invalid_action_closes_backend_with_previous_action_held(self):
        for invalid in ((0, 2, 0), (float("nan"), 0, 0), (0, 0), [[0, 0, 0]]):
            with self.subTest(action=invalid):
                backend = FakeBackend(self.clock)
                env = LiveTrackManiaEnv(backend)
                env.reset()
                env.step(Action(throttle=1))
                before = len(backend.actions)
                with self.assertRaises(ValueError):
                    env.step(invalid)
                self.assertTrue(backend.closed)
                self.assertEqual(len(backend.actions), before)
                self.assertEqual(backend.close_calls, 1)
                with self.assertRaisesRegex(RuntimeError, "closed"):
                    env.step(Action())

    def test_faulted_transition_truncates_without_second_control_write(self):
        class FaultDuringObservation(FakeBackend):
            def observe(self):
                self.metadata = {
                    "transition_valid": False,
                    "control_fault": "action_lease_expired",
                    "action_lease_expired": True,
                }
                return self.sample()

            def apply_action(self, action):
                if self.metadata.get("control_fault"):
                    raise RuntimeError("A latched control fault rejects every new write")
                return super().apply_action(action)

        backend = FaultDuringObservation(self.clock)
        env = LiveTrackManiaEnv(backend)
        env.reset()
        backend.finished = True  # A simultaneous finish is not a valid success.
        _, reward, terminal, truncated, info = env.step(Action(throttle=1))
        self.assertEqual((terminal, truncated), (False, True))
        self.assertFalse(info["success"])
        self.assertFalse(info["transition_valid"])
        self.assertLessEqual(reward, 0)
        self.assertEqual(len(backend.actions), 1)
        self.assertFalse(backend.closed)
        self.assertEqual(info["terminal_reason"], "action_lease_expired")
        env.close()

    def test_independent_fault_indicators_override_finish_and_valid_flag(self):
        for metadata in (
            {"control_fault": "telemetry_stale"},
            {"action_lease_expired": True},
            {"transition_valid": np.bool_(False)},
            {"control_fault": "telemetry_stale", "transition_valid": True},
        ):
            with self.subTest(metadata=metadata):
                backend = FakeBackend(self.clock)
                env = LiveTrackManiaEnv(backend)
                env.reset()
                backend.metadata = metadata
                backend.finished = True
                _, reward, terminal, truncated, info = env.step(Action())
                self.assertEqual((terminal, truncated), (False, True))
                self.assertFalse(info["success"])
                self.assertFalse(info["transition_valid"])
                self.assertLessEqual(reward, 0)
                env.close()

    def test_unresolved_release_failure_raises_and_closes_instead_of_truncating(self):
        backend = FakeBackend(self.clock)
        env = LiveTrackManiaEnv(backend)
        env.reset()
        backend.metadata = {
            "transition_valid": False,
            "control_fault": "action_lease_expired",
            "control_release_error": "input device rejected key-up",
        }
        with self.assertRaisesRegex(RuntimeError, "could not release controls"):
            env.step(Action(throttle=1))
        self.assertTrue(backend.closed)
        self.assertEqual(backend.close_calls, 1)

    def test_reset_rejects_faulted_or_unreleased_control_state(self):
        for metadata in (
            {"transition_valid": False},
            {"control_fault": "telemetry_stale"},
            {"action_lease_expired": True},
            {"control_release_error": "device gone"},
        ):
            with self.subTest(metadata=metadata):
                backend = FakeBackend(self.clock)
                backend.metadata = metadata
                env = LiveTrackManiaEnv(backend)
                with self.assertRaisesRegex(RuntimeError, "control"):
                    env.reset()
                self.assertTrue(backend.closed)

    def test_invalid_metadata_boolean_closes_backend(self):
        for flag in ("transition_valid", "action_lease_expired"):
            backend = FakeBackend(self.clock)
            env = LiveTrackManiaEnv(backend)
            env.reset()
            backend.metadata = {flag: "false"}
            with self.assertRaisesRegex(RuntimeError, "must be a boolean"):
                env.step(Action())
            self.assertTrue(backend.closed)

    def test_cleanup_error_does_not_replace_invalid_action_error(self):
        backend = FakeBackend(self.clock)
        env = LiveTrackManiaEnv(backend)
        env.reset()
        env.step(Action(throttle=1))
        backend.close_failure = OSError("failed to release device")
        with self.assertRaises(ValueError) as raised:
            env.step([0, float("nan"), 0])
        self.assertIn("failed to release device", " ".join(raised.exception.__notes__))
        self.assertTrue(backend.closed)
        self.assertEqual(backend.close_calls, 1)

    def test_step_error_survives_failed_cleanup(self):
        backend = FakeBackend(self.clock)
        env = LiveTrackManiaEnv(backend)
        env.reset()
        backend.stale = True
        backend.close_failure = OSError("cleanup error")
        with self.assertRaisesRegex(RuntimeError, "stale") as raised:
            env.step(Action())
        self.assertIn("cleanup error", " ".join(raised.exception.__notes__))

    def test_context_body_error_survives_failed_cleanup(self):
        backend = FakeBackend(self.clock)
        backend.close_failure = OSError("close failed")
        original = ValueError("policy inference failed")
        with self.assertRaises(ValueError) as raised:
            with LiveTrackManiaEnv(backend) as env:
                env.reset()
                env.step(Action(throttle=1))
                raise original
        self.assertIs(raised.exception, original)
        self.assertIn("close failed", " ".join(original.__notes__))
        self.assertTrue(backend.closed)

    def test_context_cleanup_error_is_visible_without_original_error(self):
        backend = FakeBackend(self.clock)
        backend.close_failure = OSError("close failed")
        with self.assertRaisesRegex(OSError, "close failed"):
            with LiveTrackManiaEnv(backend):
                pass

    def test_action_and_frequency_validation(self):
        for values in [(0, 2, 0), (float("nan"), 0, 0), (1, 0), [[0, 0, 0]]]:
            with self.assertRaises(ValueError):
                Action.from_array(values)
        for hz in (0, -1, float("inf")):
            with self.assertRaises(ValueError):
                LiveTrackManiaEnv(FakeBackend(self.clock), control_hz=hz)


if __name__ == "__main__":
    unittest.main()
