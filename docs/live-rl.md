# Live RL API

TrackManiaBench exposes a framework-independent Python environment and an optional adapter for Elemental's Torch `BaseVecEnv` contract. Each environment owns one game instance or one simulation session. Policy inference remains outside the library.

## Two clock modes

**Realtime** uses the running Trackmania 2020 game, its normal clock, the signed TMRL GrabData plugin, screen capture and a virtual gamepad or keyboard. The previous controls remain held during inference. `step(action)` writes the new controls, holds them for at least `1 / control_hz` seconds, then captures an observation. Inference and capture overhead therefore reduce the actual decision rate. Changing `control_hz` does not slow or freeze the game. Lower-level `backend.apply_action(action)` and `backend.observe()` are separate operations for an asynchronous controller.

**Lockstep** uses the optional `tickgym==1.1.0` client and a separately provisioned TICK 3.0 server. A decision advances an acknowledged integer number of 10 ms simulation ticks, regardless of inference latency. For example, 20 Hz means five ticks per decision. Frequencies such as 60 Hz do not divide the simulation tick and are rejected rather than silently rounded. Early finishes report their actual tick duration.

The native Linux TICK 3.0.1 optimizer has been validated with a Steam/Proton installation: exact ticks, pauses without simulation progress, repeatability, a complete race and reset all passed. See [installation and live verification](tickgym-setup.md). The server must be installed independently; installing the Python extra alone does not enable stepping. The client exposes telemetry and optional lidar, **not a rendering API**. This backend therefore returns no RGB image. Pairing an unrelated game screenshot with a headless simulation tick would not produce a synchronized observation.

The normal-game backend does not advertise lockstep. FPS limiting, longer sleeps, process suspension and menu pauses are not substitutes for acknowledged simulation steps.

## Install and configure realtime

On the Linux machine running the game:

```bash
pip install -e '.[live]'
python scripts/install_tmrl_plugin.py /path/to/OpenplanetNext/Plugins
```

The installer downloads only the unchanged, hash-pinned signed `TMRL_GrabData.op` from the upstream TMRL resource archive. It preserves any different existing plugin, does not install the full TMRL framework and does not reset a `TmrlData` directory. Reload/restart Openplanet after installation. The plugin listens on localhost TCP port 9000.

