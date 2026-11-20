# Dataset stages and evidence

The production path retains separate source clocks, native video clocks, and training masks. A successful source extraction or scene preparation is not evidence of visible rendering or correct frame/action alignment. Files are immutable after acceptance; changed sources, tools, settings, or evidence require a new output cohort and a new measured pilot.

The stable entry points are versioned because those versions identify validation contracts. `join_dataset.py`, `join_dataset_partial_v1.py`, and the first automatic visibility builder remain dependency adapters for explicit validation of older evidence. New cohorts use the `automatic_v2` calibration, join, and export entry points.

## Build the two source tools

Python 3.11+, FFmpeg/ffprobe and .NET 8 are needed. Build the extractor as described in [extractor/README.md](../extractor/README.md). Build the native scene inspector separately:

```sh
dotnet restore dataset/clip-inspector/TrackmaniaClipInspector.csproj --locked-mode
dotnet publish dataset/clip-inspector/TrackmaniaClipInspector.csproj -c Release \
  --no-self-contained -o build/clip-inspector -p:RestoreLockedMode=true
dotnet build/clip-inspector/TrackmaniaClipInspector.dll --self-test-timing
```

The inspector preserves source RecordData and player appearance while constructing an External camera (camera 2/entity 1), the usual behind-and-above third-person chase view. Clean scenes have no diagnostic text or renderer HUD. A second scene includes three timing pulses. Native import/re-export and rendered visibility must still be checked. The unsupported scratch-entity construction route remains disabled.

## First five, then production

1. Freeze the leaderboard collection, including source map, original ghosts, hashes and exact ordinal order. Extract positions 1–5 using `dataset/batch_extract_partial_v1.py`. Keep one source record per account and do not substitute lower ranks for failed records.
2. Use the native replay conversion/renderer workflow to obtain actual Replay_Save files and one actual native exported scaffold. Verify original/native actions and map semantics during scene preparation:

```sh
python dataset/prepare_native_scenes_partial_v1.py \
  --actions /data/actions/action_dataset_manifest.json --replays /data/replays \
  --native-scaffold /data/native-scaffold.Clip.Gbx \
  --extractor build/extractor/TrackmaniaExtractor.dll --extractor-dotnet "$(command -v dotnet)" \
  --inspector build/clip-inspector/TrackmaniaClipInspector.dll --inspector-dotnet "$(command -v dotnet)" \
  --output /data/scenes --count 5 --preroll-ms 200 --postroll-ms 200
```

The optional `--native-index` accelerates file selection; every selected replay is still independently parsed. `--ordinals` permits a controlled subset of a complete cohort. The requested count must match the action manifest. A count above five additionally requires `--gate` and `--render-root`.

3. Build source-clock proofs, then attach them to a new preparation manifest:

```sh
python dataset/build_source_clocks_v2.py --scenes /data/scenes/native_cohort_manifest.json \
  --output /data/source-clocks --report /data/source-clock-report.json
python dataset/finalize_native_clock_manifest_v2.py \
  --scenes /data/scenes/native_cohort_manifest.json --clocks /data/source-clocks \
  --output /data/prepared-with-clocks.json
```

Direct verification requires one unambiguous typed notice series containing every exact checkpoint time, including finish, with at least three events. Track length and checkpoint count are not hardcoded. Unsupported notice layouts, fewer checkpoints, missing finish notices or competing entities fail closed. Reflected-control evidence is a separately reviewed experimental route, never an automatic replacement for missing direct evidence.

4. Import/re-export each scene natively and verify readback using `verify_native_readback_v041.py --readback FILE --intended-clip FILE --intended-inspection FILE --inspector FILE --dotnet FILE --output-prefix PREFIX`. Render clean and diagnostic scenes. Verify camera, car visibility, settings, exact native/accepted decoded-frame equality, timing pulses, repeat-render consistency and immutable resume with the renderer tools. The fixed UI profile and marker decoder must be revalidated for new settings/maps. Very short tracks need an independently tested marker placement profile.
5. Build each pilot calibration from actual reports using `build_measured_calibration_automatic_v2.py`. Its `--help` lists required source files and explicit record/race anchors. Do not infer those anchors from an assumed zero start. Then join:

```sh
python dataset/join_dataset_automatic_v2.py --actions /data/actions/action_dataset_manifest.json \
  --render-root /data/accepted --calibrations /data/calibrations \
  --output /data/pilot-joined --count 5
```

6. Record the human/agent pilot review and its evidence. Generate an unpassed template, fill it from real checks, then assemble the gate:

```sh
python dataset/assemble_actual_pilot_gate.py --review-template --output /data/review-template.json
python dataset/assemble_actual_pilot_gate.py --dataset /data/pilot-joined/dataset_manifest.json \
  --review /data/completed-review.json --output /data/pilot-gate.json
```

Every review check needs its own hash-pinned evidence, explanation and explicit pass. The review identifies the reviewer and time and pins the exact joined manifest. The assembler reruns a complete join audit and validates all five actual render results before producing the gate; it cannot decide whether a reviewer's interpretation of an image is correct. Generated templates are deliberately unpassed.

7. Extend extraction and scene preparation to the desired `N` using the same frozen collection and actual five-run gate. Repeat source-clock proofs, native readback, rendering and per-run visual/timing checks. Process accepted production reports incrementally:

