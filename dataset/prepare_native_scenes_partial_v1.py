#!/usr/bin/env python3
"""Index native Replay_Save files by their contents and prepare verified scenes.

Uses the unchanged action extractor and Clip inspector for all GBX semantics.
Native import, video rendering, observation review and calibration remain pending.
Production execution requires a complete source manifest and the actual five-run gate. A candidate native index only selects files; nested native identity and all source/action/map bytes are independently checked. Source-clock approval remains explicitly pending.
"""

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import join_dataset_partial_v1 as j
import coverage_policy_v1 as coverage

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "native_cohort_clip_checks", HERE.parent / "renderer/native_ui_queue_partial_v1.py"
)
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


def command(binary, dotnet):
    return ([str(Path(dotnet).resolve(strict=True))] if dotnet else []) + [
        str(Path(binary).resolve(strict=True))
    ]


def package(binary):
    return {
        p.name: j.sha(p) for p in sorted(Path(binary).resolve(strict=True).parent.iterdir()) if p.is_file()
    }


def invoke(cmd):
    completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    j.require(
        completed.returncode == 0,
        "GBX tool rejected input: " + completed.stdout[-2500:] + completed.stderr[-2500:],
    )


def source_run(metadata, by_account, map_uid):
    j.require(
        metadata.get("status") == "extracted"
        and metadata.get("container_class") == "CGameCtnReplayRecord"
        and metadata.get("ghost_count") == 1,
        "Input is not an extracted single-ghost native replay",
    )
    j.require(metadata.get("account_id") in by_account, "Native replay account is outside requested cohort")
    run = by_account[metadata["account_id"]]
    j.require(
        metadata.get("map_uid") == map_uid and metadata.get("replay_map_uid") == map_uid,
        "Native replay map differs",
    )
    j.require(
        metadata.get("finish_time_ms") == run["record_time_ms"]
        and metadata.get("replay_finish_time_ms") == run["record_time_ms"],
        "Native replay finish differs",
    )
    return run


def extracted_files_equal(original, native, original_dir, native_dir):
    required = {
        "actions_all_100hz.csv",
        "actions_race_100hz.csv",
        "input_changes.json",
        "input_events.json",
        "input_store_000.bin",
        "visual_samples.csv",
    }
    j.require(
        set(original["files"]) == set(native["files"]) == required, "Unsupported extracted file/store layout"
    )
    checked = []
    for name in sorted(required):
        a = j.pin(original_dir / name, original["files"][name]["sha256"])
        b = j.pin(native_dir / name, native["files"][name]["sha256"])
        j.require(
            a["sha256"] == b["sha256"] and a["bytes"] == b["bytes"],
            "Native replay changes original action/sample file: " + name,
        )
        checked.append(dict(name=name, original=a, native=b, byte_equal=True))
    return checked


def validate_index(entries, count):
    j.require(len(entries) == count, "Native replay count differs from exact requested cohort")
    for key in ("run_id", "account_id", "ordinal"):
        j.require(len({e["run"][key] for e in entries}) == count, "Duplicate native replay " + key)
    j.require(
        {e["run"]["ordinal"] for e in entries} == set(range(1, count + 1)),
        "Native replay cohort has missing leaderboard positions",
    )