Use an unlocked X11 session with Trackmania active, a local single-player map and the desired third-person gameplay camera. Establish the screen region of the game content. A gamepad needs access to `/dev/uinput` and the system libevdev library; the library does not change device permissions or invoke sudo. Keyboard mode needs xdotool and a verified focused Trackmania window. Configure these prerequisites for the desktop user running the game. See the upstream [Linux setup guide](https://github.com/trackmania-rl/tmrl/blob/master/readme/install_linux.md).

New virtual gamepads wait two seconds for Steam/game discovery before the first input report. Configure `gamepad_connect_settle_seconds` on `TMRLBackend`, or `connect_settle_seconds` on `GamepadController`, if the installation needs a different delay. An injected, already-connected pad skips this wait. An immediate reset on a newly created device was ignored during live validation; a start-position check can still pass if the car was already at the start.

Manually restart the chosen map at its starting line and record the telemetry position as `expected_start_position`. This map-specific anchor is required: a key press and a delay alone cannot establish that a reset worked. Reset verification requires fresh telemetry, proximity to that anchor, low speed and no finish flag. This is a start-state check, not cryptographic map identity; the plugin supplies no map UID. Reset bindings and countdown behavior must be verified once for the installation.

Reset must use the game's **Give Up** binding, which restarts the race clock, rather than checkpoint respawn. The defaults are gamepad B (`reset_button=0x2000`, or `8192` in JSON) and keyboard Delete (`reset_key="Delete"`); both are configurable on `TMRLBackend` and their controller classes. BackSpace is checkpoint respawn on the tested installation. The backend waits `reset_settle_seconds=1.5`, then verifies fresh start-position samples. An observed finish additionally triggers gamepad A confirmation after `finish_popup_delay_seconds=0.5`; custom input devices must supply `dismiss_finish_popup()` for this case. The built-in keyboard controller has no verified finish-popup handler and rejects resets after a finish until a suitable handler is supplied. The reported `reset_binding` makes the configured input explicit, while `race_clock_reset_verified=False` reflects that GrabData cannot verify the clock itself. Confirm the visible countdown and restarted clock during installation testing.

The same start pose can be reported behind a pre-race menu. GrabData exposes neither menu state nor readiness to accept controls, so pose verification alone cannot prove the race is playable. Inspect the captured frame and confirm movement plus reported inputs in a short pilot before collecting training data. Keep the virtual controller connected throughout setup and collection; changing input devices may change the game's active player/device state. Starter Access may block local maps: select a playable free campaign track or use an account with the required access.

If the pre-race menu responds to mouse hover but ignores clicks, Enter and gamepad confirmation, the Ubisoft Connect overlay may still be capturing input. On our Ubuntu/Proton installation, pressing **Shift+F2 to open the Ubisoft overlay, then Shift+F2 to close it**, restored race entry with the same connected controller. Check visually that the overlay has closed, enter the race and rerun the [control checker](../examples/verify_live_controls.py) before collecting data. This is an observed recovery for this installation, not a universal diagnosis or an automatic library action. Ubisoft documents the [overlay shortcut and settings](https://www.ubisoft.com/en-us/help/article/000103536); a [Trackmania Proton report](https://github.com/ValveSoftware/Proton/issues/6507#issuecomment-3025504251) describes the same open/close recovery after input loss.

While the car is stationary at that start, run `python examples/probe_live_telemetry.py --samples 5 --interval 0.2`. The [telemetry probe](../examples/probe_live_telemetry.py) only connects to the GrabData receiver; it creates no input controller, sends no controls and captures no desktop images. It prints fresh JSON records containing position, raw speed and host receipt timing. Confirm the repeated positions are stable, then use that XYZ triple as `expected_start_position`; the probe does not verify the map or choose an anchor automatically. Use `--host`, `--port` and `--timeout` when needed, and `--help` for all options. Receipt age is not a measurement of game-clock or producer latency.

```python
from trackmaniabench.live import Action, LiveTrackManiaEnv
from trackmaniabench.live.tmrl import TMRLBackend

backend = TMRLBackend(
    control="gamepad",
    capture_region={"left": 0, "top": 0, "width": 1920, "height": 1080},
    expected_start_position=(START_X, START_Y, START_Z),
    image_size=(224, 224),
    action_lease_seconds=10.0,
)
with LiveTrackManiaEnv(backend, control_hz=20, max_episode_seconds=120) as env:
    observation, info = env.reset()
    while True:
        # Your model chooses bounded [steer, throttle, brake] controls.
        action = policy(observation)
        observation, reward, terminated, truncated, info = env.step(action)
        if terminated or truncated:
            observation, info = env.reset()
```

Construct controls as `Action(steer, throttle, brake)` or a length-three array. Steering is in `[-1, 1]`; throttle and brake are in `[0, 1]`. Nonfinite and out-of-range values are errors. Gamepad resolution and digital keyboard/tickgym pedals are explicit quantization; `info` contains both requested and applied controls. The adapter does not silently clip an unbounded Gaussian policy.

The configurable action lease releases controls if the controller stops refreshing them. Disconnection, capture/control failure and closing the backend also attempt release. Failed watchdog releases are retried; unresolved release errors raise and trigger cleanup. An expired lease invalidates the ongoing episode rather than pretending the policy's command remained held. The standalone environment returns a resettable invalid truncation when release succeeded and telemetry remains available. Other failures close the environment. The watchdog runs in the Python process and cannot protect against that process being forcibly terminated. Keep a context manager or call `close()` in `finally`.

## Controller-paced telemetry

After [separately provisioning a compatible TICK server](tickgym-setup.md):

```python
from trackmaniabench.live import LiveTrackManiaEnv
from trackmaniabench.live.tickgym_backend import TickGymBackend

backend = TickGymBackend("/data/maps/circuit.Map.Gbx")
with LiveTrackManiaEnv(backend, control_hz=20) as env:
    observation, info = env.reset(seed=1)
    observation, reward, terminated, truncated, info = env.step([0, 1, 0])
    assert info["clock_mode"] == "lockstep"
    assert "image" not in observation
```

Map bytes are uploaded from the Python host, avoiding assumptions about Wine path mapping. A reset begins at tick -1; the first five-tick step executes ticks 0 through 4. The backend retains the server's race time and actual tick difference. It closes a failed session instead of retrying a step with uncertain execution. Steering rounds to an integer in `[-127, 127]`; throttle/brake use the threshold 0.5. The API does not expose arbitrary game-memory mutation.

## Observations, rewards and timing

The framework-independent API follows `reset() -> (observation, info)` and `step() -> (observation, reward, terminated, truncated, info)` without requiring Gymnasium.

Observation fields are independent NumPy arrays:

- `image`, when available: RGB `uint8[H,W,3]`.
- `telemetry`, `float32[12]`: speed, position XYZ, race time, checkpoint count, gear, RPM, finish flag, race-time-valid flag, checkpoint-count-valid flag, reserved zero. Absent optional numeric fields are zero; use the validity flags and backend contract. The speed unit is in `info["speed_unit"]`.
- `timing`, `float32[4]`: elapsed host wall time, elapsed simulation time or zero, observation-to-input-write delay, simulation-time-valid flag.
- `previous_action` and `applied_action`, each `float32[3]`.

GrabData sends raw `CSmScriptPlayer.Speed` and `Distance` values without unit annotations. They are retained with source provenance; no unverified conversion or route-progress interpretation is imposed. Its packets contain no game clock or sequence counter. The receiver assigns local packet sequence and monotonic receive time, separately records screen capture times, and exposes their skew. Observations after an action require a packet received after the device write. Finish events remain latched until a verified reset, even if newer packets leave the finish screen. These are host measurements, not exact physics or exposure timestamps. In realtime mode `game_dt_s` is `None`.

A realtime transition can contain both the previous action during inference and the new action after the device write. The timing and action-history fields make that distinction observable. The host write completion time is not a physics acknowledgement. A receiver timestamp also cannot prove there was no earlier producer/network delay. Exact offline frame/control alignment is a separate contract from this live interface.

The default reward is a finish bonus of 1 minus an elapsed-time cost of 0.01 per second. It uses acknowledged simulation duration for lockstep and host duration for realtime. It deliberately supplies no guessed checkpoint or distance progress. Pass a callable `reward_fn(previous_sample, current_sample, info)` to implement a map-specific reward. Success terminates an episode; time/step limits and an action-lease expiry truncate it. The standalone environment requires an explicit reset after either.

## Elemental integration

```python
from trackmaniabench.live.elemental import ElementalTrackManiaEnv

with ElementalTrackManiaEnv(backend=backend) as env:
    obs = env.reset()
    # policy returns a bounded Torch tensor with shape [1, 3].
    obs, reward, done, info = env.step(policy(obs))
```

Install the `elemental` extra or use Elemental's existing Torch environment. The adapter is structurally compatible with `env.base_vec_env.BaseVecEnv`; importing Elemental itself is unnecessary. It returns fresh normalized RGB float32 `[1,3,H,W]` images when available, batched telemetry/timing/history, text prompts and `[1]` reward/done tensors. It honors a writable `device`. Completed episodes auto-reset because Elemental's collector does not issue per-episode resets; `final_observation`, `final_info`, `terminated` and `truncated` remain in `info`.

Merge the component blocks in [realtime config](../configs/elemental-trackmania-realtime.yaml) or [tickgym config](../configs/elemental-trackmania-tickgym.yaml) into an Elemental RL experiment. Use `num_action_chunks: 1`, `action_dim: 3` and buffer `action_shape: [1,3]`. Stock PPO executes only the first row of a predicted action chunk. The example [Elemental smoke collector](../examples/elemental_trackmania_smoke.py) runs its actual PPO collection, buffer and GAE with constant bounded controls; it is an integration check, not a trained racing policy.

Elemental's current PPO/GAE uses a fixed discount per decision and merges termination with time-limit truncation. It does not implement elapsed-time discounts or time-limit bootstrapping. Thus realtime is compatible with its decision-indexed objective, but is not automatically a time-correct semi-Markov learner. Include timing/action-history keys in `obs_keys` if a policy needs them; the stock collector otherwise discards most `info`. A custom collector/GAE is required for duration-aware discounting. Tickgym telemetry-only experiments need a telemetry policy rather than an image-only conditioner.

The adapter raises and closes on invalid control transitions, including expired leases. Elemental's stock collector cannot discard them based on `info`, so returning them would contaminate its rollout buffer. Restart the collection with a new environment after fixing the cause. Invalid policy outputs likewise close and release immediately.

Stock Elemental also resets at every rollout boundary. The supplied 128-step configuration is a short integration sample: at 20 Hz lockstep it covers only 6.4 simulation seconds. Choose a rollout length sufficient for the track (at least 2400 decisions for a 120-second limit at 20 Hz), or adapt the collector to preserve episodes across rollouts. Realtime controls stay held during optimization until the action lease expires or the next reset. This is not a background training scheduler.

## Verification

Read [the current validation record](live-validation.md). Automated API compatibility does not establish that this installation accepts driving inputs.

Run `python scripts/check.py` for offline regressions. With Elemental's `src` on `PYTHONPATH` and Torch/Hydra available, run `python -m unittest discover -s tests -p test_live_elemental.py -v` to exercise the actual PPO collector, buffer, GAE and configuration instantiation. Missing optional dependencies are explicit skips, not successful integration claims.

Before training on a game installation, verify reset position and countdown, accelerator/brake/steering signs and quantization, camera/crop, telemetry freshness, capture latency, watchdog release, disconnect behavior and terminal reset. Use a short controlled rollout before long training. The live checks below complement the offline protocol regressions.

The [control checker](../examples/verify_live_controls.py) **drives the car** for four short phases, verifies movement and reported input signs, then resets. It creates the controller before an optional readiness prompt, emits a JSON report and exits nonzero on failure:

```bash
python examples/verify_live_controls.py \
  --backend-options backend.local.json --wait-for-ready > control-verification.json
```

The JSON file supplies the same `TMRLBackend` arguments as the Python example above. A successful check still needs visual camera inspection and a complete finish/reset test; reported steering signs are corroboration, not a calibrated measurement of turning direction.

The [real-time timing checker](../examples/verify_realtime_timing.py) then verifies movement with held controls during a simulated inference delay, action-history/timing attribution, action-lease expiry, neutral controls, rejection of the expired transition and reset recovery:

```bash
python examples/verify_realtime_timing.py \
  --backend-options backend.local.json > realtime-timing.json
```

It deliberately uses a one-second lease and exits nonzero if any check fails. For controller-paced simulation, run [the lockstep verifier](../examples/verify_lockstep.py) with a known finishing input sequence as described in [the TICK setup guide](tickgym-setup.md).

The [episode checker](../examples/verify_realtime_episodes.py) follows a supplied reference path to exercise actual finishes and repeated resets. Select `--adapter elemental` to test automatic resets. Its JSON reference contains `samples` with XYZ `position` and `velocity` vectors; no map or player recording is bundled. The simple controller needs tuning for the map, steering deadzone and observation latency. A time limit or failed reset is reported as failure, never a completed race. Use `--captures` to retain terminal and reset images for inspection.

Primary sources: [TMRL telemetry/control source](https://github.com/trackmania-rl/tmrl/tree/master/tmrl/custom/tm), [signed-plugin confirmation](https://github.com/trackmania-rl/tmrl/discussions/104), [tickgym package and requirements](https://pypi.org/project/tickgym/1.1.0/), [tickgym stepping contract](https://tm.achepta.com/tickgym/api/gym.html).
