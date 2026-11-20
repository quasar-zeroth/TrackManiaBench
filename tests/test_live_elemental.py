"""Offline adapter checks, plus optional tests against real Elemental modules."""

import importlib.util
from pathlib import Path
import unittest

import numpy as np

from trackmaniabench.live.elemental import ElementalTrackManiaEnv
from trackmaniabench.live.types import Action, Capabilities, Sample, Telemetry

try:
    import torch
except ImportError:
    torch = None


class FakeBackend:
    """Deterministic bridge double; the production environment still runs."""

    def __init__(self, rgb=True, finish_after=2):
        self.capabilities = Capabilities("lockstep", rgb, "analog", tick_seconds=0.01)
        self.finish_after = finish_after
        self.resets = 0
        self.closes = 0
        self.steps = 0
        self.sequence = 0
        self.ticks = 0
        self.actions = []
        self.image = np.zeros((6, 8, 3), dtype=np.uint8)

    def sample(self):
        self.sequence += 1
        self.image.fill(self.steps * 40)
        return Sample(
            telemetry=Telemetry(
                speed=float(self.steps),
                position=(float(self.steps), 0.0, 0.0),
                race_time_s=self.ticks * 0.01,
                checkpoint_count=self.steps,
                finished=self.steps >= self.finish_after,
                speed_unit="m/s",
            ),
            captured_ns=__import__("time").monotonic_ns(),
            sequence=self.sequence,
            image=self.image if self.capabilities.rgb else None,
        )

    def reset(self, seed=None):
        self.resets += 1
        self.steps = 0
        self.ticks = 0
        return self.sample()

    def observe(self):
        return self.sample()

    def apply_action(self, action):
        self.actions.append(action)
        return action

    def advance(self, action, ticks):
        self.actions.append(action)
        self.steps += 1
        self.ticks += ticks
        return self.sample(), action

    def close(self):
        self.closes += 1