```sh
python dataset/calibrate_production_automatic_v2.py \
  --actions /data/actions/action_dataset_manifest.json --prepared /data/prepared-with-clocks.json \
  --gate /data/pilot-gate.json --render-root /data/accepted --audit-root /data/audits \
  --calibrations /data/calibrations --state /data/calibration-state.json \
  --clock-parameters-from-source-proofs --require-complete
python dataset/join_dataset_automatic_v2.py --actions /data/actions/action_dataset_manifest.json \
  --render-root /data/accepted --calibrations /data/calibrations \
  --output /data/joined --count 250 --gate /data/pilot-gate.json \
  --split-seed my-benchmark-v1 --split-fractions 0.8,0.1,0.1
python dataset/export_portable_automatic_v2.py --dataset /data/joined/dataset_manifest.json \
  --gate /data/pilot-gate.json --output /data/release
python /data/release/tools/export_portable.py --verify /data/release
```

Production calibration preserves the original five calibrations; missing reports remain pending. `--max-new` bounds a batch; `--require-complete` exits 3 while waiting, and failures exit 2. All positive cohort counts are supported by extraction/preparation/join/export; the production calibration supervisor requires more than five. Smaller pilots use the per-run builder. Cohorts of five or fewer keep the `pilot` split. Larger cohorts split whole accounts deterministically using the seed, with largest-remainder allocation of the configured train/validation/test fractions. Default 250-run allocation is 200/25/25. Export re-audits the joined data before copying and standalone verification recomputes split membership and all hashes without accessing original host paths.

## Training semantics

`frame_actions.jsonl` contains every actual decoded video frame and its PTS. Each action interval ends at the next frame PTS (the actual final duration is retained) and preserves every overlapping original 100 Hz control substep and raw event reference. Preroll, postfinish, unobserved images and missing controls remain explicit; no frames, actions or terminal successor images are synthesized.

Use `training_eligible` and `transition_eligible` for directly supported action-aligned training. Conditional control-reflection runs always have false strict masks and null unconditional time bounds; `experimental_training_eligible` and `experimental_transition_eligible` expose candidate pairs only under the recorded assumptions. `video_training_eligible` selects usable in-race images for video-only prediction. Read each run's observation window, source-clock evidence class, assumptions, missing intervals and alignment uncertainty before sampling. Transitions require both observations to be eligible.

The portable release contains relative training paths plus unchanged original evidence in a hash-addressed audit archive. Evidence can contain historical local paths; the release index resolves those references locally. Publishing the source repository does not publish a dataset, account records, game files or credentials.

## Reviewed exception policies

The default registry is empty. Set `TRACKMANIABENCH_COVERAGE_POLICY` to an absolute path to an independently reviewed JSON document only when original source evidence demonstrates a supported exception. The file is hash-pinned in action package identities, scene preparation, calibrations and joined provenance. Modifying it invalidates reuse. The structure is:

```json
{
  "schema_version": 1,
  "map_uid": "YOUR_MAP_UID",
  "cases": {},
  "record_cases": {},
  "record_end_cases": {},
  "native_source_bounds": []
}
```

`cases` maps run IDs to `ordinal`, `account_id`, `ghost_sha256`, `record_time_ms`, `input_version`, `ticks`, `input_end_ms`, `packed_sha256`, `record_start_ms`, `record_end_ms`. The supported partial input policy is exactly an original 10 ms prefix gap with an explicit complete vehicle update at the first observed tick. `record_cases` and `record_end_cases` contain independently reviewed visual RecordData exceptions with source identity and observed bounds; they do not imply missing actions. `native_source_bounds` entries bind `start_ms`, `end_ms`, `finish_time_ms` and `payload_sha256` of the compressed native record. A case's actual metadata/store bytes, source map/account/time and native equivalence still must pass all validators. Do not populate this file merely to silence a rejection.

Conditional control-reflection verification requires four explicit environment settings: `TRACKMANIABENCH_CONTROL_EVIDENCE_INDEX`, `TRACKMANIABENCH_CONTROL_EVIDENCE_SHA256`, `TRACKMANIABENCH_CONTROL_ANALYZER_SHA256`, and `TRACKMANIABENCH_EXACT_VERIFIER_SHA256`. Evidence must be independently packaged for the cohort with the included research analyzers. The verifier pins the reviewed index, reruns full-sample correlation and phase ambiguity, verifies archive-family checkpoint calibration and retains the assumption that reflected control timing does not establish physical-state timing. No historical study's acceptance counts or hashes are treated as evidence for a new track.

## Tests and production limits

```sh
python -m unittest discover -s dataset -p 'test_*.py' -v
```

The source-only suite uses temporary synthetic files and actual FFmpeg-generated test video. It covers clock composition, notice identity/hash/boundary ambiguity, unknown action intervals, strict versus conditional masks, immutable audit, portable relocation and tampering, review routing, arbitrary cohort splitting and reviewed-policy mutation. .NET self-tests exercise bitstream framing, explicit source-bound admission, native padding and refusal of fabricated source extent. Historical tests that required original player ghosts, videos, absolute machine paths or saved production evidence were removed; they are not silently represented as passing public integration tests. A new installation still needs a real 1–5-run gate before production. The existing game's UI/rendering profile has not been re-executed as part of this source cleanup.
