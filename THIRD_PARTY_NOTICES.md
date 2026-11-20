# Third-party components

The repository's original code is distributed under GPL-3.0-or-later. The pinned .NET dependencies retain their respective licenses:

- GBX.NET 2.5.1: MIT, copyright Petr Pivoňka.
- TmEssentials 2.8.0: MIT, copyright Petr Pivoňka.
- GBX.NET.LZO 2.1.6: GPL-3.0-or-later, including the bundled LZO dependency.
- GBX.NET.ZLib 1.1.3: MIT; underlying compression components retain their own notices.

The full retained notices are in `extractor/THIRD_PARTY_NOTICES.md`. NuGet lockfiles record transitive dependencies and package content hashes. Consult the licenses in restored packages when distributing compiled runtime bundles.

Python dependencies NumPy and Pillow retain their upstream licenses. FFmpeg, .NET, Openplanet, Trackmania and GhostToReplay are external prerequisites, not vendored binaries. No signed Openplanet plugin is redistributed or modified here.

The optional live API uses MSS, vgamepad, libevdev's Python bindings, PyTorch and tickgym under their upstream licenses. The TMRL telemetry protocol and unchanged signed GrabData plugin come from https://github.com/trackmania-rl/tmrl. The installer fetches a hash-pinned upstream plugin; its source and binary are not included in this repository. TICK's server is a separate prerequisite and is not distributed here.

Trackmania.io supplies leaderboard and map metadata. **Powered by Trackmania.io.** Its API usage policy is https://openplanet.dev/tmio/api. The data collection code preserves attribution; the code license does not relicense API data, replay recordings, maps or game artwork.