@unittest.skipIf(torch is None, "optional Torch dependency is not installed")
class ElementalAdapterTests(unittest.TestCase):
    def make_env(self, rgb=True, finish_after=2, **kwargs):
        backend = FakeBackend(rgb=rgb, finish_after=finish_after)
        env = ElementalTrackManiaEnv(
            backend=backend,
            image_size=(4, 5),
            env_kwargs=kwargs,
        )
        self.addCleanup(env.close)
        return env, backend

    def test_fresh_tensors_and_render_shape(self):
        env, backend = self.make_env()
        first = env.reset()
        self.assertEqual(first["image"].shape, (1, 3, 4, 5))
        self.assertEqual(first["telemetry"].shape, (1, 12))
        self.assertEqual(first["timing"].shape, (1, 4))
        self.assertEqual(first["previous_action"].shape, (1, 3))
        self.assertEqual(first["applied_action"].shape, (1, 3))
        self.assertEqual(first["image"].dtype, torch.float32)
        second, reward, done, info = env.step(torch.tensor([[0.25, 1.0, 0.0]]))
        self.assertFalse(done.item())
        self.assertEqual(reward.shape, (1,))
        self.assertEqual(reward.dtype, torch.float32)
        self.assertEqual(info["success"].dtype, torch.bool)
        self.assertEqual(first["image"].max().item(), 0.0)
        self.assertEqual(first["telemetry"][0, 0].item(), 0.0)
        self.assertAlmostEqual(second["image"].max().item(), 40.0 / 255.0)
        self.assertEqual(second["telemetry"][0, 0].item(), 1.0)
        self.assertEqual(backend.actions[-1], Action(0.25, 1.0, 0.0))
        frame = env.render()
        frame.zero_()
        self.assertGreater(env.render().max().item(), 0.0)

    def test_terminal_autoreset_retains_final_observation(self):
        env, backend = self.make_env(finish_after=1)
        env.reset()
        next_obs, reward, done, info = env.step(torch.tensor([[0.0, 1.0, 0.0]]))
        self.assertTrue(done.item())
        self.assertTrue(info["terminated"].item())
        self.assertFalse(info["truncated"].item())
        self.assertTrue(info["success"].item())
        self.assertEqual(backend.resets, 2)
        self.assertEqual(next_obs["telemetry"][0, 0].item(), 0.0)
        self.assertEqual(info["final_observation"]["telemetry"][0, 0].item(), 1.0)
        self.assertGreater(info["final_observation"]["image"].mean().item(), 0.0)
        self.assertEqual(env.render().max().item(), 0.0)
        self.assertIn("reset_info", info)

    def test_truncation_is_not_success(self):
        env, backend = self.make_env(finish_after=100, max_steps=1)
        env.reset()
        _, _, done, info = env.step(torch.tensor([[0.0, 0.0, 0.0]]))
        self.assertTrue(done.item())
        self.assertFalse(info["terminated"].item())
        self.assertTrue(info["truncated"].item())
        self.assertFalse(info["success"].item())
        self.assertEqual(backend.resets, 2)

    def test_telemetry_only_does_not_fabricate_images(self):
        env, _ = self.make_env(rgb=False)
        observation = env.reset()
        self.assertNotIn("image", observation)
        self.assertNotIn("image", env.observation_spec)
        self.assertIsNone(env.render())
        env.step(torch.tensor([[0.0, 0.5, 0.0]]))
        self.assertIsNone(env.render())

    def test_invalid_actions_are_rejected_without_advancing(self):
        for action in (
            torch.zeros(3),
            torch.zeros(1, 1, 3),
            torch.tensor([[1.01, 0.0, 0.0]]),
            torch.tensor([[0.0, -0.1, 0.0]]),
            torch.tensor([[0.0, 0.0, 1.1]]),
            torch.tensor([[float("nan"), 0.0, 0.0]]),
            torch.tensor([[0.0, float("inf"), 0.0]]),
            [[0.0, 0.0, 0.0]],
            torch.zeros((1, 3), dtype=torch.complex64),
        ):
            with self.subTest(action=action):
                env, backend = self.make_env()
                env.reset()
                env.step(torch.tensor([[0.0, 1.0, 0.0]]))
                initial_actions = len(backend.actions)
                with self.assertRaises((ValueError, TypeError)):
                    env.step(action)
                self.assertEqual(len(backend.actions), initial_actions)
                self.assertEqual(backend.closes, 1)

    def test_invalid_transition_cannot_be_returned_to_collector(self):
        from dataclasses import replace

        env, backend = self.make_env()
        env.reset()
        original = backend.sample
        backend.sample = lambda: replace(original(), metadata={"transition_valid": False})
        with self.assertRaisesRegex(RuntimeError, "refusing insertion"):
            env.step(torch.tensor([[0.0, 1.0, 0.0]]))
        self.assertEqual(backend.closes, 1)
        self.assertEqual(backend.resets, 1)

    def test_device_assignment_and_close(self):
        env, backend = self.make_env()
        env.device = torch.device("cpu")
        self.assertEqual(env.reset()["telemetry"].device, env.device)
        env.close()
        env.close()
        self.assertEqual(backend.closes, 1)
        with self.assertRaises(RuntimeError):
            env.reset()

    def test_constructor_rejects_incompatible_configuration(self):
        with self.assertRaises(ValueError):
            ElementalTrackManiaEnv(num_envs=2)
        with self.assertRaises(ValueError):
            ElementalTrackManiaEnv(image_size=(0, 12))
        env, backend = self.make_env()
        with self.assertRaises(ValueError):
            ElementalTrackManiaEnv(env=env.env, backend=backend)

    def test_invalid_rgb_sample_closes_backend(self):
        backend = FakeBackend(rgb=False)
        backend.capabilities = Capabilities("lockstep", True, "analog", tick_seconds=0.01)
        original_sample = backend.sample

        def missing_rgb():
            sample = original_sample()
            return Sample(sample.telemetry, sample.captured_ns, sample.sequence)

        backend.sample = missing_rgb
        env = ElementalTrackManiaEnv(backend=backend)
        with self.assertRaisesRegex(ValueError, "RGB backend returned no image"):
            env.reset()
        self.assertEqual(backend.closes, 1)


def elemental_available():
    if torch is None:
        return False
    try:
        return (
            importlib.util.find_spec("rl.ppo") is not None
            and importlib.util.find_spec("env.base_vec_env") is not None
        )
    except (ImportError, ModuleNotFoundError):
        return False


