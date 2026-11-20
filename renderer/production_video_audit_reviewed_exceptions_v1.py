#!/usr/bin/env python3
"""Explicit assistant image-review resolution of preserved automatic quarantines.

Reuses the frozen candidate/source/timing validators without mutating their
modules or artifacts. A separately attested interior ambiguous-frame resolution
may establish contiguous visible scene membership; no scanner output is edited.
Raw native tails stay intact and the existing measured v2 calibration intersects
visible bounds with the original race. No automatic pass or human review is claimed.
"""

from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path
import time
import production_video_audit_conditional_v2 as historical

ui = historical.ui
pilot = historical.pilot
scanner = historical.scanner
alignment = historical.alignment
HERE = Path(__file__).resolve().parent
BUILD = historical.BUILD
REVIEW_MODE = historical.REVIEW_MODE
EXCEPTION_VERSION = "1"
manifest_at = historical.manifest_at
check_gate = historical.check_gate
candidate_at = historical.candidate_at
relative_check = historical.relative_check
source_timing = historical.source_timing
timing_tools = historical.timing_tools
new_json = historical.new_json


def check_review_provenance(r, review_path):
    base = review_path.parent
    ui.require(
        r.get("reviewed_exception_version") == EXCEPTION_VERSION, "Unknown reviewed exception contract"
    )
    ui.require(
        r.get("exception_pipeline") == ui.pin(Path(__file__)), "Reviewed exception implementation changed"
    )
    draft_path = ui.ref(r["original_review_draft"], base)
    draft = ui.read(draft_path)
    for key in (
        "build",
        "ordinal",
        "run_id",
        "manifest",
        "clean_candidate",
        "clock_candidate",
        "source_clock_proof",
        "source_clock_contract",
        "timing_tools",
        "scan",
        "scanner_validation",
        "pulses",
        "correspondence",
        "review_mode",
        "required_manual_frame_indices",
        "pipeline",
        "pilot_gate",
    ):
        ui.require(r.get(key) == draft.get(key), "Original preparation changed: " + key)
    quarantine_path = ui.ref(r["original_automatic_quarantine"], base)
    quarantine = ui.read(quarantine_path)
    ui.require(
        quarantine.get("status") == "quarantined"
        and quarantine.get("schema_version") == 2
        and quarantine.get("policy_version") == "2"
        and quarantine.get("review_mode") == "deterministic_decoded_image_policy_v1"
        and quarantine.get("manual_review_performed") is False
        and quarantine.get("run_id") == r["run_id"]
        and quarantine.get("ordinal") == r["ordinal"],
        "Original automatic quarantine identity/status differs",
    )
    for left, right in (
        ("original_review_draft", "original_review_draft"),
        ("prepared_scan", "scan"),
        ("native_video", None),
        ("clean_candidate", "clean_candidate"),
        ("clock_candidate", "clock_candidate"),
        ("manifest", "manifest"),
        ("pulses", "pulses"),
        ("correspondence", "correspondence"),
    ):
        ui.ref(quarantine[left], quarantine_path.parent)
        if right:
            ui.require(quarantine[left] == r[right], "Quarantine source differs: " + left)
    issues = quarantine.get("unresolved_issues")
    allowed = {
        "marker_text_identity_unresolved",
        "unrecognized_chase_camera_scene_or_anomaly",
        "prepared_boundary_scan_ambiguous",
    }
    ui.require(
        isinstance(issues, list) and issues and set(issues) <= allowed and len(set(issues)) == len(issues),
        "Unsupported automatic quarantine requires separate investigation",
    )
    resolutions = r.get("quarantine_issue_resolutions", [])
    ui.require(
        len(resolutions) == len(issues) and {x.get("issue") for x in resolutions} == set(issues),
        "Every automatic quarantine issue needs explicit image review resolution",
    )
    for resolution in resolutions:
        ui.require(
            resolution.get("resolved") is True
            and resolution.get("reason")
            and resolution.get("evidence_files"),
            "Quarantine issue lacks actual image review evidence",
        )
        for ref in resolution["evidence_files"]:
            ui.ref(ref, base)
    report_path = ui.ref(r["reviewer_report"], base)
    report = ui.read(report_path)
    ui.require(
        report.get("status") == "actual_sampled_exception_review_complete"
        and report.get("run_id") == r["run_id"]
        and report.get("ordinal") == r["ordinal"],
        "Reviewer report identity/status differs",
    )
    ui.require(
        report.get("reviewer")
        and report.get("reviewed_at")
        and report.get("no_human_review_claimed") is True,
        "Actual assistant reviewer/time and truthful attribution required",
    )
    ui.require(
        set(r["required_manual_frame_indices"]) <= set(report.get("actual_frame_indices", [])),
        "Reviewer did not inspect all required sampled/flagged frames",
    )
    ui.require(
        report.get("observed_marker_texts") == r["marker_texts_reviewed"],
        "Actual observed marker text differs",
    )
    ui.require(report.get("evidence_files"), "Reviewer report lacks pinned observations/images")
    for ref in report["evidence_files"]:
        ui.ref(ref, report_path.parent)
    ui.require(report.get("unresolved_issues", []) == [], "Reviewer has unresolved issues")
    ui.require(
        not any(
            k in r
            for k in (
                "automatic_contiguous_race_visibility_verified",
                "automatic_review",
                "manual_review_performed",
                "policy_version",
            )
        ),
        "Reviewed exception must not masquerade as automatic acceptance",
    )
    return quarantine


