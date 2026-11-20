#!/usr/bin/env python3
"""Accept only validated deterministic QA; quarantine exceptions without GUI work.

The supervisor owns the single-decoder scheduling gate. Historical preparation,
reviews, results, videos and action streams are never rewritten by this version.
"""

from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
import sys
import time
import automatic_video_policy_v1 as policy
import production_video_audit_conditional_v2 as historical

ui = historical.ui
BUILD = "native-production-automatic-acceptance-1"


def export_evidence(record, run, manifest, candidate, automatic_path, role):
    # The frozen source/media validator accepts identified machine observers.
    # It does not require a human. Explicit historical review is never asserted.
    value = historical.evidence_for(record, run, manifest, candidate, automatic_path, role)
    value.pop("explicit_review", None)
    value.update(
        review_mode=policy.MODE,
        automatic_review=ui.pin(automatic_path),
        manual_review_performed=False,
        verification_method=policy.MODE,
    )
    for key in ("settings_observation", "scene_observation", "completion_observation"):
        value[key]["verification_method"] = policy.MODE
        value[key]["manual_review_performed"] = False
        value[key]["evidence_files"].append(ui.pin(automatic_path))
    value["settings_observation"]["settings_equivalence"] = record["settings_equivalence"]
    value["completion_observation"]["completion_equivalence"] = record["completion_equivalence"]
    return value


def verify_receipt(path, automatic_path, accepted):
    receipt = ui.read(path)
    r = policy.verify_record(automatic_path)
    ui.require(
        receipt.get("schema_version") == 2
        and receipt.get("review_mode") == policy.MODE
        and receipt.get("status") == "accepted_calibration_join_still_required"
        and receipt.get("run_id") == r["run_id"]
        and receipt.get("ordinal") == r["ordinal"]
        and receipt.get("automatic_review") == ui.pin(automatic_path),
        "Acceptance resume identity differs",
    )
    ui.require(receipt["pipeline"] == ui.pin(Path(__file__)), "Automatic acceptance code changed")
    for item in receipt["evidence_files"]:
        ui.ref(item, path.parent)
    folder = accepted / r["run_id"]
    ui.require(receipt["accepted_result"] == ui.pin(folder / "result.json"), "Accepted result changed")
    manifest = historical.manifest_at(ui.ref(r["manifest"], automatic_path.parent))
    run = next(x for x in manifest["runs"] if x["ordinal"] == r["ordinal"])
    ui.common.validate_render_result(ui.read(folder / "result.json"), manifest, run, folder)
    return receipt


