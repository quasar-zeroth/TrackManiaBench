#!/usr/bin/env python3
"""Calibrate completed, reviewed production renders; preserve original five pilots.

One invocation processes currently ready runs and records those still waiting.
Every clock parameter is explicit. Resume verifies the same sources, native
evidence, action files and calibration bytes without decoding unchanged video.
"""

import argparse
import fcntl
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import build_measured_calibration_automatic_v2 as builder
import build_measured_calibration_partial_v1 as legacy_builder
import build_measured_calibration_automatic_v1 as previous_automatic_builder
import join_dataset_automatic_v2 as j


CLOCK_FIELDS = ("record_anchor_ms", "race_anchor_ms", "race_ms_per_record_ms", "record_error_ms")


def clock_parameters(args):
    if args.clock_parameters_from_source_proofs:
        j.require(
            all(getattr(args, key) is None for key in CLOCK_FIELDS),
            "Choose explicit global parameters or measured per-run source proofs, not both",
        )
        return {"parameter_policy": "verified_source_clock_nominal_mapping_v2"}
    j.require(
        all(getattr(args, key) is not None for key in CLOCK_FIELDS),
        "Explicit record/race clock parameters or source-proof mode are required",
    )
    return {key: str(j.rational(getattr(args, key))) for key in CLOCK_FIELDS}


def measured_parameters(parameters, paths):
    if parameters.get("parameter_policy") != "verified_source_clock_nominal_mapping_v2":
        return parameters
    clock = j.read(paths["source_clock"])
    nominal = clock["nominal_mapping"]
    recomputed = builder.clocks.validate_proof(clock)
    j.require(
        clock["action_metadata"]["sha256"] == j.sha(paths["action_metadata"]),
        "Per-run clock parameters refer to another action source",
    )
    j.require(
        all(clock.get(k) == v for k, v in recomputed.items()),
        "Per-run clock parameter proof differs from actual notices",
    )
    return dict(
        record_anchor_ms=str(j.rational(nominal["record_anchor_ms"])),
        race_anchor_ms=str(j.rational(nominal["race_anchor_ms"])),
        race_ms_per_record_ms=str(j.rational(nominal["scale"])),
        record_error_ms=str(builder.contract.source_bound(clock)),
    )


def input_paths(run, prepared, render_root, audit_root, automatic_audit_subdir="automatic-policy-v1"):
    rid = run["run_id"]
    automatic = audit_root / rid / automatic_audit_subdir
    # Only committed automatic acceptance reports may replace historical routes.
    if (automatic / "acceptance-receipt.json").is_file():
        receipt = j.read(automatic / "acceptance-receipt.json")
        j.require(
            receipt.get("schema_version") == 2
            and receipt.get("status") == "accepted_calibration_join_still_required"
            and receipt.get("run_id") == rid
            and receipt.get("ordinal") == run["ordinal"],
            "Automatic acceptance receipt is incomplete or belongs to another run",
        )
        j.require(
            (automatic / "pulse-verification.json").is_file()
            and (automatic / "observation-window-verification.json").is_file(),
            "Committed automatic acceptance lacks required reports",
        )
        report = j.read(automatic / "observation-window-verification.json")
        j.require(
            report.get("review_mode") == "deterministic_decoded_image_policy_v1",
            "Automatic acceptance visibility policy differs",
        )
        report_root = automatic
    else:
        report_root = audit_root / rid
    return dict(
        render_result=render_root / rid / "result.json",
        action_metadata=Path(run["actions"]["directory"]) / "metadata.json",
        source_clock=Path(prepared["source_clock"]["path"]),
        pulses=report_root / "pulse-verification.json",
        visibility=report_root / "observation-window-verification.json",
    )


def collect_calibration_dependencies(cal, base):
    refs = {}

    def visit(value):
        if isinstance(value, dict):
            if isinstance(value.get("path"), str) and isinstance(value.get("sha256"), str):
                ref = j.evidence(value, base)
                refs[ref["path"]] = ref
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(cal)
    return [refs[key] for key in sorted(refs)]


