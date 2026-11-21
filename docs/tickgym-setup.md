# Native Linux controller-paced simulation

TrackManiaBench's tickgym backend has been tested against the **native Linux TICK 3.0.1 optimizer** using a Steam/Proton Trackmania 2020 installation. It simulates the game's physics in separate headless sessions. The desktop game need not advance alongside a gym. The backend supplies telemetry, not RGB frames.

## Install TICK

Install the Linux x86_64 release from the publisher's [TAS Tools page](https://tmtas.exchange/tas-tools), then install the Python client:

```bash
pip install -e '.[tickgym]'
```

The public GitHub URL in the Python package's metadata was unavailable during validation. The publisher's download page supplies the installer. The [3.0 release notes](https://tmtas.exchange/api/tas-tools/tick/changelog) describe the standalone optimizer and native Linux support. Installing the Python package alone does not install this service.

The tested Linux manager requires **GLIBC_2.39** and the optimizer requires **GLIBC_2.38**. Ubuntu 22.04's system runtime cannot run these binaries directly. Validation used an isolated Ubuntu 24.04 libc 2.39 directory without replacing any system libraries. The launcher below supports that directory explicitly. Installing the graphical manager on an older distribution also requires a compatible runtime; merely setting an optimizer argument does not solve the manager's runtime requirement. A system providing the required libc avoids this extra setup.

TICK's optional graphical overlay also needs GTK4/WebKitGTK6. It is not needed for the standalone optimizer tested here. No overlay, game DLL injection, or desktop input automation was used for these headless checks.

## Start the optimizer

TICK can discover game folders after being run with the game, as described in its [requirements](https://tm.achepta.com/tickgym/). The tested optimizer also accepts those folders explicitly. This allows headless startup with the installed game resources:

```bash
python scripts/start_tick_optimizer.py \
  --optimizer "$HOME/.local/share/tick/versions/3.0.1/optimizer/TickOptimizer" \
  --game-data "/path/to/steamapps/common/Trackmania/GameData" \
  --packs "/path/to/steamapps/common/Trackmania/Packs" \
  --user-data "/path/to/compatdata/2225070/pfx/drive_c/users/steamuser/Documents/Trackmania"
```

When using an isolated runtime, append `--glibc-library-directory /path/to/runtime/usr/lib/x86_64-linux-gnu`. That directory must contain the matching `ld-linux-x86-64.so.2` and `libc.so.6`. The launcher starts only this executable with those libraries, in the foreground. Ctrl+C stops it.

The default control endpoint is `http://127.0.0.1:46323`. The server also allocates a local TCP gym port. Run Python on the same machine, or forward both the HTTP endpoint and the gym endpoint returned by `Client().status()`. Forwarding only HTTP is insufficient. The launcher only accepts loopback addresses.

If opening a map returns `game-resources-missing`, check all three resource directories. If startup reports a missing GLIBC version, use a compatible isolated runtime or operating system. Do not replace the system libc. A schema mismatch means the Python client and TICK server versions are incompatible; the tested pair is tickgym 1.1.0 and TICK 3.0.1.

## Verify actual simulation

Run the [live verifier](../examples/verify_lockstep.py) against a local map and a known successful input sequence:

```bash
python examples/verify_lockstep.py /data/circuit.Map.Gbx \
  --finish-inputs /data/known-finish.txt > lockstep-verification.json
```

The finish file uses the upstream [InputText format](https://tm.achepta.com/tickgym/quickstart.html): timestamped `steer`, `accel`, `brake`, and an optional initial `seed`. The verifier supports those driving controls; respawn and other input kinds are rejected. Timestamps are milliseconds aligned to 10 ms ticks. Choose inputs that finish with the installed physics version; old records can diverge.

The verifier checks server-acknowledged snapshot ticks across wall-clock pauses both before and after driving, exact step lengths, reported inputs, movement, braking, reset, and reproducibility across different inference delays. It then checks TrackManiaBench's environment at 20 Hz and replays the finishing sequence through `TickGymBackend`, followed by a reset. Without a finish file, successful mechanics checks return `partial` and exit code 2; full success requires a completed episode and reset. No check claims RGB support.

On 2026-11-21, these checks passed on Summer 2020 – 01 (map UID `XJ_JEjWGoAexDWe8qfaOjEcq5l8`) with TICK build `3.0.1+57d9a57933d36e0b17727e74e337aa403caa9eab`. A sequence from the existing dataset completed in **19.723 seconds**, traversed all three checkpoints including the finish, and reset to tick -1. The desktop game and original dataset were not modified by that test.
