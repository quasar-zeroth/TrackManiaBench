# Native rendering and verification

The working export route uses Trackmania (2020), Steam app 2225070, running under Proton on Linux, and the game's MediaTracker **Shoot** renderer. The camera is `External` / game camera 2, following entity 1 from behind and above. Clean clips contain one source car and no diagnostic text. A paired clock clip contains three timing markers. Source RecordData, controls and raw video frames are preserved.

This port retains the production export and evidence checks, but has not been run through a new live game session after reorganization. A new installation or map needs the pilot below. Rendering is a sequential script after configuration; Steam/Ubisoft sign-in, native replay conversion/scaffold setup, UI-plan capture and actual visual review remain operator tasks. Unrecognized UI states, uncertain Shoot outcomes, ambiguous images and unsupported clocks stop or quarantine. The software does not manufacture a passing review.

The supported profile is 640×360 at 50 FPS, silent VP8/WebM, with a 1920×1080 game window at desktop origin `(0,0)`. Pixel regions, text geometry and timing bounds depend on this profile. Changing resolution, FPS, font or camera requires new validators and measured pilot evidence, not a config override.

## Machine setup

The controller needs Python with the project's rendering dependencies, FFmpeg/ffprobe, SSH/SCP and the built .NET scene inspector. Durable concurrent QA additionally requires Linux `/proc` and a Python build exposing `os.pidfd_open` and `signal.pidfd_send_signal`; the Linux distribution Python normally supplies these APIs. The game machine needs Steam, Trackmania and its account access, a working Proton installation, an unlocked X11 session, passwordless SSH, Python with NumPy/Pillow, `xdotool`, `xprop` and ImageMagick's `import`. Wayland input and arbitrary window scaling are not supported. The active window must report `steam_app_2225070` and its process must be Trackmania.exe under Wine; the worker checks PID lifetime, focus, dimensions and origin before every action.

Configure `[remote]` in the project TOML and invoke scripts through `tmbench tool --config CONFIG ...`, or export these explicitly when calling scripts directly:

```sh
export TRACKMANIABENCH_SSH_HOST=render-pc
export TRACKMANIABENCH_EXPECTED_HOSTNAME=render-pc
export TRACKMANIABENCH_GAME_DOCUMENTS=/absolute/path/to/Proton/Documents/Trackmania
export TRACKMANIABENCH_REMOTE_DATA=/absolute/path/to/datasets/my-track
export TRACKMANIABENCH_DISPLAY=:0
export TRACKMANIABENCH_XAUTHORITY=/absolute/path/to/your/Xauthority
export TRACKMANIABENCH_XDOTOOL=xdotool
```

`SSH_HOST` is an SSH config alias or `user@hostname`. Host-key checking stays strict. `EXPECTED_HOSTNAME` is the actual remote `hostname`, which can differ from the alias. Only these named renderer variables are transmitted; account credentials and unrelated environment variables are not forwarded. `TRACKMANIABENCH_LIBRARY_PATH` optionally supports a nonstandard xdotool installation.

Create a dedicated data directory on the game machine. Configure the game's native video output to reach `<REMOTE_DATA>/render_staging/videos` and verify that destination with one manually observed export before enabling the queue. A symlink can route an existing game output directory, but first preserve all pre-existing files and verify the game's actual path; the queue does not relocate unknown files or create this routing. The staging directory must be empty before every render. The queue archives and hash-checks each completed native file outside staging before removing only that freshly owned staging file. This avoids native `VideoNN` filename wrap overwriting previous exports.

No power control, Steam credentials, signed plugin bundles or game files are included in this repository.

## Native replay and scaffold preparation

1. Download original ghosts and the exact map with the collector and extract the first five source actions. Keep the frozen collection order and hashes.
2. Install the signed **GhostToReplay** plugin through Openplanet's normal plugin installation flow. The original production run used its batch conversion UI; the custom renderer/plugin experiments were not the working export route. Convert the first five ghosts using the game's native replay-saving path. For a larger batch, confirm the complete input list visually before starting: asynchronous X11 typing can outlive the typing process, so a returned `xdotool` command is not proof the complete list reached the UI.
3. Open a verified replay for that map in MediaTracker. Export a native `.Clip.Gbx` scaffold containing its original entity/camera. Save this actual file; the inspector uses it to preserve native chunk structure and appearance. A synthetic scratch clip is not a replacement.
4. Run scene preparation and source-clock verification as described in [dataset stages](dataset.md). Replay matching uses embedded map/account/finish/control evidence, not filenames. Scene preparation produces clean and clock clips, native action-equivalence evidence, inspection reports and per-clip `.provenance.json` files.
5. Keep the editor on the same verified map. The fixed-slot queue imports clips into this editor; a generic toolbar screenshot cannot identify the map. Source replay/map equivalence and the operator's editor context are separate required evidence.