def accept_record(automatic_path, accepted):
    r = policy.verify_record(automatic_path)
    out = automatic_path.parent
    receiptpath = out / "acceptance-receipt.json"
    if receiptpath.exists():
        return verify_receipt(receiptpath, automatic_path, accepted)
    folder = accepted / r["run_id"]
    ui.require(
        not (folder / "result.json").exists(),
        "Unreceipted or historical accepted result exists; reconcile without overwrite",
    )
    manifest = historical.manifest_at(ui.ref(r["manifest"], out))
    run = next(x for x in manifest["runs"] if x["ordinal"] == r["ordinal"])
    draft = ui.read(ui.ref(r["original_review_draft"], out))
    historical.check_gate(
        manifest, run, ui.ref(draft["pilot_gate"], out) if draft.get("pilot_gate") else None, accepted
    )
    candidates = {
        role: historical.candidate_at(ui.ref(r[role + "_candidate"], out), manifest, run, role)
        for role in ("clean", "clock")
    }
    started = time.monotonic()
    for role, name in [("clean", "clean-export-evidence.json"), ("clock", "diagnostic-export-evidence.json")]:
        path = out / name
        value = export_evidence(r, run, manifest, candidates[role], automatic_path, role)
        policy.new_json(path, value)
        ui.validate_evidence(path, manifest, run, "production" if role == "clean" else "diagnostic")
    result = ui.accept(manifest, run, accepted, out / "clean-export-evidence.json")
    historical.pilot.decode_equality(candidates["clean"][1]["video"], folder / "video.webm", out / "decode")
    before = {p.name: [ui.common.sha256(p), p.stat().st_mtime_ns] for p in folder.iterdir() if p.is_file()}
    resumed = ui.accept(manifest, run, accepted, out / "clean-export-evidence.json")
    after = {p.name: [ui.common.sha256(p), p.stat().st_mtime_ns] for p in folder.iterdir() if p.is_file()}
    ui.require(
        before == after and resumed["status"] == "skipped_verified",
        "Automatic acceptance resume modified immutable artifacts",
    )
    first, last = r["visibility"]["first_usable_frame_index"], r["visibility"]["last_usable_frame_index"]
    metrics = ui.read(ui.ref(r["decoded_diagnostics"], out))
    start = metrics["entity_start_ms"]
    equality = ui.pin(out / "decode/native-accepted-decode-equality.json")
    observation = {
        "schema_version": 2,
        "status": "contiguous_native_race_scene_automatically_verified",
        "review_mode": policy.MODE,
        "automatic_contiguous_race_visibility_verified": True,
        "manual_review_performed": False,
        "automatic_review": ui.pin(automatic_path),
        "verified_by": r["verified_by"],
        "verified_utc": r["verified_utc"],
        "ordinal": r["ordinal"],
        "run_id": r["run_id"],
        "native_video": r["native_video"],
        "accepted_video": ui.pin(folder / "video.webm"),
        "accepted_result": ui.pin(folder / "result.json"),
        "native_accepted_decode_equality": equality,
        **r["visibility"],
        "first_usable_nominal_race_ms": metrics["pts"][first] - start,
        "last_usable_nominal_race_ms": metrics["pts"][last] - start,
        "frames_scanned": len(metrics["pts"]),
        "method": "Deterministic decoded-frame scene matching, freeze/black/decode checks, clean/clock correspondence, OCR timing identity and pinned settings/completion equivalence.",
        "natural_occlusions": "Only images admitted by independently reviewed fixed-scene reference bank; unrecognized anomalies quarantine.",
        "raw_frames_and_actions_retained": True,
        "terminal_transition_fabricated": False,
        "full_native_visibility_preserved": True,
        "visible_nominal_tail_past_finish_ms": r["source_finish_intersection"][
            "raw_postfinish_tail_preserved_ms"
        ],
        "training_window_requires_measured_clock_intersection": True,
        "source_clock_contract": r["source_clock_contract"],
        "source_finish_intersection": r["source_finish_intersection"],
        "evidence_files": [ui.pin(automatic_path), r["prepared_scan"], r["reference"], equality],
        "calibration_status": "Separate measured source/clip calibration and source-finish/known-control intersection required",
    }
    if ui.common.exception_fact(run) is not None:
        observation["source_admission"] = ui.source_admission(run, candidates["clean"][1]["action_metadata"])
    policy.new_json(out / "observation-window-verification.json", observation)
    pulse = copy.deepcopy(ui.read(ui.ref(r["pulses"], out)))
    c, refs, timeline = candidates["clock"]
    pulse.update(
        {
            "schema_version": 2,
            "status": "native_clip_pulse_timing_verified_at_20ms_sampling_precision",
            "review_mode": policy.MODE,
            "manual_review_performed": False,
            "automatic_review": ui.pin(automatic_path),
            "marker_identity": r["marker_identity"],
            "ordinal": r["ordinal"],
            "run_id": r["run_id"],
            "verified_by": r["verified_by"],
            "verified_utc": r["verified_utc"],
            "accepted_clean_result": ui.pin(folder / "result.json"),
            "accepted_clean_video": ui.pin(folder / "video.webm"),
            "diagnostic_video": ui.pin(refs["video"]),
            "clean_native_video": r["native_video"],
            "native_readback_clip": ui.pin(refs["readback"]),
            "native_readback_inspection": ui.pin(refs["readback_inspection"]),
            "native_readback_comparison": ui.pin(refs["readback_proof"]),
            "diagnostic_export_evidence": ui.pin(out / "diagnostic-export-evidence.json"),
            "source_record_to_race_evidence": r["source_clock_proof"],
            "source_clock_contract": r["source_clock_contract"],
            "timing_tools": r["timing_tools"],
            "clean_diagnostic_correspondence": r["correspondence"],
            "native_accepted_decode_equality": equality,
            "all_native_frames_preserved": True,
            "video_to_clip_nominal_mapping": {
                "video_pts_anchor_ms": 0,
                "clip_time_anchor_ms": 0,
                "clip_ms_per_video_ms": 1,
                "max_absolute_error_ms": 20,
            },
            "mapping_precision_limits": [
                "Native event onset is quantized0..20ms; no global20ms offset inferred.",
                "Conditional sources retain null unconditional bounds and false strict action-alignment eligibility.",
                "Raw visible tails are retained; usable training is independently intersected with source finish and known controls.",
            ],
        }
    )
    policy.new_json(out / "pulse-verification.json", pulse)
    policy.new_json(
        out / "resume-proof.json",
        {
            "schema_version": 2,
            "passed": True,
            "accepted_files_hash_and_mtime_unchanged": True,
            "first_accept": result,
            "resume": resumed,
            "files": before,
        },
    )
    receipt = {
        "schema_version": 2,
        "status": "accepted_calibration_join_still_required",
        "review_mode": policy.MODE,
        "run_id": r["run_id"],
        "ordinal": r["ordinal"],
        "automatic_review": ui.pin(automatic_path),
        "accepted_result": ui.pin(folder / "result.json"),
        "accepted_video": ui.pin(folder / "video.webm"),
        "manual_review_performed": False,
        "source_clock_contract": r["source_clock_contract"],
        "elapsed_accept_seconds": time.monotonic() - started,
        "pipeline": ui.pin(Path(__file__)),
        "evidence_files": [
            ui.pin(q)
            for q in (
                out / "observation-window-verification.json",
                out / "pulse-verification.json",
                out / "resume-proof.json",
                out / "decode/native-accepted-decode-equality.json",
                folder / "result.json",
                folder / "video.webm",
            )
        ],
    }
    policy.new_json(receiptpath, receipt)
    return receipt


