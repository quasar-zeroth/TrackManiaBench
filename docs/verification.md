# Verification before scaling

The pipeline records what was checked and binds supporting files by SHA-256. A JSON boolean without the referenced evidence is not a valid pilot gate.

| Stage | Required checks |
| --- | --- |
| Leaderboard | Map UID/ID, contiguous cohort ordinals, ordered scores, unique accounts and record UUIDs, source response hashes and acquisition times. Pagination is not atomic. |
| Download | Frozen record UUID, official source URL, binary GBX class, file size/hash, immutable map and pilot files. Never substitute a newer PB or lower-ranked record. |
| Extraction | Source identity, map/account/finish, supported packed input framing, event semantics, held-action bounds and complete or explicitly reviewed partial coverage. |
| Replay conversion | Native replay has the same controls, visual samples, account, finish, map semantics and source payload as the frozen ghost. Filenames do not prove identity. |
| Scene preparation | Actual native scaffold; one intended car and External chase camera; camera/sample coverage; no inherited effect tracks; clean and diagnostic variants preserved. |
| Rendering | Guarded UI plan, cache proof, source/native readback, settings evidence, closed writer, stable files, full decode, original frame PTS. Never retry an uncertain GUI transaction automatically. |
| Pilot review | First five runs: car following view, scene/visibility, marker readability and correspondence, independent clock corroboration and measured error bound. Review evidence and notes are required. |
| Production QA | The same profile/gate; source variant checks; new ambiguous images or clocks require explicit review. Preserve failed automatic reports. |
| Join | Real frame times, every intersecting 100 Hz substep, no invented controls or images, strict/experimental masks, original next-frame target, deterministic whole-player splits. |
| Release | Complete requested cohort, hash inventory, relocatable evidence graph, standalone verification without original-machine paths. |

## Offline tests

`python scripts/check.py` runs self-contained tests in isolated temporary directories. They use synthetic GBX headers and temporary videos to exercise invariants; synthetic artifacts never become real gate evidence. The C# extractor's `--self-test` covers packed input decoding, timing, held states and rejection behavior.

Run the .NET builds with `dotnet restore --locked-mode` before publication or deployment. The CI workflow performs Python tests, static error checks, locked builds of the extractor, scene inspector and optional control probe, and package builds. It does not access a game account, call leaderboard APIs or publish datasets.

See the [initial release validation record](release-validation.md) for the checks performed during this source reorganization.

## Live validation boundaries

A passing offline suite does not certify a new display layout, game update, track, camera or physics version. Establish a fresh native pilot and retain its settings, source identities, evidence files and gate. Keep the original first-five manifest when increasing collection size; do not refresh its leaderboard.

No original production approvals or image references ship in this public source tree. They were specific to one cohort and installation. New evidence must come from the current experiment. Unsupported input formats and unreviewed source coverage remain errors.

## Publication checks

Keep the data workspace outside tracked code or under the ignored `data/` directory. Do not commit game assets, replay GBXs, rendered videos, signed plugin bundles, account sessions, tokens, SSH material, or private machine configuration. `bench.local.toml` is ignored; `configs/example.toml` contains placeholders and a public sample map only.

Generated datasets are separate artifacts. The code license does not settle rights to redistribute game artwork, maps or player recordings.
