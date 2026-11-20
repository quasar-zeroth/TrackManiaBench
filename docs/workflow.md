# From a leaderboard to a verified dataset

Run from the repository root with the Python environment active. Paths below use `/data/cohort` as an example data workspace. Replace them with your configured workspace; `tmbench` resolves paths in TOML relative to the configuration file. Low-level `tmbench tool` commands accept explicit paths so every stage can be inspected and run separately.

## 1. Configure the experiment and build the tools

```bash
tmbench init --output bench.local.toml
tmbench doctor --config bench.local.toml
tmbench plan --config bench.local.toml

dotnet restore extractor/TrackmaniaExtractor.csproj --locked-mode
dotnet publish extractor/TrackmaniaExtractor.csproj -c Release \
  --no-self-contained -o build/extractor -p:RestoreLockedMode=true
dotnet restore dataset/clip-inspector/TrackmaniaClipInspector.csproj --locked-mode
dotnet publish dataset/clip-inspector/TrackmaniaClipInspector.csproj -c Release \
  --no-self-contained -o build/clip-inspector -p:RestoreLockedMode=true
```

Edit `dataset.cohort`, `dataset.count`, `track.uid`, `paths.workspace`, and the render machine settings. Use a new cohort directory when changing maps, snapshots, tools or reviewed evidence. No credentials belong in configuration. A public sample UID is supplied; its final verified dataset is not part of this repository.

The controller may run on a separate Linux machine. Data referenced by the extraction and validation commands must be locally readable there. Game staging paths are explicitly remote; the native worker copies the required scene files to its isolated slot.

## 2. Freeze metadata and collect the pilot

```bash
tmbench snapshot --config bench.local.toml
tmbench download --config bench.local.toml --count 5
tmbench batch --config bench.local.toml --output /data/cohort/pilot-batch.txt
```