def run(draft, reference, accepted, output):
    if (output / "acceptance-receipt.json").exists():
        return verify_receipt(output / "acceptance-receipt.json", output / "automatic-review.json", accepted)
    ui.require(
        not output.exists(),
        "Uncommitted automatic output exists; quarantine/reconcile with a fresh version, never overwrite",
    )
    r = policy.evaluate(draft, reference, output, accepted)
    if r["status"] != "automatic_checks_passed":
        return r
    try:
        return accept_record(output / "automatic-review.json", accepted)
    except Exception as exc:
        failure = {
            "schema_version": 2,
            "status": "quarantined",
            "run_id": r["run_id"],
            "ordinal": r["ordinal"],
            "automatic_review": ui.pin(output / "automatic-review.json"),
            "manual_review_performed": False,
            "unresolved_issues": [type(exc).__name__ + ": " + str(exc)],
            "accepted_files_require_reconciliation": True,
        }
        policy.new_json(output / "acceptance-quarantine.json", failure)
        return failure


def main():
    p = argparse.ArgumentParser(description=__doc__)
    s = p.add_subparsers(dest="command", required=True)
    a = s.add_parser("run")
    a.add_argument("draft", type=Path)
    for name in ("reference", "accepted", "output"):
        a.add_argument("--" + name, type=Path, required=True)
    a = s.add_parser("accept")
    a.add_argument("automatic_review", type=Path)
    a.add_argument("--accepted", type=Path, required=True)
    a = p.parse_args()
    r = (
        run(a.draft, a.reference, a.accepted, a.output)
        if a.command == "run"
        else accept_record(a.automatic_review, a.accepted)
    )
    print(
        json.dumps(
            {
                "status": r["status"],
                "ordinal": r.get("ordinal"),
                "run_id": r.get("run_id"),
                "unresolved_issues": r.get("unresolved_issues", []),
            }
        )
    )
    raise SystemExit(0 if r["status"] == "accepted_calibration_join_still_required" else 2)


if __name__ == "__main__":
    main()
