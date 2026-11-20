# Live API validation — 2026-11-20

This record distinguishes implemented interfaces from a working game installation. No trained policy or complete live racing episode was produced in this validation.

## Automated checks

- 85 live/API tests pass with the actual Elemental source checkout, Torch 2.9.1 CPU, Hydra and tickgym 1.1.0 available. They include Elemental PPO collection, rollout buffer/GAE, automatic terminal resets and Hydra configuration instantiation.
- The portable project check runs 246 Python tests: 234 pass and 12 Torch/Elemental checks skip in its minimal environment. Those 12 pass in the integration environment above. The native UI worker also passes its six offline self-checks.
- Regressions cover packet framing, stale/disconnected streams, post-input receipt barriers, latched finish events, failed-release retries, inference-time lease expiry, invalid-action cleanup, rejection of invalid Elemental transitions, tick quantization and acknowledged early-finish durations.
- Ruff passes. Source and wheel distributions build; a fresh base installation imports the live modules without Torch, input devices or a running game. Optional dependencies remain lazy.

These checks use protocol doubles for the game and TICK server. Actual tickgym telemetry layouts are exercised, but no running TICK server is implied.

## Ubuntu / RTX 3090 desktop attempt

The unchanged hash-pinned TMRL GrabData 0.3 plugin loaded in Openplanet Regular mode. A Python 3.12 process connected to its localhost TCP stream, received fresh position/control fields and captured RGB frames through MSS. A virtual Xbox controller was created through writable `/dev/uinput`. On this installation, vgamepad also required the Python libevdev bindings, which are included in the Linux live extra.

Starter Access blocked local map selection, so the test used the free campaign's Fall 2026–01. The game remained on its pre-race medal-selection screen: keyboard and virtual-controller attempts did not establish playable driving, including a restart with the controller already connected. The cause is unresolved. A 150-step attempted rollout reported zero maximum speed and no movement; it is a **failed driving test**, not training data. Its roughly 79 ms median host step duration measures menu capture/control overhead only, not driving throughput.

Start-pose checks and action-lease release/reset paths ran, but the same pose existed behind the menu. That does not verify race readiness. Actual accelerator/brake/steering effects, third-person driving capture, countdown timing and finish-to-reset behavior remain unverified on this installation. The live guide explains this protocol limitation and requires a movement/input pilot before training.

The public `examples/verify_live_controls.py` checker reproduced the failure after the restart. Its ten-second accelerator/left/right/brake sequence received 100 fresh observations, but all four reported-control checks failed and displacement remained zero. Both start-pose checks passed, confirming why those checks alone are insufficient. The checker returned a nonzero exit status and saved a failed verification record; no samples were accepted as training data.

## Controller-paced mode

The optional adapter targets the published tickgym 1.1.0 client and integer 10 ms steps. A compatible TICK 3.0 server was not available on the desktop, and the publisher's linked server repository was unavailable during research. Live server behavior and Linux/Proton compatibility remain unverified. This API provides telemetry, not synchronized RGB rendering.

## Remaining installation work

1. Resolve the game's pre-race input state and verify controllable movement with the same input device used for collection.
2. Repeat the control verification example until it passes, and inspect camera/crop, countdown and input signs.
3. Complete a real finish and verify automatic terminal reset before long collection.
4. Separately provision and validate TICK if exact simulation stepping is required; RGB lockstep needs additional rendering support beyond the current client.

The Python library, Elemental integration and offline replay exporter can be used independently of that remaining desktop setup. See [the API guide](live-rl.md) for explicit timing and learning semantics.