The snapshot makes one map-metadata request plus paginated leaderboard requests, caches original responses, and preserves acquisition timestamps. Requests are paced by at least two seconds and stop on service errors or rate limits. It never calls the Trackmania.io ghost-download route. Follow the [service's API policy](https://openplanet.dev/tmio/api), including its request to discuss bulk usage with the operator.

Ghost downloads use the frozen record UUIDs at official Nadeo endpoints. A changed PB is never substituted. An unavailable or unsupported record remains an explicit failure. Native parsing later checks map/account/finish semantics; a valid GBX header alone is not proof of a correct run.

To use an existing frozen snapshot, put it at the configured `snapshot/leaderboard.json`, following the schema accepted by `collector.common.validate_snapshot`, and supply `paths.map_file`. Preserve the original acquisition provenance and snapshot hash. Automatic map download requires the snapshot's hash-pinned `map.json` receipt. Do not turn an arbitrary list of replay files into a claimed top-N leaderboard.

## 3. Extract controls and create native replays

```bash
tmbench tool extract -- /data/cohort/collection/pilot-manifest.json \
  --extractor build/extractor/TrackmaniaExtractor.dll --dotnet "$(command -v dotnet)" \
  --output /data/cohort/actions --limit 5
```

Install the unmodified signed [GhostToReplay plugin](https://openplanet.dev/plugin/ghosttoreplay) through its official distribution. Check its permissions in the actual game. Enable its batch mode and paste the generated `pilot-batch.txt`; it contains `mapUID; recordUUID` pairs. The plugin downloads/converts those exact records through the game and writes native Replay_Save files.

Collect the five resulting `.Replay.Gbx` files into a dedicated local directory, such as `/data/cohort/replays-pilot`. The preparation tool identifies them by parsed contents, not player-name filenames. Ensure the plugin's cached map is the same map bytes you froze. A conversion that changes source inputs, identity or circuit semantics is rejected.

Open one converted replay in the normal Replay Editor and export its actual native scene as `.Clip.Gbx`. Use this file as `/data/cohort/native-scaffold.Clip.Gbx`. A hand-constructed empty entity is not a substitute: the game needs native entity data and notices. Keep the original source file and its hash.

## 4. Prepare scenes and prove source clocks

Use the explicit scene and clock commands in [the dataset guide](dataset.md#first-five-then-production). They produce clean chase-camera clips, diagnostic clips, source equality reports, and `prepared-with-clocks.json`. The intended and natively re-exported scene must agree. Direct checkpoint timing and conditional control reflection remain distinct evidence classes.

## 5. Validate the UI and render the first five

Follow [the rendering guide](rendering.md) to create reviewed UI plans, reference images, an isolated staging slot and a cache proof for your game machine. The source repository cannot supply approvals for an unseen installation.

```bash
tmbench tool --config bench.local.toml native-ui -- --help
tmbench tool --config bench.local.toml render-config -- --help
tmbench tool --config bench.local.toml render -- --help
```

The renderer checks expected UI states before acting. It preserves every exported native file, verifies writer closure and stable hashes, and stops on an uncertain transaction. An interrupted GUI operation is inspected before resuming; rerunning the command does not blindly duplicate it.

Render both the clean and diagnostic clip for each pilot run. Check that the clean race footage follows the intended car from behind and above, with no inherited free camera, other cars, diagnostic overlays or missing images concealed by cropping. Keep every original frame and its PTS.

## 6. Review, calibrate and assemble the pilot gate

```bash
tmbench tool audit -- prepare --help
tmbench tool calibrate-pilot -- --help
tmbench tool pilot-gate -- --review-template --output /data/cohort/pilot-review-template.json
```

Review start/middle/finish diagnostic pulses, source clocks, clean/diagnostic correspondence, settings, visibility boundaries and repeat-render/resume evidence. Fill actual measured values and references. A template remains unpassed until that evidence exists.

Build the five per-run calibrations, join the five pilot runs, then assemble the gate as shown in [the dataset guide](dataset.md#first-five-then-production). The gate assembler reruns the join audit and checks referenced files; it does not invent a review or certify an image interpretation.

## 7. Promote and process the remaining records

```bash
tmbench download --config bench.local.toml --count 250 \
  --gate /data/cohort/pilot-gate.json \
  --pilot /data/cohort/collection/pilot-manifest.json \
  --render-output /data/cohort/rendered
tmbench batch --config bench.local.toml --output /data/cohort/all-records-batch.txt
tmbench tool extract -- /data/cohort/collection/manifest.json \
  --extractor build/extractor/TrackmaniaExtractor.dll --dotnet "$(command -v dotnet)" \
  --output /data/cohort/actions --limit 250 \
  --gate /data/cohort/pilot-gate.json --render-output /data/cohort/rendered
```

Replace `250` with the configured count. Preserve the five accepted pilot replays and convert only missing records; the generated all-records batch can be filtered to its remaining lines before sending it to the plugin. Prepare the full cohort into a new scene directory; preserve the original accepted pilot artifacts.

Build a production render queue from the full cohort and reviewed UI plans. The worker selects remaining production ordinals and serializes native rendering. Reuse automatic QA only after its scene, marker and settings references have passed the new map's pilot. Ambiguous cases stop for a recorded review; do not copy another cohort's exception approval. The row-level validators never call an agent or a language model.

## 8. Join, package and verify

Run the production calibration, join and export commands in [the dataset guide](dataset.md#first-five-then-production). Keep strict and experimental masks separate. For a 250-run cohort, the default whole-player split is 200/25/25; the seed and fractions are configurable.

```bash
python -I -S /data/cohort/release/tools/export_portable.py --verify /data/cohort/release
```

This final verifier uses Python's standard library and the release itself. It checks every inventory hash and the audit-reference graph without relying on original machine paths. A release is complete only after this verification passes for exactly the requested cohort. Keep the dataset on its storage machine; transfer is optional and is not a required pipeline stage.