def verified_run_inputs(run, manifest, prepared, paths):
    for key in ("run_id", "account_id", "ghost_sha256", "record_time_ms", "ordinal"):
        j.require(prepared["run"][key] == run[key], "Prepared run identity differs: " + key)
    j.evidence(prepared["source_clock"], Path(prepared["source_clock"]["path"]).parent)
    j.pin(paths["action_metadata"], run["actions"]["metadata_sha256"])
    j.pin(run["ghost_path"], run["ghost_sha256"])
    result = j.read(paths["render_result"])
    for key in ("run_id", "account_id", "ghost_sha256", "record_time_ms"):
        j.require(result["run"][key] == run[key], "Accepted production render identity differs: " + key)
    j.renderer.validate_render_result(result, manifest, run, paths["render_result"].parent)
    metadata = j.read(paths["action_metadata"])
    j.load_actions(paths["action_metadata"].parent, metadata, run["record_time_ms"])


def process_one(run, manifest, prepared, paths, output, proof_path, parameters):
    verified_run_inputs(run, manifest, prepared, paths)
    parameters = measured_parameters(parameters, paths)
    identity = dict(
        run_id=run["run_id"],
        sources={key: j.pin(value) for key, value in paths.items()},
        parameters=parameters,
        builder=j.pin(Path(builder.__file__)),
        supervisor=j.pin(Path(__file__)),
    )
    if proof_path.exists():
        proof = j.read(proof_path)
        j.require(
            proof["source_identity"] == identity, "Completed production calibration inputs/tooling changed"
        )
        j.require(
            proof["status"] == "measured_production_calibration_verified", "Incomplete calibration checkpoint"
        )
        j.require(
            Path(proof["calibration"]["path"]) == output, "Calibration checkpoint points to another output"
        )
        j.evidence(proof["calibration"], proof_path.parent)
        for ref in proof["pinned_dependencies"]:
            j.evidence(ref, proof_path.parent)
        return dict(
            run_id=run["run_id"],
            ordinal=run["ordinal"],
            status="calibration_reused_verified",
            calibration=proof["calibration"],
            checkpoint=j.pin(proof_path),
        )
    args = SimpleNamespace(
        **{key: str(value) for key, value in paths.items()},
        **parameters,
        output=str(output),
        check_only=False,
    )
    preserved_existing = output.exists()
    migration = None
    if preserved_existing:
        before = j.pin(output)
        mtime = output.stat().st_mtime_ns
        existing = j.read(output)
        old_source = existing.get("builder_inputs", {}).get("builder_sha256")
        if old_source == j.sha(Path(legacy_builder.__file__)):
            args.check_only = True
            result = legacy_builder.build(args)
            migration = dict(
                policy="revalidate_preserved_legacy_calibration_v1",
                legacy_builder=j.pin(Path(legacy_builder.__file__)),
                original_calibration=before,
            )
        elif old_source == j.sha(Path(previous_automatic_builder.__file__)):
            args.check_only = True
            result = previous_automatic_builder.build(args)
            migration = dict(
                policy="revalidate_preserved_automatic_calibration_v1",
                previous_automatic_builder=j.pin(Path(previous_automatic_builder.__file__)),
                original_calibration=before,
            )
        else:
            j.require(
                old_source == j.sha(Path(builder.__file__)),
                "Existing calibration was produced by an unknown builder",
            )
            args.check_only = True
            result = builder.build(args)
        j.require(
            j.pin(output) == before and output.stat().st_mtime_ns == mtime,
            "Existing calibration changed during migration/recovery validation",
        )
    else:
        result = builder.build(args)
    j.require(result["run_id"] == run["run_id"], "Calibration builder returned another run")
    calibration = j.read(output)
    proof = dict(
        schema_version=1,
        status="measured_production_calibration_verified",
        source_identity=identity,
        calibration=j.pin(output),
        builder_result=result,
        pinned_dependencies=collect_calibration_dependencies(calibration, output.parent),
        review_method=calibration["observation_window"]["method"],
        max_absolute_alignment_error_ms=calibration["mapping"]["max_absolute_error_ms"],
        video_redecode_on_resume_required=False,
        resume_policy="Full validation/decode occurred on creation. Resume rechecks native evidence, all actions, calibration and every pinned dependency; unchanged video bytes preserve the verified PTS/decode.",
        preserved_existing_calibration=preserved_existing,
        legacy_migration=migration,
    )
    j.checked_write(proof_path, j.encoded(proof))
    return dict(
        run_id=run["run_id"],
        ordinal=run["ordinal"],
        status=("calibration_reused_verified" if preserved_existing else "calibration_created_verified"),
        calibration=j.pin(output),
        checkpoint=j.pin(proof_path),
    )