def validate_boundary_resolution(r, s, scanpath, video, pts):
    """Preserve all scan decisions; admit only explicitly reviewed interior ambiguity."""
    if s["automatic_boundary_checks_passed"] and not s["failures"] and not s["ambiguous_frames"]:
        ui.require(not r.get("boundary_resolution"), "Unneeded boundary override is forbidden")
        return
    ui.require(r.get("boundary_resolution"), "Ambiguous scan requires explicit boundary resolution")
    path = ui.ref(r["boundary_resolution"], scanpath.parent.parent)
    proof = ui.read(path)
    ui.require(
        proof.get("schema_version") == 1
        and proof.get("status") == "interior_ambiguity_resolved_by_actual_image_review",
        "Unknown boundary resolution contract",
    )
    ui.require(
        proof.get("run_id") == r["run_id"]
        and proof.get("ordinal") == r["ordinal"]
        and proof.get("original_scan") == ui.pin(scanpath)
        and proof.get("native_video") == ui.pin(video),
        "Boundary resolution uses another source",
    )
    ui.require(
        proof.get("reviewer")
        and proof.get("reviewed_at")
        and proof.get("method")
        and proof.get("no_human_review_claimed") is True
        and proof.get("all_frames_manually_reviewed") is False,
        "Boundary review attribution is missing or overstates reviewed scope",
    )
    ambiguous = s["ambiguous_frames"]
    first, last = s["first_usable_frame_index"], s["last_usable_frame_index"]
    ui.require(
        ambiguous
        and set(s["failures"])
        == {"ambiguous_overview_vs_race_pixels", "race_scene_not_one_contiguous_interval"},
        "Boundary resolution cannot waive other scanner failures",
    )
    ui.require(
        proof.get("original_failures") == s["failures"] and proof.get("ambiguous_frame_indices") == ambiguous,
        "Boundary resolution must retain original failures and ambiguous identities",
    )
    ui.require(
        proof.get("first_usable_frame_index") == first and proof.get("last_usable_frame_index") == last,
        "Boundary resolution must not move measured raw bounds",
    )
    ui.require(
        all(first + 2 < i < last - 2 for i in ambiguous),
        "Only strictly interior ambiguity can be reviewed here",
    )
    race = []
    for a, b in s["race_candidate_groups"]:
        race.extend(range(a, b + 1))
    ui.require(
        set(race).isdisjoint(ambiguous) and sorted(race + ambiguous) == list(range(first, last + 1)),
        "Reviewed ambiguous frames do not close the exact candidate interval",
    )
    ui.require(
        not any(first <= i <= last for i in s["all_black_frames"]), "Black frame inside reviewed interval"
    )
    neighborhoods = sorted({j for i in ambiguous for j in range(i - 2, i + 3)})
    ui.require(
        set(neighborhoods) <= set(proof.get("actual_frame_indices", [])),
        "Every ambiguous frame and both neighbors require actual image review",
    )
    evidence = proof.get("frame_evidence", [])
    ui.require(
        {x.get("frame_index") for x in evidence} == set(neighborhoods)
        and len(evidence) == len(neighborhoods),
        "Boundary source frame evidence is incomplete or duplicated",
    )
    selected = {
        int(Path(x["path"]).stem.split("-")[-1]): {"path": x["path"], "sha256": x["sha256"]}
        for x in s["selected_frame_evidence"]
    }
    for row in evidence:
        ui.require(
            row.get("image") == selected.get(row["frame_index"]),
            "Boundary image does not match preserved scan frame",
        )
        ui.ref(row["image"], path.parent)
    resolutions = proof.get("resolutions", [])
    ui.require(
        len(resolutions) == len(ambiguous) and {x.get("frame_index") for x in resolutions} == set(ambiguous),
        "Every ambiguous frame needs an explicit resolution",
    )
    ui.require(
        all(
            x.get("disposition") == "expected_native_race_scene"
            and x.get("reason")
            and x.get("reviewed") is True
            for x in resolutions
        ),
        "Unresolved interior ambiguity",
    )
    ui.require(proof.get("unresolved_issues") == [], "Boundary review has unresolved issues")