def prepare(args):
    manifest_path = Path(args.actions).resolve(strict=True)
    manifest = j.read(manifest_path)
    j.require(type(args.count) is int and args.count > 0, "Positive cohort count required")
    j.require(0 <= args.preroll_ms <= 5000 and 0 <= args.postroll_ms <= 5000, "Padding must be in0..5000ms")
    requested = (
        [int(x) for x in args.ordinals.split(",")] if args.ordinals else list(range(1, args.count + 1))
    )
    j.require(
        requested == sorted(set(requested)) and requested and all(1 <= x <= args.count for x in requested),
        "Invalid ordinal selection",
    )
    if args.diagnostic_variants_only:
        j.require(
            requested == sorted(c["ordinal"] for c in coverage.RECORD_CASES.values()),
            "Diagnostic staging must include all registered record variants",
        )
    j.require(
        manifest.get("status") == "actions_verified"
        and not manifest.get("failures")
        and not manifest.get("source_collection_failures"),
        "Action source cohort is incomplete",
    )
    runs = sorted(manifest["runs"], key=lambda r: r["ordinal"])
    j.require(
        manifest["requested_count"] == args.count
        and len(runs) == args.count
        and [r["ordinal"] for r in runs] == list(range(1, args.count + 1)),
        "Action manifest must contain exactly requested contiguous cohort",
    )
    for key in ("run_id", "account_id", "ghost_sha256"):
        j.require(len({r[key] for r in runs}) == args.count, "Duplicate source " + key)
    if args.count > 5:
        j.require(
            args.gate and args.render_root,
            "Production preparation requires a measured pilot gate and render root",
        )
        gate_path = Path(args.gate).resolve(strict=True)
        gate = j.renderer.validate_gate(gate_path, manifest, Path(args.render_root).resolve(strict=True))
        for pilot in gate["pilot_runs"]:
            j.evidence(pilot["calibration"], gate_path.parent)
    map_ref = j.pin(manifest["map"]["path"], manifest["map"]["sha256"])
    scaffold = Path(args.native_scaffold).resolve(strict=True)
    output = Path(args.output).resolve()
    extractor_command = command(args.extractor, args.extractor_dotnet)
    inspector_command = command(args.inspector, args.inspector_dotnet)
    tools = dict(
        extractor_package=package(args.extractor),
        inspector_package=package(args.inspector),
        helper=j.pin(Path(__file__).resolve()),
        coverage_policy=j.pin(Path(coverage.__file__).resolve()),
        native_validator=j.pin(HERE.parent / "renderer/native_ui_queue_partial_v1.py"),
    )
    tools["reviewed_coverage_policy"] = coverage.policy_reference()
    replay_files = sorted(Path(args.replays).resolve(strict=True).glob("*.Replay.Gbx"))
    j.require(
        len(replay_files) == args.count, "Replay directory must contain exactly requested *.Replay.Gbx files"
    )
    j.require(
        len({j.sha(p) for p in replay_files}) == args.count,
        "Duplicate native replay bytes under different filenames",
    )
    by_account = {r["account_id"]: r for r in runs}
    indexed = []
    if args.native_index:
        native_index = Path(args.native_index).resolve(strict=True)
        rows = [json.loads(line) for line in native_index.read_text().splitlines()]
        entries = [r for r in rows if r.get("role") == "native"]
        j.require(
            len(entries) == args.count and len({r["account_id"] for r in entries}) == args.count,
            "Native diagnostic index is incomplete/duplicated",
        )
        chosen = []
        for entry in entries:
            j.require(entry["account_id"] in by_account, "Foreign native diagnostic identity")
            run = by_account[entry["account_id"]]
            j.require(
                entry["map_uid"] == manifest["map"]["uid"]
                and entry["finish_time_ms"] == run["record_time_ms"],
                "Native diagnostic identity differs",
            )
            path = Path(entry["source_path"]).resolve(strict=True)
            j.require(path in replay_files, "Diagnostic native path outside requested directory")
            j.pin(path, entry["source_sha256"])
            if run["ordinal"] in requested:
                chosen.append(path)
        replay_files = sorted(chosen)
        tools["candidate_native_index"] = j.pin(native_index)
    sources = {}
    for run in runs:
        if run["ordinal"] not in requested:
            continue
        j.renderer.safe_name(run["run_id"])
        directory = Path(run["actions"]["directory"]).resolve(strict=True)
        metadata_ref = j.pin(directory / "metadata.json", run["actions"]["metadata_sha256"])
        metadata = ui.metadata(metadata_ref["path"], run["ghost_sha256"], run, manifest["map"]["uid"])
        j.load_actions(directory, metadata, run["record_time_ms"])
        sources[run["run_id"]] = dict(
            ghost=j.pin(run["ghost_path"], run["ghost_sha256"]), metadata=metadata_ref
        )
    for replay in replay_files:
        native_ref = j.pin(replay)
        identity = dict(native_replay=native_ref, map=map_ref, tools=tools)
        folder = output / "index" / native_ref["sha256"]
        record_path = folder / "index.json"
        if record_path.exists():
            item = j.read(record_path)
            j.require(item["source_identity"] == identity, "Completed replay index sources/tooling changed")
            for ref in item["files"]:
                j.evidence(ref, folder)
        else:
            j.require(not folder.exists(), "Interrupted replay index retained; choose a new output cohort")
            folder.mkdir(parents=True)
            invoke(
                extractor_command
                + [
                    str(replay),
                    "--output",
                    str(folder / "actions"),
                    "--expected-map-uid",
                    manifest["map"]["uid"],
                ]
            )
            metadata = j.read(folder / "actions/metadata.json")
            run = source_run(metadata, by_account, manifest["map"]["uid"])
            item = dict(
                schema_version=1,
                source_identity=identity,
                run=run,
                native_replay=native_ref,
                action_metadata=j.pin(folder / "actions/metadata.json"),
                files=[j.pin(f) for f in sorted((folder / "actions").iterdir()) if f.is_file()],
            )
            j.checked_write(record_path, j.encoded(item))
        metadata = j.read(item["action_metadata"]["path"])
        run = source_run(metadata, by_account, manifest["map"]["uid"])
        j.require(item["run"] == run, "Indexed native replay source run changed")
        j.require(metadata["source_sha256"] == native_ref["sha256"], "Indexed replay source hash changed")
        if run["ordinal"] in requested:
            indexed.append(item)
    j.require(
        len(indexed) == len(requested) and sorted(i["run"]["ordinal"] for i in indexed) == requested,
        "Parsed native subset differs or repeats identity",
    )
    produced = []
    for item in sorted(indexed, key=lambda x: x["run"]["ordinal"]):
        run = item["run"]
        rid = run["run_id"]
        source = sources[rid]
        j.evidence(item["native_replay"], output)
        source_identity = dict(
            run=run,
            map=map_ref,
            original_source=source,
            native_replay=item["native_replay"],
            native_action_metadata=item["action_metadata"],
            native_scaffold=j.pin(scaffold),
            tools=tools,
            preroll_ms=args.preroll_ms,
            postroll_ms=args.postroll_ms,
        )
        folder = output / "runs" / rid
        proof_path = folder / "prepared.json"
        if proof_path.exists():
            old = j.read(proof_path)
            j.require(
                old["source_identity"] == source_identity, "Completed prepared run sources/tooling changed"
            )
            for ref in old["files"]:
                j.evidence(ref, folder)
            produced.append(old)
            print(json.dumps(dict(run_id=rid, status="prepared_reused_verified")), flush=True)
            continue
        j.require(not folder.exists(), "Interrupted prepared run retained; choose a new output cohort")
        folder.mkdir(parents=True)
        original_meta = j.read(source["metadata"]["path"])
        native_meta = j.read(item["action_metadata"]["path"])
        equality = extracted_files_equal(
            original_meta,
            native_meta,
            Path(source["metadata"]["path"]).parent,
            Path(item["action_metadata"]["path"]).parent,
        )
        j.checked_write(
            folder / "actions-equivalence.json",
            j.encoded(
                dict(
                    schema_version=1,
                    all_extracted_files_byte_equal=True,
                    original_action_metadata=source["metadata"],
                    native_replay_action_metadata=item["action_metadata"],
                    files=equality,
                    video_alignment_verified=False,
                )
            ),
        )
        native_copy = folder / f"DatasetR{run['ordinal']:03}.Replay.Gbx"
        shutil.copyfile(item["native_replay"]["path"], native_copy)
        j.pin(native_copy, item["native_replay"]["sha256"])
        comparison = folder / "replay-comparison.json"
        invoke(
            inspector_command
            + [
                "--compare-replay",
                source["ghost"]["path"],
                str(native_copy),
                map_ref["path"],
                str(comparison),
            ]
        )
        compared = j.read(comparison)
        j.require(
            compared["status"] == "native_replay_gameplay_sources_verified"
            and compared["canonical_record_equal"]
            and compared["semantic_map_verification"]["passed"],
            "Native replay semantics differ",
        )
        for key, wanted in dict(
            map_uid=manifest["map"]["uid"], account_id=run["account_id"], finish_time_ms=run["record_time_ms"]
        ).items():
            j.require(compared.get(key) == wanted, "Native replay comparison identity differs: " + key)
        rebound = folder / "ReboundNative.Clip.Gbx"
        invoke(
            inspector_command
            + ["--rebind-native-ghost", str(scaffold), str(native_copy), str(comparison), str(rebound)]
        )
        notices = folder / "source-clock-notices.json"
        invoke(inspector_command + ["--inspect-ghost-clocks", source["ghost"]["path"], str(notices)])
        # Rendering preserves the source timeline. No source-clock approval is implied by scene preparation.
        outputs = []
        for role in ("clean", "clock"):
            clip = folder / f"DatasetR{run['ordinal']:03}{role.title()}.Clip.Gbx"
            inspection = folder / (role + ".inspected.json")
            invoke(
                inspector_command
                + [
                    "--control-clip",
                    str(rebound),
                    str(clip),
                    "--preroll-ms",
                    str(args.preroll_ms),
                    "--postroll-ms",
                    str(args.postroll_ms),
                ]
                + (["--markers"] if role == "clock" else [])
            )
            invoke(inspector_command + [str(clip), str(inspection)])
            timeline = ui.clip_inspection(
                clip,
                inspection,
                run,
                Path(source["metadata"]["path"]).parent / "visual_samples.csv",
                "diagnostic" if role == "clock" else "production",
            )
            outputs.append(dict(role=role, clip=j.pin(clip), inspection=j.pin(inspection), timeline=timeline))
        record = dict(
            schema_version=1,
            status="native_sources_and_scenes_verified_clock_pending",
            run=run,
            source_identity=source_identity,
            native_replay=j.pin(native_copy),
            replay_comparison=j.pin(comparison),
            action_equivalence=j.pin(folder / "actions-equivalence.json"),
            native_action_metadata=item["action_metadata"],
            source_notices=j.pin(notices),
            source_clock_verified=False,
            outputs=outputs,
            files=[j.pin(p) for p in sorted(folder.iterdir()) if p.is_file()],
            native_import_verified=False,
            video_alignment_verified=False,
        )
        j.checked_write(proof_path, j.encoded(record))
        produced.append(record)
        print(json.dumps(dict(run_id=rid, status=record["status"])), flush=True)
    manifest_out = dict(
        schema_version=1,
        status="native_sources_and_scenes_verified_clock_pending",
        requested_count=args.count,
        prepared_count=len(requested),
        selected_ordinals=requested,
        diagnostic_variants_only=args.diagnostic_variants_only,
        source_clock_verified=False,
        map=manifest["map"],
        actions_manifest=j.pin(manifest_path),
        runs=produced,
        index_files=[j.pin(output / "index" / i["native_replay"]["sha256"] / "index.json") for i in indexed],
        native_import_verified=False,
        video_alignment_verified=False,
    )
    manifest_output = output / (
        "native_scene_subset_manifest.json"
        if args.diagnostic_variants_only
        else "native_cohort_manifest.json"
    )
    j.checked_write(manifest_output, j.encoded(manifest_out))
    return dict(status=manifest_out["status"], runs=len(requested), manifest=str(manifest_output))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("actions", "replays", "native-scaffold", "extractor", "inspector", "output"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--count", type=int, required=True)
    p.add_argument(
        "--native-index", help="Optional pre-index; all candidate contents are independently verified"
    )
    p.add_argument("--preroll-ms", type=int, default=200)
    p.add_argument("--postroll-ms", type=int, default=200)
    p.add_argument("--ordinals")
    p.add_argument("--diagnostic-variants-only", action="store_true")
    for name in ("extractor-dotnet", "inspector-dotnet", "gate", "render-root"):
        p.add_argument("--" + name)
    args = p.parse_args()
    try:
        print(json.dumps(prepare(args)))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print("Rejected: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