## Capture the UI plan once

`setup/ui_plan_blueprint.json` contains the measured clear/import/readback/prepare/Shoot sequences and conservative image guards. It is an **unvalidated blueprint**, with no screenshots or approval. Review its coordinates against the local game UI and edit a private copy if necessary. It clears the current editor tracks after matching the confirmation dialog, so archive any desired unsaved clip first.

Use the read-only capture command while manually navigating each listed state:

```sh
python setup/native_ui_worker.py capture --output /data/captures/session-001
python setup/native_ui_worker.py self-test
```

The evidence directory contains a PNG and identity/event log. Make full 1920×1080 captures with these basenames in a separate capture directory:

| Capture | Actual state to observe |
| --- | --- |
| `editor_ready.png` | MediaTracker editor with its transport controls |
| `editor_zero.png` | Editor playhead at time zero |
| `delete_confirmation.png` | Delete-all-tracks confirmation |
| `editor_empty.png` | Editor after deleting tracks |
| `clips_parent.png` | Clips picker parent containing `DatasetQueue` |
| `queue_folder.png` | `DatasetQueue` picker containing `DatasetSlot.Clip.Gbx` |
| `slot_selected.png` | That exact slot selected with its full filename |
| `export_dialog.png` | Clip export dialog in the Clips parent |
| `readback_named.png` | Export name exactly `DatasetReadback` |
| `shoot_named.png` | Shoot dialog named `DatasetQueueVideo` |
| `shoot_settings.png` | All visible export settings matching the supported profile |

The fixed paths are `Replays/Clips/DatasetQueue/DatasetSlot.Clip.Gbx` and `Replays/Clips/DatasetReadback.Clip.Gbx` beneath game Documents. The optional queue `seed` operation creates the owned slot without GUI input. Manually refresh the picker to index it if needed. Never create a fake cache proof to skip this step.

```sh
python setup/build_ui_plans.py --captures /data/captures/reference \
  --output /data/ui-plans
python setup/native_ui_worker.py validate /data/ui-plans/clear.json
python setup/native_ui_worker.py run /data/ui-plans/clear.json \
  --output /data/ui-preflight
```

Without `--execute`, `run` only observes the first guarded state. It does not navigate the full plan. Review every generated plan and reference region, including readable settings values. A white-text guard checks missing and additional glyphs; blank or nondistinctive reference crops fail. Re-capture/review after UI layout or settings changes.

## Cache proof, pilot render, then production

Use a verified native cohort with original action metadata and a reviewed `plans.json`. The builder records the supplied reviewer; it does not inspect or decide whether the review was performed.

```sh
python setup/build_render_config.py --cohort /data/prepared-with-clocks.json \
  --plans /data/ui-plans/plans.json --mode cache --queue-id cache-001 \
  --reviewer 'Actual reviewer' --output /data/cache-config.json
python setup/render.py validate /data/cache-config.json
python setup/render.py seed /data/cache-config.json \
  --output /data/seed-evidence --execute
python setup/render.py cache-proof /data/cache-config.json \
  --inspector build/clip-inspector/TrackmaniaClipInspector.dll \
  --dotnet "$(command -v dotnet)" --output /data/cache-evidence --execute
```

`cache-proof` imports **A-clean, B-clean, A-clean, A-clock, A-clean** at the same slot and compares every native readback against its intended scene. It needs two distinct source runs. It binds the map, trajectories, source/tool hashes, reviewed plans, remote configuration and current game-process lifetime. It renders no video. Restarting the game or changing code/plans requires a fresh proof.

```sh
python setup/build_render_config.py --cohort /data/prepared-with-clocks.json \
  --plans /data/ui-plans/plans.json --mode pilot --queue-id pilot-001 \
  --reviewer 'Actual reviewer' --output /data/pilot-config.json
python setup/render.py run /data/pilot-config.json \
  --cache-proof /data/cache-evidence/cache-proof.json \
  --inspector build/clip-inspector/TrackmaniaClipInspector.dll \
  --dotnet "$(command -v dotnet)" --expected-video-start 1 \
  --output /data/pilot-candidates --execute
```

