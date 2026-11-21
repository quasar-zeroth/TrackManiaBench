# Live API validation — 2026-11-21

TrackManiaBench 0.2.1 was exercised on Ubuntu with an AMD 64-bit CPU and RTX 3090, using Steam/Proton Trackmania 2020. Real-time game control and native controller-paced simulation both ran. These are environment and integration checks, not a trained policy or an RL training result.

## Real-time game

The unchanged, hash-pinned TMRL GrabData 0.3 plugin ran in Openplanet Regular mode. A Python 3.12 process received localhost telemetry, sent virtual Xbox controls and captured third-person chase-camera RGB through MSS. The test used the playable free campaign map Fall 2026–01, UID `ypPmohQofd72MyTsIsijKfrJH98`; Starter Access blocked the local-map and Training campaign alternatives.

The previous day's pre-race failure was resolved by opening and closing the Ubisoft Connect overlay with Shift+F2. The same connected gamepad then entered the race. A second issue appeared when creating a new controller: Steam could ignore an immediate reset before registering the device. The library now waits two seconds for newly created pads. It also uses configurable Give Up bindings, waits for reset settling, and handles the finish popup only after an observed finish. Keyboard Delete and gamepad B restart the race on this installation; BackSpace is checkpoint respawn. Visible countdowns and restarted clocks were inspected.

`examples/verify_live_controls.py` passed all seven checks over 100 observations: accelerator, left/right steering, brake, movement, and initial/final reset. Maximum displacement was 198.63 game-world units. Median observation latency was 34.84 ms, including 31.36 ms capture; median telemetry-receipt-to-capture-end skew was 31.39 ms. The controls are corroborated by plugin reports and visible movement; they do not establish exact physics/input latency.

`examples/verify_realtime_timing.py` passed all 20 checks. During a 0.500-second inference pause with no new device write, throttle remained held and the car moved 16.28 game-world units. The next transition reported 0.616 seconds of host elapsed time and 0.535 seconds of observation-to-write delay. Waiting 1.2 seconds under a one-second lease released the controls, rejected the requested action, produced an invalid truncation and recovered after reset.

A separate 150-step driving pilot had an 83.68 ms median host step with a configured 50 ms minimum action hold: about 12 actual decisions per second after capture overhead. Configuration frequency is not a promise of achieved frequency. Its median capture/telemetry skew was 36.33 ms. This short pilot did not finish the race.

`examples/verify_realtime_episodes.py --adapter elemental` then completed **two consecutive races and three verified resets** (initial reset plus one after each finish), without intervention. Host episode durations were **55.392 and 55.319 seconds**, with 663 and 662 decisions. Median step durations were 83.64 and 83.48 ms. Terminal frames showed the car crossing the finish; reset frames showed the starting line and restarted clock. The second completed race confirms that automatic reset returned to a playable state.

This diagnostic used a recorded reference path, gain 0.8, 10 m lookahead, a 30 m/s speed cap and 0.1 steering-deadzone compensation. Earlier controller settings reached the last section but hit a barrier or lacked speed for the final ramp; they correctly failed the verification. Matching failures and successful settings were reproduced in headless simulation with irregular decisions and 20–40 ms input delay. The example is a tunable installation check, not a general driving policy.

## Controller-paced simulation

The publisher's native Linux TICK 3.0.1 optimizer ran independently of the desktop game, using its installed game resources. Build: `3.0.1+57d9a57933d36e0b17727e74e337aa403caa9eab`; client: tickgym 1.1.0. An isolated Ubuntu 24.04 libc 2.39 directory supplied the newer runtime without replacing system libraries. See [the reproducible setup procedure](tickgym-setup.md).

`examples/verify_lockstep.py` passed all 13 checks on Summer 2020–01, UID `XJ_JEjWGoAexDWe8qfaOjEcq5l8`:

- Server-acknowledged snapshots remained at tick -1 across a one-second pause, and at tick 295 across another pause after driving.
- Requested advances of 1, 5, 20 and 200 ticks were exact. Reported steering/pedals matched, the car moved, and braking reduced speed.
- Identical seeds and inputs produced identical recorded states despite different inference delays. Reset restored the initial state.
- The public environment at 20 Hz advanced exactly five 10 ms ticks per decision.
- Known inputs from the existing dataset completed all three checkpoints, including the finish, in **19.723 seconds**. Reset after finishing returned to tick -1; sessions closed cleanly.

No original dataset files were changed. This mode supplies telemetry only. The desktop game's screen is not a synchronized renderer for the headless simulation.

## Actual Elemental integration

The public smoke collector ran Elemental's actual PPO collection, rollout buffer and GAE against both live backends. Each collected 40 decisions with action shape `[40, 1, 3]`. Real-time observations included RGB, telemetry, timing and action history; lockstep observations had the same non-image fields and no RGB. The real-time run used Python 3.12 and CPU Torch 2.15.0; the lockstep client run used CPU Torch 2.9.1 with the same Elemental source checkout. These short collector runs did not complete episodes.

Elemental's existing per-decision discounting, first-action-only chunk handling and reset at rollout boundaries remain unchanged. Timing fields do not automatically make its PPO/GAE duration-aware. See [the integration contract](live-rl.md#elemental-integration).

## Automated and packaging checks

- 102 live/API tests pass with actual Elemental source, Torch, Hydra and tickgym available. They cover real PPO/buffer/GAE use, terminal resets, device discovery delays, reset bindings, packet framing, disconnects, stale observations, latched finishes, release retries, invalid transitions and tick quantization.
- The portable project check runs 263 Python tests: 251 pass and 12 optional Torch/Elemental tests skip in the minimal environment. Those 12 pass in the integration environment. The native UI worker passes its six offline self-checks.
- Ruff and whitespace checks pass. Source and wheel distributions build. Optional live dependencies remain lazy.

The protocol regressions complement the real installation checks above; they do not replace them. Repeat the live checkers after changing maps, game/Proton versions, control bindings, capture configuration or controller hardware. GrabData does not expose map identity, menu state or the race clock, so the library continues to report those verification limits explicitly.

Full JSON reports, terminal/reset images and private reference files remain on the test machine. The repeatable public commands are:

```bash
python examples/verify_live_controls.py --backend-options backend.local.json
python examples/verify_realtime_timing.py --backend-options backend.local.json
python examples/verify_realtime_episodes.py \
  --backend-options backend.local.json --reference reference.local.json \
  --adapter elemental --episodes 2 --steer-deadzone 0.1 --captures evidence/episodes
python examples/verify_lockstep.py /data/circuit.Map.Gbx \
  --finish-inputs /data/known-finish.txt
```
