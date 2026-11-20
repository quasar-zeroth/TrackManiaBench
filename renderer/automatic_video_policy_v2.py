#!/usr/bin/env python3
"""V2 deterministic image QA with independently inferred native glyph fallback.

V1 source, evidence and scene checks are immutable. Glyph labels are inferred
without an expected-string argument; strong disagreements always quarantine.
"""

from __future__ import annotations
import argparse
from collections import Counter
import copy
import csv
import importlib
import json
from pathlib import Path
import re
import numpy as np
import automatic_video_policy_v1 as v1

historical = v1.historical
settings = v1.settings
ui = v1.ui
ROOT = v1.ROOT
MODE = v1.MODE
BUILD = "automatic-native-image-policy-2"
POLICY_VERSION = "2"
MARKER_POLICY = {
    "primary": "unchanged_v1_exact_ocr_consensus",
    "fallback": "expected_independent_native_glyph_decoder_v1",
    "strong_ocr_min_votes": 3,
    "strong_ocr_min_transform_families": 2,
    "strong_disagreement": "quarantine",
    "confident_glyph_wrong_expected": "quarantine",
    "transform_families": ["threshold", "diff", "alpha"],
    "families_are_correlated_transforms_not_statistical_independence": True,
}
POLICY = {**v1.POLICY, "marker_identity_v2": MARKER_POLICY}
new_json = v1.new_json
decode_features = v1.decode_features
scene_scores = v1.scene_scores
image_decision = v1.image_decision
completion_pixels = v1.completion_pixels
verify_camera = v1.verify_camera
WORLD = v1.WORLD


def glyph_module():
    return importlib.import_module("marker_glyph_decoder_v1")


def tools_identity():
    return {
        **v1.tools_identity(),
        "policy": ui.pin(Path(__file__)),
        "inherited_v1_policy": ui.pin(Path(v1.__file__)),
        "glyph_decoder": ui.pin(ROOT / "renderer/marker_glyph_decoder_v1.py"),
    }


