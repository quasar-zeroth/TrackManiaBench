#!/usr/bin/env python3
"""Collect a short live rollout using Elemental's actual PPO and buffer.

This is an environment integration check using constant controls, not model
training. It resets the game and applies controls. Backend options are a JSON
object containing the installation's measured capture/start configuration, or
the map_path/origin of a separately available TICK server.

    PYTHONPATH=/path/to/elemental/src python examples/elemental_trackmania_smoke.py \
      --backend realtime --backend-options local-backend.json --steps 8

The default action is neutral. For example, --action 0 0.5 0 requests half
throttle; the backend reports any controller quantization in observations.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("realtime", "tickgym"), required=True)
    parser.add_argument("--backend-options", type=Path, required=True)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--control-hz", type=float, default=20.0)
    parser.add_argument(
        "--action", type=float, nargs=3, default=(0.0, 0.0, 0.0), metavar=("STEER", "THROTTLE", "BRAKE")
    )
    args = parser.parse_args()
    if args.steps < 1:
        parser.error("--steps must be positive")

    import torch

    try:
        from rl.policy_wrapper import BasePolicyWrapper
        from rl.ppo import PPOAlgorithm
        from rl.rollout_buffer import OnPolicyRolloutBuffer
    except ImportError as error:
        raise SystemExit("Add the Elemental checkout's src/ directory to PYTHONPATH") from error

    from trackmaniabench.live.elemental import ElementalTrackManiaEnv
    from trackmaniabench.live.types import Action

    action = Action.from_array(args.action)
    options = json.loads(args.backend_options.read_text())
    if not isinstance(options, dict):
        parser.error("--backend-options must contain a JSON object")

    class ConstantPolicy(BasePolicyWrapper):
        @torch.no_grad()
        def act(self, obs, *, deterministic=False):
            return {
                "action": torch.tensor(action.array()).reshape(1, 1, 3),
                "log_prob": torch.zeros(1),
                "value": torch.zeros(1),
            }

        def evaluate_actions(self, obs, action, action_info):
            raise RuntimeError("This smoke policy is only for rollout collection")

    class Callbacks:
        def on_batch_start(self, *args, **kwargs):
            pass

        def on_batch_end(self, *args, **kwargs):
            pass

    with ElementalTrackManiaEnv(
        backend_kind=args.backend,
        backend_kwargs=options,
        env_kwargs={"control_hz": args.control_hz},
    ) as env:
        buffer = OnPolicyRolloutBuffer(
            args.steps,
            1,
            (1, 3),
            obs_keys=tuple(env.observation_spec),
        )
        algorithm = PPOAlgorithm(steps_per_rollout=args.steps)
        metrics = algorithm.collect_rollouts(
            env=env,
            policy_wrapper=ConstantPolicy(torch.nn.Identity()),
            buffer=buffer,
            callbacks=Callbacks(),
            epoch=0,
            step_offset=0,
            device=torch.device("cpu"),
        )
        minibatch = next(buffer.iter_minibatches(args.steps, shuffle=False))
        print(
            json.dumps(
                {
                    "steps": len(buffer),
                    "clock_mode": env.capabilities.clock_mode,
                    "rgb": env.capabilities.rgb,
                    "observation_keys": list(env.observation_spec),
                    "action_shape": list(minibatch["rl/action"].shape),
                    "reward_sum": buffer.rewards.sum().item(),
                    "metrics": metrics,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