def validate_cohort(actions, prepared):
    count = actions.get("requested_count")
    j.require(
        type(count) is int and count > 5,
        "Production calibration requires more than five runs; use the per-run builder for pilots",
    )
    j.require(
        actions.get("status") == "actions_verified"
        and actions.get("requested_count") == count
        and len(actions["runs"]) == count
        and not actions.get("failures")
        and not actions.get("source_collection_failures"),
        "Complete requested action cohort required",
    )
    j.require(
        prepared.get("status") == "native_cohort_prepared_import_pending"
        and prepared.get("requested_count") == count
        and len(prepared["runs"]) == count,
        "Complete verified native preparation required",
    )
    runs = sorted(actions["runs"], key=lambda r: r["ordinal"])
    native = {r["run"]["run_id"]: r for r in prepared["runs"]}
    j.require(
        [r["ordinal"] for r in runs] == list(range(1, count + 1)) and len(native) == count,
        "Native/action cohort positions or IDs differ",
    )
    for key in ("run_id", "account_id", "ghost_sha256"):
        j.require(len({r[key] for r in runs}) == count, "Duplicate production " + key)
    j.require(set(native) == {r["run_id"] for r in runs}, "Prepared/action run IDs differ")
    return runs, native


def execute(args):
    actions_path = Path(args.actions).resolve(strict=True)
    actions = j.read(actions_path)
    prepared_path = Path(args.prepared).resolve(strict=True)
    prepared = j.read(prepared_path)
    runs, native = validate_cohort(actions, prepared)
    j.evidence(prepared["actions_manifest"], prepared_path.parent)
    j.require(
        prepared["actions_manifest"]["sha256"] == j.sha(actions_path),
        "Native preparation used another action cohort",
    )
    gate_path = Path(args.gate).resolve(strict=True)
    render_root = Path(args.render_root).resolve(strict=True)
    audit_root = Path(args.audit_root).resolve()
    output = Path(args.calibrations).resolve(strict=True)
    gate = j.renderer.validate_gate(gate_path, actions, render_root)
    preserved = []
    for pilot in gate["pilot_runs"]:
        run = runs[pilot["ordinal"] - 1]
        path = output / (run["run_id"] + ".json")
        ref = j.evidence(pilot["calibration"], gate_path.parent)
        j.require(
            Path(ref["path"]) == path,
            "Original pilot calibration path differs; keep the existing pilot directory",
        )
        preserved.append(ref)
    parameters = clock_parameters(args)
    state = Path(args.state).resolve()
    state.parent.mkdir(parents=True, exist_ok=True)
    identity = dict(
        actions=j.pin(actions_path),
        prepared=j.pin(prepared_path),
        gate=j.pin(gate_path),
        render_root=str(render_root),
        audit_root=str(audit_root),
        calibrations=str(output),
        parameters=parameters,
        supervisor=j.pin(Path(__file__)),
        builder=j.pin(Path(builder.__file__)),
        original_pilot_calibrations=preserved,
        automatic_audit_subdir=args.automatic_audit_subdir,
        legacy_builder=j.pin(Path(legacy_builder.__file__)),
        previous_automatic_builder=j.pin(Path(previous_automatic_builder.__file__)),
    )
    with (output / ".production-calibration.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if state.exists():
            j.require(
                j.read(state)["source_identity"] == identity, "Calibration supervisor cohort/settings changed"
            )
        report = dict(
            schema_version=1,
            status="calibration_cohort_in_progress",
            source_identity=identity,
            original_five_calibrations_preserved=True,
            requested_count=len(runs),
            completed=[],
            waiting=[],
            deferred=[],
            failures=[],
        )

        def save():
            temporary = state.with_suffix(state.suffix + ".partial")
            temporary.write_bytes(j.encoded(report))
            temporary.replace(state)

        created = 0
        for run in runs[5:]:
            rid = run["run_id"]
            try:
                paths = input_paths(run, native[rid], render_root, audit_root, args.automatic_audit_subdir)
            except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
                report["failures"].append(dict(run_id=rid, ordinal=run["ordinal"], error=str(exc)))
                save()
                continue
            target = output / (rid + ".json")
            proof = state.parent / (state.stem + "-runs") / (rid + ".json")
            missing = [key for key, path in paths.items() if not path.is_file()]
            if missing:
                j.require(
                    not target.exists() and not proof.exists(),
                    "Completed calibration lost required source/report: " + rid,
                )
                report["waiting"].append(dict(run_id=rid, ordinal=run["ordinal"], missing=missing))
                continue
            if not proof.exists() and created >= args.max_new:
                report["deferred"].append(dict(run_id=rid, ordinal=run["ordinal"], reason="max_new reached"))
                continue
            try:
                row = process_one(run, actions, native[rid], paths, target, proof, parameters)
                report["completed"].append(row)
                if row["status"] == "calibration_created_verified":
                    created += 1
                print(json.dumps(row), flush=True)
                save()
            except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
                report["status"] = "calibration_rejected"
                report["failures"].append(dict(run_id=rid, ordinal=run["ordinal"], error=str(exc)))
                save()
                continue
        for ref in preserved:
            j.evidence(ref, output)
        complete = (
            len(report["completed"]) == len(runs) - 5
            and not report["waiting"]
            and not report["deferred"]
            and not report["failures"]
        )
        report["status"] = (
            "all_calibrations_verified"
            if complete
            else "calibration_cohort_has_quarantined_runs"
            if report["failures"]
            else "calibration_cohort_waiting_for_verified_runs"
        )
        report["total_verified_calibrations"] = 5 + len(report["completed"])
        save()
        result = dict(
            status=report["status"],
            pilot_preserved=5,
            production_verified=len(report["completed"]),
            newly_created=created,
            waiting=len(report["waiting"]),
            deferred=len(report["deferred"]),
            state=str(state),
        )
        print(json.dumps(result))
        return 2 if report["failures"] else (0 if complete or not args.require_complete else 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("actions", "prepared", "gate", "render-root", "audit-root", "calibrations", "state"):
        parser.add_argument("--" + name, required=True)
    for name in CLOCK_FIELDS:
        parser.add_argument("--" + name.replace("_", "-"))
    parser.add_argument(
        "--clock-parameters-from-source-proofs",
        action="store_true",
        help="Use each independently reverified typed source-clock proof’s nominal mapping and bound",
    )
    parser.add_argument("--automatic-audit-subdir", default="automatic-policy-v1")
    parser.add_argument("--max-new", type=int, default=2147483647)
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="Exit3 if production runs still await reviewed reports",
    )
    args = parser.parse_args()
    try:
        j.require(args.max_new > 0, "max-new must be positive")
        j.require(
            args.automatic_audit_subdir
            and Path(args.automatic_audit_subdir).name == args.automatic_audit_subdir
            and args.automatic_audit_subdir not in (".", ".."),
            "Automatic audit subdir must be one safe name",
        )
        return execute(args)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        print("Rejected: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
