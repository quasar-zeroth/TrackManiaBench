# Boundary for a future RL controller

This release creates offline data. It does not expose live game control, reset a running episode, compute rewards, or claim an online RL environment.

Future work should consume the same contracts:

1. **Observation identity:** run ID, original frame index and actual video PTS. Frame indices are never renumbered after filtering. An eligible transition targets the original next frame, not the next retained frame across a gap.
2. **Time:** explicit mapping between video, replay-record and race clocks with measured error bounds. Conditional evidence stays conditional. Source game builds are part of provenance.
3. **Actions:** raw held steering, throttle and brake on the original 100 Hz timeline, with respawn impulses kept at their actual event times. A 50 FPS interval can contain several clipped substeps; do not replace them with an arbitrary single sampled action.
4. **Validity:** strict action-conditioned masks, separate experimental masks and video-only usability. Missing first controls and unavailable endpoint images are not fabricated.
5. **Episodes:** preserve run boundaries and whole-player data splits. Offline recorded inputs do not guarantee that replay physics or online control behavior match another game version.

A later controller can add an observation source, action sink, reset/termination behavior and reward function behind a separate interface. Before online/offline comparisons, measure action latency and exposure timing in the running game and validate that steering conventions and simulation versions match. Nothing in this repository assumes those checks have already passed.