The expected video number is the logical preservation sequence, not a guess about the game's next filename. Empty staging must produce exactly one new `DatasetQueueVideo.webm` or native `VideoNN.webm`; that file is preserved under the declared logical number. Choose an unused logical range for subsequent queues. Each run produces both clean and clock candidates, so five pilots use ten numbers.

Every export checks native scene readback before Shoot, one fresh output, return to editor, no open writer descriptors in the declared game process, stable size/hash, lossless preservation of native bytes, codec/resolution/FPS/audio and error-free full decode. Each candidate remains explicitly **unaccepted and unaligned** until review/calibration.

For ordinals 6 through N, build `--mode production` and run with both `--pilot-gate /data/pilot-gate.json` and `--accepted-output /data/accepted`. These must be actual verified first-five results from the same collection, map and settings. Counts are derived from the manifest; no original 231-run remainder or source-specific timing partition is assumed.

## Review and calibrate the pilot

First run `renderer/scan_native_video_partial_v1.py --help` for each pilot candidate. Pass its video, native readback clip/inspection/comparison, original action metadata, frozen manifest and ordinal. The scan retains every frame's metrics, selected original-pixel images, review sheets and anomaly indices. `renderer/pilot_video_audit.py mosaic VIDEO OUTPUT --first N --last M` produces every-frame mosaics for the proposed visible race interval.

Independently review those images, source car, third-person camera, visible interval and all flagged neighborhoods. Record a JSON review containing `verified_by`, `verified_utc`, `method`, pinned `native_video`, `first_usable_frame_index`, `last_usable_frame_index`, `frames_reviewed`, `reviewed_contiguous_race_visibility:true`, pinned `evidence_files` and `unresolved_issues:[]` only after observing those facts. A calibration index is a list of `{ordinal, scan:{path,sha256}, review:{path,sha256}}` entries for positions 1–5.

```sh
python renderer/validate_video_scanner.py /data/scanner-review-index.json \
  /data/scanner-validation.json --expected-count 5
export TRACKMANIABENCH_SCANNER_VALIDATION=/data/scanner-validation.json
python renderer/production_video_audit_conditional_v2.py prepare --help
```

The scanner calibration recomputes all-frame boundaries and requires agreement with the actual reviews on the same map. `prepare` consumes clean/clock candidates, source-clock proof and accepted root, then writes an **unpassed review draft** plus pulse/correspondence/visibility diagnostics. Fill its review fields from real evidence, retaining the draft's identity fields. Use its `accept --help` command with the completed review. It verifies native/accepted pixel equality and immutable resume before accepting the video. Calibration and training masks are separate stages in [dataset.md](dataset.md).

For a first pilot, every frame is reviewed. Production still requires selected-frame and anomaly review unless a newly calibrated automatic policy passes. Missing source clocks, short tracks with colliding timing markers and unfamiliar image geometry remain unsupported until independently validated.

## Automatic QA and scalable operation

`renderer/prepare_audit_queue_conditional_v2.py` prepares completed clean/clock pairs without issuing GUI actions. `renderer/durable_dataset_supervisor_v1.py` schedules QA, incremental calibration, join, export and verification using frozen JSON command specifications. Stage retries are limited to resumable verification/transfer operations; uncertain acceptance is quarantined. Its `configure` command freezes source/tool pins before `run`. Local-only finalization uses the first four stages (calibration, join, export, verification); transfer stages are optional.

Automatic image QA is specific to the map/profile. Before enabling it, supply fresh, actually reviewed reference data:

- `TRACKMANIABENCH_SETTINGS_IMAGE` and `_SHA256`, plus `TRACKMANIABENCH_SETTINGS_REVIEW` and `_SHA256`, identify an actual settings screenshot and its completed review.
- `TRACKMANIABENCH_CAMERA_AUDIT` and `_SHA256` identify the independently reviewed camera evidence.
- `TRACKMANIABENCH_TESSDATA` and `_SHA256` identify your installed English Tesseract model; `TRACKMANIABENCH_TESSERACT_LIBRARY` is the actual absolute shared-library path. OCR assets are not bundled. Model/library bytes are pinned into policy identity.

`automatic_video_policy_v1.py build-reference` builds and tests a scene bank from independently accepted, explicitly reviewed runs in a supplied audit root. It needs at least two runs for leave-one-run-out comparisons, and may require more varied reviewed trajectories to cover a new map. The retained thresholds may fail on new scenery; failures do not authorize threshold relaxation.