def validate_visible_bounds(first, last, pts, timeline, run):
    start = timeline["entity"]["start_ms"]
    ui.require(
        0 <= pts[first] - start <= 40,
        "Candidate visible start exceeds the independently tested pixel-boundary limit",
    )
    ui.require(
        pts[last] <= timeline["camera"]["end_ms"], "Measured visibility exceeds native camera coverage"
    )
    ui.require(
        last + 1 < len(pts)
        and 0 <= run["record_time_ms"] - min(run["record_time_ms"], pts[last + 1] - start) <= 40,
        "Candidate visible finish exceeds the tested missing-suffix tolerance",
    )
    # A native visible tail is evidence, not a source control extension. The
    # unchanged v2 measured builder clips PTS to [0, source finish) for every
    # reviewed or automatic report; unknown controls remain unknown.


def validate_review(review_path, accepted):
    r = ui.read(review_path)
    base = review_path.parent
    quarantine = check_review_provenance(r, review_path)
    ui.require(r["build"] == BUILD and r["review_mode"] == REVIEW_MODE, "Review uses another policy")
    ui.require(
        r.get("verified_by") and r.get("verified_utc") and r.get("method"),
        "Explicit reviewer/time/method required",
    )
    ui.require(
        r["pipeline"]["sha256"] == ui.common.sha256(Path(historical.__file__)),
        "Audit implementation changed after preparation",
    )
    for k in (
        "settings_reviewed",
        "completion_reviewed",
        "scene_unchanged_until_render",
        "sampled_continuity_reviewed",
    ):
        ui.require(r.get(k) is True, "Actual review required: " + k)
    ui.require(r["settings_observed"] == ui.SETTINGS, "Actual visible export settings differ")
    manifestpath = ui.ref(r["manifest"], base)
    manifest = manifest_at(manifestpath)
    run = next(j for j in manifest["runs"] if j["ordinal"] == r["ordinal"])
    ui.require(run["run_id"] == r["run_id"], "Review run differs")
    gate = ui.ref(r["pilot_gate"], base) if r.get("pilot_gate") else None
    check_gate(manifest, run, gate, accepted)
    candidates = {
        role: candidate_at(ui.ref(r[role + "_candidate"], base), manifest, run, role)
        for role in ("clean", "clock")
    }
    scanpath = ui.ref(r["scan"], base)
    s = ui.read(scanpath)
    _, cr, ct = candidates["clean"]
    ui.require(quarantine["native_video"] == ui.pin(cr["video"]), "Quarantine refers to another native video")
    ui.require(
        s["script"]["sha256"] == ui.common.sha256(HERE / "scan_native_video_partial_v1.py")
        and s["policy"] == scanner.POLICY,
        "Scanner source/policy differs",
    )
    for key, ref in (
        ("video", cr["video"]),
        ("native_clip", cr["readback"]),
        ("native_inspection", cr["readback_inspection"]),
        ("action_metadata", cr["action_metadata"]),
    ):
        ui.require(ui.ref(s[key], scanpath.parent) == ref, "Scan belongs to another source: " + key)
    ui.require(
        s["run_id"] == run["run_id"] and s["map_sha256"] == manifest["map"]["sha256"],
        "Scan source identity differs",
    )
    ui.require(
        r["manual_frame_indices"] == s["required_manual_frame_indices"] == r["required_manual_frame_indices"],
        "Every selected and flagged neighborhood requires actual visual review",
    )
    for ref in s["selected_frame_evidence"] + s["review_sheets"]:
        ui.ref(ref, scanpath.parent)
    metrics = list(csv.DictReader(ui.ref(s["all_frame_metrics"], scanpath.parent).open()))
    ui.require(len(metrics) == s["all_frames_scanned"], "Scan frame count differs")
    pts = [float(x["pts_ms"]) for x in metrics]
    derived = scanner.classify(
        pts,
        [float(x["overview_roi_mae"]) for x in metrics],
        [float(x["previous_roi_mae"]) for x in metrics],
        s["all_black_frames"],
        ct["entity"]["start_ms"],
        run["record_time_ms"],
    )
    ui.require(
        all(derived[k] == s[k] for k in derived), "Stored scanner decision differs from all-frame metrics"
    )
    first, last = s["first_usable_frame_index"], s["last_usable_frame_index"]
    validate_boundary_resolution(r, s, scanpath, cr["video"], pts)
    validate_visible_bounds(first, last, pts, ct, run)
    resolutions = r["flag_resolutions"]
    ui.require(
        len(resolutions) == len(s["flagged_frames_require_review"]),
        "Every anomaly needs explicit review resolution",
    )
    ui.require(
        {v["frame_index"] for v in resolutions} == set(s["flagged_frames_require_review"]),
        "Flag resolution identities differ",
    )
    ui.require(
        all(
            v.get("reviewed") is True
            and v.get("disposition") == "expected_native_scene_content"
            and v.get("reason")
            for v in resolutions
        ),
        "Unresolved visual anomaly",
    )
    validation = ui.read(ui.ref(r["scanner_validation"], base))
    ui.require(
        validation["passed"] and validation["scanner"]["sha256"] == s["script"]["sha256"],
        "Scanner lacks pilot calibration",
    )
    pulsepath = ui.ref(r["pulses"], base)
    pulse = ui.read(pulsepath)
    ui.require(
        pulse["clean_native_video"]["sha256"] == ui.common.sha256(cr["video"])
        and pulse["diagnostic_video"]["sha256"] == ui.common.sha256(candidates["clock"][1]["video"])
        and pulse["native_readback_inspection"]["sha256"]
        == ui.common.sha256(candidates["clock"][1]["readback_inspection"]),
        "Pulse detection belongs to another scene/video",
    )
    ui.require(
        r["marker_texts_reviewed"] == [p["native_text"] for p in pulse["pulses"]],
        "Actual diagnostic text identity review required",
    )
    ui.ref(pulse["detection"]["review_sheet"], pulsepath.parent)
    ui.ref(pulse["detection"]["all_frame_scores"], pulsepath.parent)
    for p in pulse["pulses"]:
        for ref in p["evidence_files"]:
            ui.ref(ref, pulsepath.parent)
    relative_check(ui.ref(r["correspondence"], base), cr["video"], candidates["clock"][1]["video"])
    source_proof = ui.ref(r["source_clock_proof"], base)
    ui.require(r.get("timing_tools") == timing_tools(), "Source timing validation tools changed")
    ui.require(
        r.get("source_clock_contract") == source_timing(source_proof, cr["action_metadata"]),
        "Source timing classification differs from recomputed evidence",
    )
    return r, manifest, run, candidates, s, pts, pulse