@unittest.skipUnless(elemental_available(), "real Elemental src/ is not on PYTHONPATH (or Torch is absent)")
class ActualElementalIntegrationTests(unittest.TestCase):
    def test_real_ppo_buffer_and_gae_with_terminal_resets(self):
        from rl.policy_wrapper import BasePolicyWrapper
        from rl.ppo import PPOAlgorithm
        from rl.rollout_buffer import OnPolicyRolloutBuffer

        class Policy(BasePolicyWrapper):
            @torch.no_grad()
            def act(self, obs, *, deterministic=False):
                return {
                    "action": torch.tensor([[[0.0, 0.5, 0.0]]]),
                    "log_prob": torch.zeros(1),
                    "value": obs["telemetry"][:, 0],
                }

            def evaluate_actions(self, obs, action, action_info):
                return {name: torch.zeros(action.shape[0]) for name in ("log_prob", "value", "entropy")}

        class Callbacks:
            def on_batch_start(self, *args, **kwargs):
                pass

            def on_batch_end(self, *args, **kwargs):
                pass

        for rgb in (True, False):
            with self.subTest(rgb=rgb):
                backend = FakeBackend(rgb=rgb)
                with ElementalTrackManiaEnv(backend=backend, image_size=(4, 5)) as env:
                    keys = tuple(env.observation_spec)
                    buffer = OnPolicyRolloutBuffer(4, 1, (1, 3), obs_keys=keys)
                    algorithm = PPOAlgorithm(steps_per_rollout=4, minibatch_size=2)
                    metrics = algorithm.collect_rollouts(
                        env=env,
                        policy_wrapper=Policy(torch.nn.Identity()),
                        buffer=buffer,
                        callbacks=Callbacks(),
                        epoch=0,
                        step_offset=0,
                        device=torch.device("cpu"),
                    )
                    self.assertEqual(metrics["rollout/num_episodes"], 2.0)
                    self.assertEqual(metrics["rollout/success_rate"], 1.0)
                    self.assertEqual(backend.resets, 3)
                    batches = list(buffer.iter_minibatches(2, shuffle=False))
                    self.assertEqual(len(batches), 2)
                    self.assertEqual(batches[0]["rl/action"].shape, (2, 1, 3))
                    self.assertEqual(batches[0]["input/telemetry"].shape, (2, 12))
                    self.assertEqual(batches[0]["input/timing"].shape, (2, 4))
                    self.assertEqual(batches[0]["input/telemetry"][:, 0].tolist(), [0.0, 1.0])
                    self.assertTrue(torch.isfinite(buffer.advantages).all())
                    if rgb:
                        self.assertEqual(batches[0]["input/image"].shape, (2, 3, 4, 5))
                    else:
                        self.assertNotIn("input/image", batches[0])

    @unittest.skipUnless(importlib.util.find_spec("hydra") is not None, "optional Hydra dependency is absent")
    def test_hydra_instantiates_external_adapter(self):
        from hydra.utils import instantiate

        backend = FakeBackend()
        env = instantiate(
            {"_target_": "trackmaniabench.live.elemental.ElementalTrackManiaEnv", "image_size": [4, 5]},
            backend=backend,
            _convert_="partial",
        )
        self.addCleanup(env.close)
        self.assertEqual(env.reset()["image"].shape, (1, 3, 4, 5))

    @unittest.skipUnless(importlib.util.find_spec("hydra") is not None, "optional Hydra dependency is absent")
    def test_shipped_component_configs_compose_with_real_elemental(self):
        from hydra.utils import instantiate
        from omegaconf import OmegaConf

        for name, rgb in (("realtime", True), ("tickgym", False)):
            with self.subTest(backend=name):
                path = Path(__file__).resolve().parents[1] / "configs" / f"elemental-trackmania-{name}.yaml"
                config = OmegaConf.load(path)
                # Substitute the protocol double for this installation's
                # manually provisioned game/server, retaining the shipped
                # adapter, PPO, buffer, and interpolation configuration.
                config.env.backend_kwargs = {}
                env = instantiate(config.env, backend=FakeBackend(rgb=rgb), _convert_="partial")
                self.addCleanup(env.close)
                buffer = instantiate(config.rollout_buffer, device="cpu", _convert_="partial")
                algorithm = instantiate(config.rl, _convert_="partial")
                self.assertEqual(set(buffer.obs_keys), set(env.observation_spec))
                self.assertEqual(buffer.num_envs, env.num_envs)
                self.assertEqual(buffer.num_steps, algorithm.steps_per_rollout)
                self.assertEqual(tuple(buffer.action_shape), (1, env.action_dim))


if __name__ == "__main__":
    unittest.main()
