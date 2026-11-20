# TrackManiaBench

Build Trackmania 2020 datasets from leaderboard replays: **native third-person chase-camera video, original controls, measured timing, and reproducible training masks**.

This project reorganizes the pipeline used to produce a verified 250-run dataset for **Summer 2020 – 01**. It includes the actual native Replay Editor workflow, source decoding, quality checks, frame/action joining, and portable release verification. Map UID, cohort size, paths, remote machine, and dataset splits are configurable.

The project supports offline dataset generation for world models and autoregressive models, plus a **Python live-control API compatible with Elemental's RL environment interface**. Realtime control uses the running game's clock and third-person screen capture. An optional tickgym backend supports acknowledged simulation steps through a separately provisioned TICK server; that backend is telemetry-only and has not been validated under Proton. See [the live RL guide](docs/live-rl.md) for setup and timing limits.

The live API is experimental. Its automated Elemental integration passes, but the first Ubuntu desktop test was blocked at the game's pre-race menu; successful driving and terminal reset are not yet established. See [the live validation record](docs/live-validation.md) before using it for training.

## What it produces

- Silent native WebM video at **640×360, 50 FPS**, with the External camera following the car from behind and above.
- Original 100 Hz controls, raw input events, source hashes, player/record identity, and game-build provenance.
- One row per original video frame, actual PTS, all intersecting action substeps, and next-frame transition masks.
- Separate strict and experimental timing masks. Missing controls, uncertainty, preroll and post-finish frames remain explicit.
- Whole-player train/validation/test splits and a portable release with hash verification and the complete audit evidence graph.

The historical batch contained 250 runs, with 158 supported by direct checkpoint timing and 92 carrying conditional timing assumptions. Those counts describe that batch, not what a new track is guaranteed to produce. The dataset and its private operational evidence are not included in this repository.

## Install

Use Linux with Python 3.11+, FFmpeg/ffprobe, and .NET SDK 8 to build the GBX tools. Native rendering needs Trackmania 2020, a working graphical session, Openplanet and the unmodified signed **GhostToReplay** plugin. The rendering worker uses SSH, Python, xdotool and screenshot tools on the game machine. Account/editor/plugin permissions must work in the installed game.

```bash
git clone https://github.com/quasar-zeroth/TrackManiaBench.git
cd TrackManiaBench
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

tmbench init --output bench.local.toml
# Edit the map UID, cohort, storage and game-machine paths in bench.local.toml.
tmbench doctor --config bench.local.toml
tmbench plan --config bench.local.toml
```

Build the extractor and scene inspector using their pinned NuGet lockfiles; commands are in [the dataset guide](docs/dataset.md). Render setup and dependencies are in [the rendering guide](docs/rendering.md).

## Start with five runs

```bash
# Freeze metadata for all configured records; default: top 250.
tmbench snapshot --config bench.local.toml

# Download only the pilot, preserving exact record identities.
tmbench download --config bench.local.toml --count 5
tmbench batch --config bench.local.toml

# Discover the exact arguments for each production stage.
tmbench tool extract -- --help
tmbench tool prepare-scenes -- --help
tmbench tool --config bench.local.toml render -- --help
tmbench tool audit -- --help
tmbench tool pilot-gate -- --help
```

Follow [the complete workflow](docs/workflow.md) to convert replays, obtain a native scene scaffold, prepare a machine-specific UI profile, render the pilot, and measure its alignment. Only then promote the same frozen cohort:

```bash
tmbench download --config bench.local.toml --count 250 \
  --gate /data/cohort/pilot-gate.json \
  --pilot /data/cohort/collection/pilot-manifest.json \
  --render-output /data/cohort/rendered
```

Rendering, routine QA, joining and packaging are scripted. First-time UI calibration, the measured pilot, and ambiguous visual exceptions need explicit review. The original batch required review of 36 exceptions. This release does not claim unattended success on arbitrary maps, game updates or display layouts. It refuses missing evidence instead of silently accepting a run.

The validated video profile is fixed at 640×360/50 FPS. Changing it requires updating and validating the pixel/timing checks; changing a configuration number alone is insufficient.

## Source layout

| Directory | Responsibility |
| --- | --- |
| `trackmaniabench/` | CLI, configuration, stage discovery |
| `trackmaniabench/live/` | Realtime controls/capture, optional tickgym stepping, Elemental adapter |
| `collector/` | Cached leaderboard snapshot, official replay downloads, signed-plugin batch files |
| `extractor/` | Strict GBX input decoder and batch integrity checks |
| `dataset/clip-inspector/` | Native scene inspection, replay comparison, controlled chase-camera clips |
| `setup/` | Guarded native GUI rendering, staging and file closure |
| `renderer/` | Scene, camera, marker, visibility and evidence validation |
| `dataset/` | Source clocks, calibration, joins, splits and portable releases |
| `tests/` | Offline acquisition/configuration regression tests |

Version suffixes are retained where they distinguish evidence schemas or production validation semantics. Historical machine recovery scripts, superseded prototypes, real review approvals, game files and compiled binaries are excluded.

See [data and action semantics](docs/dataset.md), [verification](docs/verification.md), and [live controller timing](docs/controller-interface.md).

## Tests

```bash
python scripts/check.py
dotnet run --project extractor/TrackmaniaExtractor.csproj -- --self-test
```

Offline tests exercise collection resume/tamper rejection, input parsing, synthetic native-video decoding, alignment uncertainty, masks, deterministic splits, and portable-release relocation. They do not stand in for a new map's live game pilot.

## Attribution and license

**Powered by Trackmania.io.** Only leaderboard pages and map metadata use its API; replay downloads use frozen UUIDs at official Nadeo URLs. Requests are cached and paced, and errors/rate limits stop collection. Read the [Trackmania.io API policy](https://openplanet.dev/tmio/api) before use, including its bulk-use and attribution requirements.

TrackManiaBench code is licensed under **GPL-3.0-or-later**. See [LICENSE](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md). Game assets, player recordings, upstream plugins and generated datasets are not granted rights by this code license. TrackManiaBench is an independent research project, unaffiliated with Ubisoft, Nadeo or Openplanet.
