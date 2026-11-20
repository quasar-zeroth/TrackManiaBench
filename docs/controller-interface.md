# Offline/live controller boundary

The [live Python API](live-rl.md) supports real-time game interaction and an optional exact-step telemetry backend, with an Elemental adapter. These live observations are distinct from the measured alignment of exported replay videos.

1. **Offline observations** retain run ID, original frame index and actual video PTS. Eligible transitions target the original next frame, without joining across filtered gaps.
2. **Live realtime observations** carry host capture/receive timestamps and previous/applied controls. The game continues during inference. The signed telemetry plugin exposes no simulation clock, so host time is never relabeled as game time.
3. **Live lockstep observations** carry acknowledged tick counts and race time from a separately installed TICK server. Its current Python API supplies no synchronized RGB.
4. **Actions** use steering, throttle and brake. Original replay controls retain their 100 Hz source timeline; live control resolution and digital pedal thresholds depend on the backend. Requested and applied controls stay distinguishable.
5. **Validity** preserves unknown source inputs, conditional offline timing, live packet/capture skew, watchdog faults and termination/truncation separately. Missing observations are not fabricated.
6. **Learning** preserves episode boundaries and offline whole-player splits. Stock Elemental PPO discounts per decision; elapsed-time discounting requires a custom collector/GAE. Offline replay physics and live controller behavior must be compared under known game versions and measured latency.

A working API is not a trained policy or evidence of racing performance. Establish reset, action, camera and timing checks on the installation before scaling training.