def evidence_for(r, run, manifest, candidate, review_path, role):
    value = historical.evidence_for(r, run, manifest, candidate, review_path, role)
    value.update(
        reviewed_exception_version=EXCEPTION_VERSION,
        original_automatic_quarantine=r["original_automatic_quarantine"],
        reviewer_report=r["reviewer_report"],
        exception_pipeline=ui.pin(Path(__file__)),
        review_actor_type="assistant",
        no_human_review_claimed=True,
    )
    return value


def accept(args):
    r, m, run, candidates, s, pts, pulse = validate_review(args.review, args.accepted)
    out = args.review.parent
    started = time.monotonic()
    receipt = out / "acceptance-receipt.json"
    if receipt.exists():
        prior = ui.read(receipt)
        ui.require(prior["review"] == ui.pin(args.review), "Resume review differs")
        ui.require(prior.get("pipeline") == ui.pin(Path(__file__)), "Acceptance implementation changed")
        for item in prior["evidence_files"]:
            ui.ref(item, out)
        folder = args.accepted / run["run_id"]
        record = ui.read(folder / "result.json")
        ui.common.validate_render_result(record, m, run, folder)
        print(json.dumps({"status": "skipped_verified", "receipt": str(receipt)}))
        return
    ui.require(
        not (args.accepted / run["run_id"] / "result.json").exists(),
        "Existing unreceipted acceptance requires explicit recovery, never overwrite",
    )
    cleanpath = out / "clean-export-evidence.json"
    clockpath = out / "diagnostic-export-evidence.json"
    for role, path in (("clean", cleanpath), ("clock", clockpath)):
        value = evidence_for(r, run, m, candidates[role], args.review, role)
        if path.exists():
            ui.require(ui.read(path) == value, "Existing acceptance evidence differs")
        else:
            new_json(path, value)
        ui.validate_evidence(path, m, run, "production" if role == "clean" else "diagnostic")
    result = ui.accept(m, run, args.accepted, cleanpath)
    folder = args.accepted / run["run_id"]
    pilot.decode_equality(candidates["clean"][1]["video"], folder / "video.webm", out / "decode")
    before = {p.name: (ui.common.sha256(p), p.stat().st_mtime_ns) for p in folder.iterdir() if p.is_file()}
    resumed = ui.accept(m, run, args.accepted, cleanpath)
    after = {p.name: (ui.common.sha256(p), p.stat().st_mtime_ns) for p in folder.iterdir() if p.is_file()}
    ui.require(before == after and resumed["status"] == "skipped_verified", "Resume changed accepted files")
    first, last = s["first_usable_frame_index"], s["last_usable_frame_index"]
    start = s["timeline"]["entity"]["start_ms"]
    obs = {
        "schema_version": 1,
        "status": "contiguous_native_race_scene_reviewed",
        "review_mode": REVIEW_MODE,
        "verified_by": r["verified_by"],
        "verified_utc": r["verified_utc"],
        "native_video": candidates["clean"][0]["video"],
        "accepted_video": ui.pin(folder / "video.webm"),
        "accepted_result": ui.pin(folder / "result.json"),
        "first_usable_frame_index": first,
        "last_usable_frame_index": last,
        "first_usable_video_pts_ms": pts[first],
        "last_usable_video_pts_ms": pts[last],
        "first_usable_nominal_race_ms": pts[first] - start,
        "last_usable_nominal_race_ms": pts[last] - start,
        "frames_scanned": s["all_frames_scanned"],
        "manual_frame_indices": r["manual_frame_indices"],
        "frames_reviewed": len(r["manual_frame_indices"]),
        "reviewed_contiguous_race_visibility": True,
        "method": r["method"],
        "natural_occlusions": r["natural_occlusions"],
        "raw_frames_and_actions_retained": True,
        "terminal_transition_fabricated": False,
        "evidence_files": [ui.pin(args.review), r["scan"], r["scanner_validation"]],
        "source_clock_contract": r["source_clock_contract"],
        "calibration_status": "Separate measured video/clip calibration and explicit direct or conditional source timing required",
    }
    obs.update(
        reviewed_exception_version=EXCEPTION_VERSION,
        review_actor_type="assistant",
        no_human_review_claimed=True,
        original_automatic_quarantine=r["original_automatic_quarantine"],
        reviewer_report=r["reviewer_report"],
        exception_pipeline=ui.pin(Path(__file__)),
        all_frames_manually_reviewed=False,
        full_native_visibility_preserved=True,
        visible_nominal_tail_past_finish_ms=max(0, pts[last] - start - run["record_time_ms"]),
        training_window_requires_measured_clock_intersection=True,
    )
    if r.get("boundary_resolution"):
        obs["boundary_resolution"] = r["boundary_resolution"]
    if ui.common.exception_fact(run) is not None:
        obs["source_admission"] = ui.source_admission(run, candidates["clean"][1]["action_metadata"])
    new_json(out / "observation-window-verification.json", obs)
    c, refs, _ = candidates["clock"]
    pulse.update(
        {
            "schema_version": 1,
            "status": "native_clip_pulse_timing_verified_at_20ms_sampling_precision",
            "ordinal": run["ordinal"],
            "run_id": run["run_id"],
            "verified_by": r["verified_by"],
            "verified_utc": r["verified_utc"],
            "accepted_clean_result": ui.pin(folder / "result.json"),
            "accepted_clean_video": ui.pin(folder / "video.webm"),
            "diagnostic_video": ui.pin(refs["video"]),
            "clean_native_video": candidates["clean"][0]["video"],
            "native_readback_clip": ui.pin(refs["readback"]),
            "native_readback_inspection": ui.pin(refs["readback_inspection"]),
            "native_readback_comparison": ui.pin(refs["readback_proof"]),
            "diagnostic_export_evidence": ui.pin(clockpath),
            "source_record_to_race_evidence": r["source_clock_proof"],
            "source_clock_contract": r["source_clock_contract"],
            "timing_tools": r["timing_tools"],
            "clean_diagnostic_correspondence": r["correspondence"],
            "native_accepted_decode_equality": ui.pin(out / "decode/native-accepted-decode-equality.json"),
            "all_native_frames_preserved": True,
            "video_to_clip_nominal_mapping": {
                "video_pts_anchor_ms": 0,
                "clip_time_anchor_ms": 0,
                "clip_ms_per_video_ms": 1,
                "max_absolute_error_ms": 20,
            },
            "mapping_precision_limits": [
                "Event onset is quantized0..20ms; no global20ms offset inferred.",
                (
                    "Source control reflection supports a conditional30ms composed assumption; the unconditional physical-state error bound is unknown and strict action-aligned eligibility is false."
                    if r["source_clock_contract"]["evidence_class"] == alignment.CONDITIONAL
                    else "The three exact source checkpoint events corroborate the10ms record/race contract; composition with20ms native pulse sampling gives the declared30ms contract. No uniform frame exposure guarantee is asserted."
                ),
            ],
        }
    )
    new_json(out / "pulse-verification.json", pulse)
    new_json(
        out / "resume-proof.json",
        {
            "schema_version": 1,
            "passed": True,
            "accepted_files_hash_and_mtime_unchanged": True,
            "first_accept": result,
            "resume": resumed,
            "files": before,
        },
    )
    new_json(
        receipt,
        {
            "schema_version": 1,
            "status": "accepted_calibration_join_still_required",
            "review": ui.pin(args.review),
            "source_clock_contract": r["source_clock_contract"],
            "reviewed_exception_version": EXCEPTION_VERSION,
            "run_id": run["run_id"],
            "ordinal": run["ordinal"],
            "review_actor_type": "assistant",
            "no_human_review_claimed": True,
            "original_automatic_quarantine": r["original_automatic_quarantine"],
            "reviewer_report": r["reviewer_report"],
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
        },
    )
    print(
        json.dumps(
            {"status": "accepted_calibration_join_still_required", "result": str(folder / "result.json")}
        )
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("validate", "accept"))
    p.add_argument("review", type=Path)
    p.add_argument("--accepted", type=Path, required=True)
    args = p.parse_args()
    if args.command == "validate":
        r, _, run, _, _, _, _ = validate_review(args.review, args.accepted)
        print(
            json.dumps(
                {
                    "status": "reviewed_exception_validation_passed",
                    "run_id": run["run_id"],
                    "ordinal": run["ordinal"],
                    "accepted_files_written": False,
                    "original_automatic_quarantine": r["original_automatic_quarantine"],
                }
            )
        )
    else:
        accept(args)


if __name__ == "__main__":
    main()