def ocr_inference(texts):
    """Select all candidate labels before consulting the expected native label."""
    ui.require(len(texts) == 9, "V2 requires exact nine ordered OCR preprocessing views")
    normalized = [v1.normalize_text(x) for x in texts]
    valid = [x for x in normalized if re.fullmatch(r"clip\d+ms", x)]
    counts = Counter(valid)
    ranked = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    candidate = ranked[0][0] if ranked else None
    unique = bool(ranked and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]))
    votes = counts.get(candidate, 0)
    families = (
        sorted(
            {MARKER_POLICY["transform_families"][i // 3] for i, x in enumerate(normalized) if x == candidate}
        )
        if candidate
        else []
    )
    legacy = bool(
        unique
        and votes >= v1.POLICY["ocr_min_exact_votes"]
        and votes / max(1, len(valid)) >= v1.POLICY["ocr_exact_fraction_of_parseable_min"]
    )
    strong = bool(
        legacy
        and votes >= MARKER_POLICY["strong_ocr_min_votes"]
        and len(families) >= MARKER_POLICY["strong_ocr_min_transform_families"]
    )
    return {
        "candidate_normalized": candidate,
        "unique_plurality": unique,
        "counts": dict(sorted(counts.items())),
        "parseable_votes": len(valid),
        "candidate_votes": votes,
        "candidate_transform_families": families,
        "legacy_consensus": legacy,
        "strong_consensus": strong,
        "families_are_correlated_transforms_not_statistical_independence": True,
    }


def reconcile_marker(expected, texts, glyph):
    ocr = ocr_inference(texts)
    target = v1.normalize_text(expected)
    ui.require(re.fullmatch(r"clip\d+ms", target), "Invalid serialized native marker label")
    confident = glyph.get("confident") is True
    actual = glyph.get("decoded_text")
    ui.require(not confident or isinstance(actual, str), "Confident glyph result lacks decoded label")
    label = v1.normalize_text(actual) if confident else None
    primary = bool(ocr["legacy_consensus"] and ocr["candidate_normalized"] == target)
    contradiction = bool(confident and ocr["strong_consensus"] and ocr["candidate_normalized"] != label)
    if contradiction:
        accepted = False
        reason = "strong_ocr_glyph_disagreement"
    elif confident and label != target:
        accepted = False
        reason = "confident_glyph_contradicts_native_label"
    elif primary:
        accepted = True
        reason = "original_ocr_primary"
    elif confident and label == target:
        accepted = True
        reason = "confident_independent_glyph_fallback"
    else:
        accepted = False
        reason = "marker_identity_ambiguous"
    return {
        "accepted": accepted,
        "reason": reason,
        "ocr": ocr,
        "original_ocr_primary_passed": primary,
        "glyph_confident": confident,
        "glyph_decoded_normalized": label,
        "strong_method_contradiction": contradiction,
        "expected_normalized": target,
    }


def marker_checks(clean, clock, pts, pulse, output, glyph_bank_path):
    reports = v1.marker_checks(clean, clock, pts, pulse, output)
    glyph = glyph_module()
    bank = glyph.load_bank(glyph_bank_path)
    for m in reports:
        inferred = glyph.infer(
            clean,
            clock,
            m["first_visible_frame"],
            m["last_visible_frame"],
            bank,
            output=output,
            phase=m["phase"],
        )
        decision = reconcile_marker(m["expected_text"], m["ocr_texts"], inferred)
        m.update(
            glyph_inference=inferred,
            marker_decision=decision,
            exact_text_verified=decision["accepted"],
            policy_version=POLICY_VERSION,
        )
    return reports


def validate_reference(path):
    r = ui.read(path)
    ui.require(
        r.get("schema_version") == 2
        and r.get("policy_version") == POLICY_VERSION
        and r.get("status") == "reference_validated"
        and r.get("policy") == POLICY,
        "V2 reference did not pass validation",
    )
    ui.require(r["tools"] == tools_identity(), "V2 implementation or dependencies changed")
    scene, bank = v1.validate_reference(ui.ref(r["scene_reference"], path.parent))
    ui.require(r.get("map") == scene["map"], "V2 reference map differs from scene evidence")
    glyph_reference = ui.ref(r["glyph_reference"], path.parent)
    ui.require(
        ui.read(glyph_reference).get("status") == "supplemental_glyph_bank_validated",
        "V2 requires validated glyph reference, not an experimental bank",
    )
    glyph_module().load_bank(glyph_reference)
    validation = ui.read(ui.ref(r["marker_validation"], path.parent))
    ui.require(
        validation.get("status") == "joint_marker_policy_validated"
        and validation.get("policy_tools") == tools_identity()
        and validation.get("actual_marker_cases", 0) >= 6
        and validation.get("accepted_correct_labels") == validation.get("actual_marker_cases")
        and validation.get("negative_controls_passed") is True,
        "V2 lacks complete actual joint marker regression and negatives",
    )
    return r, bank


def evaluate(draft_path, reference_path, output, accepted):
    """Only a new artifact is written; the original preparation remains pinned."""
    ui.require(not output.exists(), "Automatic policy output already exists")
    output.mkdir(parents=True)
    draft_path = draft_path.resolve()
    r = ui.read(draft_path)
    issues = []
    report = {
        "schema_version": 2,
        "review_mode": MODE,
        "build": BUILD,
        "policy_version": POLICY_VERSION,
        "status": "quarantined",
        "ordinal": r["ordinal"],
        "run_id": r["run_id"],
        "manual_review_performed": False,
        "verified_by": BUILD,
        "verified_utc": historical.now(),
        "original_review_draft": ui.pin(draft_path),
        "original_prepare_pipeline": r["pipeline"],
        "policy_migration": {
            "from_build": r["build"],
            "to_build": BUILD,
            "preserves_original_preparation_identity": True,
            "authorization": "Explicit evaluation command requested deterministic QA and exception quarantine.",
        },
        "reference": ui.pin(reference_path),
        "tools": tools_identity(),
        "policy": POLICY,
        "clean_candidate": r["clean_candidate"],
        "clock_candidate": r["clock_candidate"],
        "source_clock_proof": r["source_clock_proof"],
        "source_clock_contract": r["source_clock_contract"],
        "timing_tools": r["timing_tools"],
        "unresolved_issues": [],
        "evidence_files": [],
    }
    try:
        ref, bank = validate_reference(reference_path)
        ui.require(
            r["build"] == historical.BUILD and r["pipeline"] == ui.pin(Path(historical.__file__)),
            "Unsupported original preparation identity",
        )
        ui.ref(r["pipeline"], draft_path.parent)
        manifest = historical.manifest_at(ui.ref(r["manifest"], draft_path.parent))
        run = next(x for x in manifest["runs"] if x["ordinal"] == r["ordinal"])
        ui.require(run["run_id"] == r["run_id"], "Draft cohort identity changed")
        ui.require(
            all(ref["map"][k] == manifest["map"][k] for k in ("uid", "sha256")),
            "Automatic reference belongs to another map",
        )
        historical.check_gate(
            manifest,
            run,
            ui.ref(r["pilot_gate"], draft_path.parent) if r.get("pilot_gate") else None,
            accepted,
        )
        candidates = {
            role: historical.candidate_at(
                ui.ref(r[role + "_candidate"], draft_path.parent), manifest, run, role
            )
            for role in ("clean", "clock")
        }
        c, cr, ct = candidates["clean"]
        d, dr, dt = candidates["clock"]
        for timeline in (ct, dt):
            verify_camera(timeline)
        ui.require(
            all(cr[k].read_bytes() == dr[k].read_bytes() for k in historical.SOURCE_KEYS),
            "Clean/clock original source differs",
        )
        ui.require(
            r["source_clock_contract"]
            == historical.source_timing(
                ui.ref(r["source_clock_proof"], draft_path.parent), cr["action_metadata"]
            ),
            "Source timing contract changed",
        )
        report["native_video"] = ui.pin(cr["video"])
        report["manifest"] = r["manifest"]
        report["candidates_source_evidence"] = {
            role: {k: ui.pin(v) for k, v in candidate[1].items()} for role, candidate in candidates.items()
        }
        settings_results = {
            role: settings.verify(ui.ref(r[role + "_candidate"], draft_path.parent))
            for role in ("clean", "clock")
        }
        new_json(output / "settings-equivalence.json", settings_results)
        report["evidence_files"].append(ui.pin(output / "settings-equivalence.json"))
        if not all(v["passed"] for v in settings_results.values()):
            issues.append("settings_pixel_mismatch")
        completion_results = {}
        for role, refs in [("clean", cr), ("clock", dr)]:
            pixel = completion_pixels(refs["completion_observation"])
            delta = np.abs(bank["completion"].astype(np.int16) - pixel.astype(np.int16))
            difference = int(delta.max(axis=1).min())
            completion_results[role] = {
                "maximum_channel_difference": difference,
                "passed": difference <= 1,
                "image": ui.pin(refs["completion_observation"]),
            }
        new_json(output / "completion-equivalence.json", completion_results)
        report["evidence_files"].append(ui.pin(output / "completion-equivalence.json"))
        if not all(v["passed"] for v in completion_results.values()):
            issues.append("completion_editor_controls_mismatch")
        scanpath = ui.ref(r["scan"], draft_path.parent)
        s = ui.read(scanpath)
        ui.require(
            s["video"]["sha256"] == ui.common.sha256(cr["video"]) and s["run_id"] == run["run_id"],
            "Boundary scan video identity changed",
        )
        ui.require(
            s["script"] == ui.pin(Path(historical.scanner.__file__))
            and s["policy"] == historical.scanner.POLICY,
            "Prepared scan uses unsupported implementation",
        )
        rows = list(csv.DictReader(ui.ref(s["all_frame_metrics"], scanpath.parent).open()))
        pts = [float(x["pts_ms"]) for x in rows]
        derived = historical.scanner.classify(
            pts,
            [float(x["overview_roi_mae"]) for x in rows],
            [float(x["previous_roi_mae"]) for x in rows],
            s["all_black_frames"],
            ct["entity"]["start_ms"],
            run["record_time_ms"],
        )
        ui.require(all(s[k] == v for k, v in derived.items()), "Prepared boundary scan decision changed")
        clean = decode_features(cr["video"])
        clock = decode_features(dr["video"])
        ui.require(
            np.array_equal(clean["pts"], clock["pts"]) and list(clean["pts"]) == pts,
            "Decoded clean/clock/prepared timestamps disagree",
        )
        first, last = s["first_usable_frame_index"], s["last_usable_frame_index"]
        ui.require(first is not None and last is not None, "No visible chase camera scene")
        scores, matches = scene_scores(
            clean["features"][first : last + 1],
            (clean["pts"][first : last + 1] - ct["entity"]["start_ms"]) / run["record_time_ms"],
            bank,
        )
        delta = np.abs(clean["features"].astype(np.int16) - clock["features"].astype(np.int16))
        metrics = {
            "first": first,
            "last": last,
            "pts": pts,
            "means": clean["means"].tolist(),
            "maxima": clean["maxima"].tolist(),
            "deltas": clean["deltas"].tolist(),
            "decoded_rgb_sha256": clean["hashes"],
            "black_frames": np.flatnonzero(clean["maxima"] == 0).tolist(),
            "scene_scores": scores.tolist(),
            "matched_reference_indices": matches,
            "reviewed_occlusion_matches": [bool(bank["occlusion"][j]) for j in matches],
            "candidate_abrupt_neighborhood": [
                any(abs(i - j) <= 4 for j in s["abrupt_frames"]) for i in range(first, last + 1)
            ],
            "entity_start_ms": ct["entity"]["start_ms"],
            "camera_end_ms": ct["camera"]["end_ms"],
            "finish_ms": run["record_time_ms"],
            "clean_clock_world_mae": float(delta[:, :WORLD].mean()),
            "clean_clock_car_mae": float(delta[:, WORLD:].mean()),
            "scanner_failures": s["failures"],
            "scanner": r["scan"],
            "scan_all_frame_metrics": s["all_frame_metrics"],
        }
        issues.extend(image_decision(metrics))
        new_json(output / "decoded-image-diagnostics.json", metrics)
        report["decoded_diagnostics"] = ui.pin(output / "decoded-image-diagnostics.json")
        report["evidence_files"].append(report["decoded_diagnostics"])
        report["visibility"] = {
            "first_usable_frame_index": first,
            "last_usable_frame_index": last,
            "first_usable_video_pts_ms": pts[first],
            "last_usable_video_pts_ms": pts[last],
        }
        report["source_finish_intersection"] = {
            "raw_visible_nominal_interval_ms": [
                pts[first] - ct["entity"]["start_ms"],
                pts[last] - ct["entity"]["start_ms"],
            ],
            "source_race_interval_ms": [0, run["record_time_ms"]],
            "source_finish_exclusive": True,
            "known_control_intersection_required": True,
            "raw_postfinish_tail_preserved_ms": max(
                0, pts[last] - ct["entity"]["start_ms"] - run["record_time_ms"]
            ),
            "strict_alignment_unchanged": True,
            "semantic_scene_matching_scope": "nominal_source_race_only; raw tail still fully decoded, screened for corruption/black/freeze, and bound to unchanged External camera readback",
        }
        historical.relative_check(ui.ref(r["correspondence"], draft_path.parent), cr["video"], dr["video"])
        pulsepath = ui.ref(r["pulses"], draft_path.parent)
        pulse = ui.read(pulsepath)
        ui.require(
            pulse["clean_native_video"] == ui.pin(cr["video"])
            and pulse["diagnostic_video"] == ui.pin(dr["video"]),
            "Prepared pulse videos differ",
        )
        markers = marker_checks(
            clean["crops"],
            clock["crops"],
            clean["pts"],
            pulse,
            output,
            ui.ref(ref["glyph_reference"], reference_path.parent),
        )
        new_json(
            output / "marker-identity.json",
            {
                "markers": markers,
                "method": "v1_ocr_primary_with_expected_independent_native_glyph_fallback",
                "policy_version": POLICY_VERSION,
                "glyph_reference": ref["glyph_reference"],
                "decoded_source_videos": {"clean": ui.pin(cr["video"]), "clock": ui.pin(dr["video"])},
                "manual_review_performed": False,
            },
        )
        report["evidence_files"].append(ui.pin(output / "marker-identity.json"))
        if not all(x["exact_text_verified"] for x in markers):
            issues.append("marker_text_identity_unresolved")
        report["marker_identity"] = ui.pin(output / "marker-identity.json")
        report["correspondence"] = r["correspondence"]
        report["pulses"] = r["pulses"]
        report["prepared_scan"] = r["scan"]
        report["settings_equivalence"] = ui.pin(output / "settings-equivalence.json")
        report["completion_equivalence"] = ui.pin(output / "completion-equivalence.json")
    except Exception as exc:
        issues.append(type(exc).__name__ + ": " + str(exc))
    report["unresolved_issues"] = issues
    report["status"] = "quarantined" if issues else "automatic_checks_passed"
    new_json(output / "automatic-review.json", report)
    return report


def verify_record(path):
    path = Path(path).resolve(strict=True)
    r = ui.read(path)
    base = path.parent
    ui.require(
        r.get("schema_version") == 2
        and r.get("policy_version") == POLICY_VERSION
        and r.get("review_mode") == MODE
        and r.get("status") == "automatic_checks_passed"
        and r.get("manual_review_performed") is False
        and r.get("unresolved_issues") == [],
        "Automatic policy did not pass without manual claims",
    )
    ui.require(
        r.get("policy") == POLICY and r["tools"] == tools_identity(),
        "Automatic implementation/dependencies changed",
    )
    reference, _ = validate_reference(ui.ref(r["reference"], base))
    draftpath = ui.ref(r["original_review_draft"], base)
    d = ui.read(draftpath)
    ui.require(
        r["original_prepare_pipeline"] == d["pipeline"] == ui.pin(Path(historical.__file__)),
        "Original preparation identity differs",
    )
    ui.require(
        r["policy_migration"]["from_build"] == historical.BUILD
        and r["policy_migration"]["to_build"] == BUILD
        and r["policy_migration"]["preserves_original_preparation_identity"] is True,
        "Missing explicit automatic policy migration",
    )
    for key in (
        "run_id",
        "ordinal",
        "clean_candidate",
        "clock_candidate",
        "source_clock_proof",
        "source_clock_contract",
        "timing_tools",
    ):
        ui.require(r[key] == d[key], "Automatic policy changed original identity: " + key)
    for ref in r["evidence_files"]:
        ui.ref(ref, base)
    for role, refs in r["candidates_source_evidence"].items():
        for ref in refs.values():
            ui.ref(ref, base)
    clean = ui.read(ui.ref(r["clean_candidate"], base))
    ui.require(r["native_video"] == clean["video"], "Automatic evidence video differs from source candidate")
    metadata = ui.ref(r["candidates_source_evidence"]["clean"]["action_metadata"], base)
    ui.require(
        r["source_clock_contract"]
        == historical.source_timing(ui.ref(r["source_clock_proof"], base), metadata),
        "Automatic source timing contract changed",
    )
    metrics = ui.read(ui.ref(r["decoded_diagnostics"], base))
    ui.require(not image_decision(metrics), "Decoded diagnostic decision no longer passes")
    bounds = {
        "first_usable_frame_index": metrics["first"],
        "last_usable_frame_index": metrics["last"],
        "first_usable_video_pts_ms": metrics["pts"][metrics["first"]],
        "last_usable_video_pts_ms": metrics["pts"][metrics["last"]],
    }
    ui.require(r["visibility"] == bounds, "Automatic visibility differs from actual diagnostic bounds")
    marker_doc = ui.read(ui.ref(r["marker_identity"], base))
    marks = marker_doc["markers"]
    ui.require(
        marker_doc["policy_version"] == POLICY_VERSION
        and marker_doc["glyph_reference"] == reference["glyph_reference"],
        "Marker decoder reference differs",
    )
    ui.require(
        marker_doc["decoded_source_videos"]
        == {role: r["candidates_source_evidence"][role]["video"] for role in ("clean", "clock")},
        "Glyph source video binding differs",
    )
    pulses = ui.read(ui.ref(r["pulses"], base))["pulses"]
    ui.require(
        len(marks) == len(pulses) == 3 and [m["phase"] for m in marks] == ["start", "middle", "finish"],
        "Incomplete/reordered marker identities",
    )
    glyph = glyph_module()
    bank = glyph.load_bank(ui.ref(reference["glyph_reference"], base))
    for m, pulse in zip(marks, pulses):
        ui.require(
            m["expected_text"] == pulse["native_text"]
            and all(
                m[k] == pulse[k]
                for k in ("phase", "first_visible_frame", "last_visible_frame", "onset_quantization_ms")
            ),
            "Marker result differs from actual serialized timing evidence",
        )
        inferred = m["glyph_inference"]
        ui.require(
            all(inferred[k] == m[k] for k in ("phase", "first_visible_frame", "last_visible_frame")),
            "Glyph ROI is bound to a different marker phase or frame interval",
        )
        saved = ui.read(ui.ref(inferred["report"], base))
        ui.require(
            {**saved, "report": inferred["report"]} == inferred,
            "Inline glyph diagnostics differ from pinned inference report",
        )
        glyph.verify_report(m["glyph_inference"], bank)
        decision = reconcile_marker(m["expected_text"], m["ocr_texts"], m["glyph_inference"])
        ui.require(
            m["policy_version"] == POLICY_VERSION
            and m["marker_decision"] == decision
            and m["exact_text_verified"] is True
            and decision["accepted"],
            "V2 marker policy decision failed",
        )
        for ref in m["evidence_files"]:
            ui.ref(ref, base)
    for key in ("settings_equivalence", "completion_equivalence"):
        facts = ui.read(ui.ref(r[key], base))
        ui.require(
            set(facts) == {"clean", "clock"} and all(x["passed"] for x in facts.values()),
            "Settings/completion decision failed",
        )
    return r


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("evaluate")
    e.add_argument("draft", type=Path)
    for k in ("reference", "output", "accepted"):
        e.add_argument("--" + k, type=Path, required=True)
    v = sub.add_parser("verify")
    v.add_argument("record", type=Path)
    a = p.parse_args()
    r = (
        evaluate(a.draft, a.reference, a.output, a.accepted)
        if a.command == "evaluate"
        else verify_record(a.record)
    )
    print(
        json.dumps(
            {
                "status": r["status"],
                "policy_version": POLICY_VERSION,
                "run_id": r.get("run_id"),
                "unresolved_issues": r.get("unresolved_issues", []),
            }
        )
    )
    raise SystemExit(0 if r["status"] == "automatic_checks_passed" else 2)


if __name__ == "__main__":
    main()