For the current v2 marker fallback, `marker_glyph_cache_v1.py` and `marker_glyph_cache_rgb_v2.py` extract hash-bound RGB marker crops from actual reviewed pairs. `marker_glyph_decoder_v1.py build-bank` needs all ten digit classes. `validate_marker_glyph_rgb_v2.py` runs leave-one-run-out tests, actual altered-image negatives and separately supplied holdout pulse records (`--holdouts`: list of `{ordinal,pulses:{path,sha256}}`, excluded from fitting). `regress_automatic_video_policy_rgb_v2.py` rechecks combined OCR/glyph decisions against wrong numeric labels, wrong phases and altered pixels. `finalize_automatic_policy_rgb_v2.py` binds the passing scene, glyph and joint reports into a reference; it cannot promote missing or failed checks.

`production_video_audit_automatic_v2.py` evaluates/accepts only after those references validate. It scans every frame, checks scene/camera correspondence, corruption/freeze/black frames, settings, exact marker identity and timing. Unsupported or ambiguous cases retain their original failed report for explicit review. `production_video_audit_reviewed_exceptions_v1.py` can consume separately attested image review; no pre-approved exception records ship here. The probe/analyzer in `renderer/research/vehicle-controls` provides optional control-reflection diagnostics, with separate conditional timing limitations.

After the first five accepted/calibrated runs and passing automatic references exist, generate a concrete local-only proposal. All paths below refer to actual artifacts produced earlier, never placeholder attestations:

```sh
python renderer/prepare_audit_queue_conditional_v2.py configure \
  --render-config /data/production-config.json \
  --native-cohort /data/prepared-with-clocks.json \
  --candidates /data/production-candidates --audit-root /data/audits \
  --accepted /data/accepted --gate /data/pilot-gate.json \
  --output /data/audit-queue.json
python setup/build_pipeline_spec.py \
  --actions /data/actions/actions_manifest.json \
  --cohort /data/prepared-with-clocks.json --gate /data/pilot-gate.json \
  --accepted /data/accepted --audit-root /data/audits \
  --audit-queue /data/audit-queue.json --watcher-state /data/audit-watcher \
  --reference /data/automatic-reference/reference.json \
  --calibrations /data/calibrations --calibration-state /data/calibration-state.json \
  --joined /data/joined --release /data/release \
  --state /data/supervisor --output /data/pipeline-proposal.json
python renderer/durable_dataset_supervisor_v1.py configure \
  /data/pipeline-proposal.json --output /data/pipeline-config.json
python renderer/durable_dataset_supervisor_v1.py run /data/pipeline-config.json
```

The audit queue is built from the actual native queue config and identifies each expected clean/clock candidate pair. Start its prepare watcher in a separate terminal before the supervisor:

```sh
python renderer/prepare_audit_queue_conditional_v2.py run \
  /data/audit-queue.json --state /data/audit-watcher --watch
```

Retain that state directory at the path supplied above. The supervisor reads that watcher's identity to pause/resume decoding safely, while native rendering proceeds separately. Review the generated proposal's exact argument arrays and output checks before freezing it. `configure` performs no stages; `run` starts them. The builder supports cohorts of 6–250, requires the five accepted pilots, and emits four final stages: calibration, join, portable export and local verification. It adds no transfer command.

The supervisor writes durable state under `/data/supervisor/state.json` and immutable attempt receipts alongside it. Restart the same `run` command after an ordinary process interruption; changed inputs, tool bytes or uncertain acceptance prevent automatic retry. A new reference or changed code requires a separately reviewed new configuration. `qa-spec` can generate one concrete QA worker specification for inspection before processing the remainder. The JSON is editable before freezing, but each stage must retain positive output/evidence checks; a zero exit status alone does not constitute completion.

## Stop and resume

Do not share the desktop with another controller while rendering. Local and remote cooperative locks prevent competing queues. On any timeout, the queue logs `automatic_retry:false`; **never resend an uncertain Shoot**. Inspect the actual game state, staging file, pending ownership marker, writer closure and immutable journals. A partial/uncommitted job requires explicit reconciliation and a new attempt; the portable queue intentionally does not transplant the original experiment's one-off recovery decisions.

A known completed prefix can resume with `--completed-prefix FILE --attempt-id NEW_ID`. The file contains the ordered `candidate_results` pins for fully committed jobs. The queue rehashes those sources/results, confirms corresponding remote commit receipts, requires an empty staging directory with no pending transaction, then skips only that exact prefix. It does not rerender it. A pending render or partial harvest stops before proceeding.

Candidate files, original reviews and final datasets remain outside this source repository. A successful local test suite verifies logic and corruption rejection; it does not replace a fresh game/UI/render pilot on a new machine.
