#!/usr/bin/env python3
"""Offline candidate audit and explicitly reviewed acceptance for the frozen cohort.

No game/UI inputs. prepare retains raw videos, scans every frame and creates a
review draft. accept requires actual sampled/anomaly and screenshot review;
it never silently changes the frozen pilot validators or a previous artifact.
"""

from __future__ import annotations
import argparse
import copy
import csv
import datetime
import json
import os
from pathlib import Path
import subprocess
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import native_ui_queue_partial_v1 as ui
import pilot_video_audit as pilot
import scan_native_video_partial_v1 as scanner
import compare_shifted_videos as correspondence

HERE = Path(__file__).resolve().parent
BUILD = "native-production-audit-record-start-1"
REVIEW_MODE = "automated_scan_plus_sampled_visual_review"
SOURCE_KEYS = ("native_replay", "map_comparison", "replay_action_metadata", "action_metadata")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def new_json(path, data):
    ui.require(not path.exists(), "Refuse overwrite: " + str(path))
    ui.common.write_json(path, data)


def manifest_at(path):
    return ui.common.normalise_manifest(path.resolve(), path.resolve().parent, "")


def check_gate(manifest, run, gate, accepted):
    if run["ordinal"] > 5:
        ui.require(gate is not None, "Verified five-run release gate required before production audit")
        result = ui.common.validate_gate(gate, manifest, accepted)
        ui.require(result["renderer_backend"] == "native_ui", "Release gate backend differs")


def candidate_at(path, manifest, run, role):
    c = ui.read(path)
    job = c["job"]
    base = path.parent
    ui.require(
        c.get("status") == "native_video_candidate" and job["role"] == role,
        "Wrong native candidate role/status",
    )
    for key in ("run_id", "ordinal", "account_id", "record_time_ms", "ghost_sha256"):
        ui.require(job["run"][key] == run[key], "Candidate run identity differs: " + key)
    if ui.common.exception_fact(run) is not None:
        ui.require(
            c.get("source_admission_tools") == ui.admission_tools(),
            "Exceptional candidate lacks current admission/policy provenance",
        )
    refs = {key: ui.ref(job[key], base) for key in (*SOURCE_KEYS, "clip", "inspection")}
    for key in (
        "video",
        "closure",
        "readback",
        "readback_proof",
        "settings_observation",
        "completion_observation",
    ):
        refs[key] = ui.ref(c[key], base)
    proof = ui.read(refs["readback_proof"])
    ui.require(
        proof.get("all_exposed_block_fields_equal") is True
        and proof.get("all_exposed_track_fields_equal") is True,
        "Production requires complete native track and block readback equality",
    )
    ui.require(
        proof["source_sha256"] == ui.common.sha256(refs["clip"])
        and proof["native_readback_sha256"] == ui.common.sha256(refs["readback"]),
        "Readback proof source differs",
    )
    refs["readback_inspection"] = ui.ref(proof["native_readback_inspection"], refs["readback_proof"].parent)
    ui.require(
        ui.ref(proof["imported_inspection"], refs["readback_proof"].parent).read_bytes()
        == refs["inspection"].read_bytes(),
        "Readback compares another imported inspection",
    )
    ui.metadata(refs["action_metadata"], run["ghost_sha256"], run, manifest["map"]["uid"])
    purpose = "production" if role == "clean" else "diagnostic"
    timeline = ui.clip_inspection(
        refs["readback"],
        refs["readback_inspection"],
        run,
        refs["action_metadata"].parent / "visual_samples.csv",
        purpose,
    )
    ui.common.video_check(refs["video"], timeline["expected_native_duration_seconds"])
    # These are machine execution records, not a substitute for visual review.
    seq = c.get("guarded_sequences", {})
    ui.require(
        {"export", "prepare", "render"} <= seq.keys(), "Missing guarded native export sequence evidence"
    )
    for name, record in seq.items():
        ui.ref(record["plan"], base)
        events = ui.ref(record["events"], base)
        rows = [json.loads(line) for line in events.read_text().splitlines()]
        ui.require(
            rows and rows[-1]["event"] == "sequence_complete", "Native UI sequence incomplete: " + name
        )
        identities = [r for r in rows if r.get("event") == "identity_verified"]
        ui.require(identities, "Guarded UI lacks process/window identity")
        for row in identities:
            keys = ("window", "pid", "size", "origin")
            ui.require(all(row[k] == c["identity"][k] for k in keys), "Guarded UI process/window changed")
            if run["ordinal"] > 5:
                ui.require(
                    row.get("process_start") == c["identity"].get("process_start")
                    and row.get("process_start"),
                    "Production guarded UI lacks stable process start identity",
                )
            elif "process_start" in row:
                ui.require(
                    row["process_start"] == c["identity"]["process_start"], "Pilot process start changed"
                )
    return c, refs, timeline


def relative_check(path, clean, clock):
    r = ui.read(path)
    ui.require(
        r["original_video"]["sha256"] == ui.common.sha256(clean)
        and r["shifted_video"]["sha256"] == ui.common.sha256(clock),
        "Scene correspondence video differs",
    )
    ui.require(
        r["declared_frame_shift"] == 0 and r["actual_pts_shift_range_ms"] == [0, 0], "Clean/clock PTS shifted"
    )
    ui.ref(r["per_frame_metrics"], path.parent)
    for region in ("road", "car"):
        counts = r["best_offset_frame_counts"][region]
        mae = r["same_resolution_rgb_mean_absolute_error_0_to255"][region]
        ui.require(
            counts["0"] / r["frames_compared"] >= 0.95,
            "Scene correspondence has too many nonzero best offsets",
        )
        ui.require(mae["0"] * 1.5 < min(mae["-1"], mae["1"]), "Scene correspondence timing is ambiguous")
    return r


def pulses(clean, clock, inspection, timeline, out):
    """Candidate event measurement; exact visible text must still be reviewed."""
    out.mkdir(parents=True, exist_ok=True)
    frames = pilot.frame_metadata(clock)
    pts = [float(f["best_effort_timestamp_time"]) * 1000 for f in frames["frames"]]
    ui.require(
        pts == [float(f["best_effort_timestamp_time"]) * 1000 for f in pilot.frame_metadata(clean)["frames"]],
        "Clean and diagnostic frame timestamps differ",
    )
    new_json(out / "all-video-frame-metadata.json", frames)
    blocks = [
        b
        for t in ui.read(inspection)["tracks"]
        for b in t["blocks"]
        if b["class"] == "CGameCtnMediaBlockText"
    ]
    blocks.sort(key=lambda b: min(b["key_times_ms"]))
    ui.require(len(blocks) == 3, "Exactly three known text events required")
    arrays = []
    for video in (clean, clock):
        raw = subprocess.check_output(
            [
                "ffmpeg",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(video),
                "-vf",
                "crop=240:50:200:155",
                "-vsync",
                "0",
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "-",
            ]
        )
        arrays.append(np.frombuffer(raw, np.uint8).reshape(-1, 50, 240, 3).astype(np.int16))
    a, b = arrays
    ui.require(len(a) == len(b) == len(pts), "Decoded diagnostic length differs")
    scores = ((b.min(axis=3) > 190) & ((b - a).mean(axis=3) > 70)).sum(axis=(1, 2))
    csvpath = out / "marker-differential-all-frames.csv"
    with csvpath.open("x", newline="") as f:
        w = csv.writer(f)
        w.writerow(["frame_index", "pts_ms", "bright_added_pixels"])
        w.writerows((i, t, int(scores[i])) for i, t in enumerate(pts))
    result = []
    sheet = Image.new("RGB", (960, 300), "black")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    for row, (phase, block) in enumerate(zip(("start", "middle", "finish"), blocks)):
        start, end = min(block["key_times_ms"]), max(block["key_times_ms"])
        active = [i for i, t in enumerate(pts) if start - 60 <= t <= end + 60 and scores[i] > 100]
        ui.require(
            active and active == list(range(active[0], active[-1] + 1)), "Diagnostic pulse is not contiguous"
        )
        first, last = active[0], active[-1]
        ui.require(
            0 <= pts[first] - start <= 20 and 0 <= pts[last] - end <= 20, "Diagnostic event outside20ms bound"
        )
        refs = []
        for col, index in enumerate((first - 1, first, last, last + 1)):
            path = out / f"frame-{index:06d}.png"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-xerror",
                    "-i",
                    str(clock),
                    "-vf",
                    f"select='eq(n,{index})'",
                    "-vsync",
                    "0",
                    "-frames:v",
                    "1",
                    str(path),
                ],
                check=True,
            )
            refs.append(ui.pin(path))
            sheet.paste(Image.open(path).crop((200, 155, 440, 205)), (col * 240, row * 100 + 40))
            draw.text(
                (col * 240 + 3, row * 100 + 2),
                f"{phase} frame{index}\nPTS{pts[index]:.0f}ms score{scores[index]}",
                font=font,
                fill="white",
            )
        result.append(
            {
                "phase": phase,
                "native_text": block["text"].replace("$fff$o", ""),
                "serialized_clip_interval_ms": [start, end],
                "last_inactive_before_frame": first - 1,
                "last_inactive_before_pts_ms": pts[first - 1],
                "first_visible_frame": first,
                "first_visible_pts_ms": pts[first],
                "last_visible_frame": last,
                "last_visible_pts_ms": pts[last],
                "first_inactive_after_frame": last + 1,
                "first_inactive_after_pts_ms": pts[last + 1],
                "onset_quantization_ms": pts[first] - start,
                "known_event_nominal_race_ms": start - timeline["entity"]["start_ms"],
                "measurement_interpretation": "Event time is quantized by50FPS exposure and text activation. No global+20ms offset is inferred.",
                "evidence_files": refs,
            }
        )
    sheetpath = out / "marker-boundary-review.png"
    sheet.save(sheetpath)
    return {
        "clean_native_video": ui.pin(clean),
        "diagnostic_video": ui.pin(clock),
        "native_readback_inspection": ui.pin(inspection),
        "pulses": result,
        "all_frame_metadata": ui.pin(out / "all-video-frame-metadata.json"),
        "detection": {
            "roi_xyxy": [200, 155, 440, 205],
            "clock_channel_min_gt": 190,
            "clock_minus_clean_mean_gt": 70,
            "added_pixel_threshold_gt": 100,
            "all_frame_scores": ui.pin(csvpath),
            "review_sheet": ui.pin(sheetpath),
            "automatic_detection_does_not_certify_text_identity": True,
        },
    }


def prepare(args):
    started = time.monotonic()
    manifest = manifest_at(args.manifest)
    run = next(r for r in manifest["runs"] if r["ordinal"] == args.ordinal)
    check_gate(manifest, run, args.gate, args.accepted)
    ui.require(not args.output.exists(), "Prepare requires a new output directory")
    args.output.mkdir(parents=True)
    clean, cr, ct = candidate_at(args.clean_candidate, manifest, run, "clean")
    clock, dr, dt = candidate_at(args.clock_candidate, manifest, run, "clock")
    ui.require(
        all(cr[k].read_bytes() == dr[k].read_bytes() for k in SOURCE_KEYS), "Clean/clock source proofs differ"
    )
    validation = Path(os.environ.get("TRACKMANIABENCH_SCANNER_VALIDATION", "missing-scanner-validation.json"))
    v = ui.read(validation)
    ui.require(v.get("map_sha256") == manifest["map"]["sha256"], "Scanner validation belongs to another map")
    ui.require(
        v["passed"] and v["scanner"]["sha256"] == ui.common.sha256(HERE / "scan_native_video_partial_v1.py"),
        "Scanner lacks frozen pilot validation",
    )
    scanner.scan(
        cr["video"],
        cr["readback"],
        cr["readback_inspection"],
        cr["readback_proof"],
        args.manifest,
        args.ordinal,
        cr["action_metadata"],
        args.output / "scan",
    )
    correspondence.main(cr["video"], dr["video"], args.output / "correspondence", 0)
    relative_check(args.output / "correspondence/relative-scene-verification.json", cr["video"], dr["video"])
    pulse = pulses(cr["video"], dr["video"], dr["readback_inspection"], dt, args.output / "clock")
    new_json(args.output / "clock/pulse-candidates.json", pulse)
    scan = ui.read(args.output / "scan/scan.json")
    review = {
        "schema_version": 1,
        "build": BUILD,
        "status": "awaiting_actual_sampled_anomaly_and_settings_review",
        "ordinal": args.ordinal,
        "run_id": run["run_id"],
        "manifest": ui.pin(args.manifest),
        "clean_candidate": ui.pin(args.clean_candidate),
        "clock_candidate": ui.pin(args.clock_candidate),
        "source_clock_proof": ui.pin(args.source_clock_proof),
        "scan": ui.pin(args.output / "scan/scan.json"),
        "scanner_validation": ui.pin(validation),
        "pulses": ui.pin(args.output / "clock/pulse-candidates.json"),
        "correspondence": ui.pin(args.output / "correspondence/relative-scene-verification.json"),
        "review_mode": REVIEW_MODE,
        "required_manual_frame_indices": scan["required_manual_frame_indices"],
        "manual_frame_indices": [],
        "flag_resolutions": [],
        "marker_texts_reviewed": [],
        "settings_reviewed": False,
        "completion_reviewed": False,
        "scene_unchanged_until_render": False,
        "sampled_continuity_reviewed": False,
        "settings_observed": None,
        "verified_by": None,
        "verified_utc": None,
        "method": None,
        "natural_occlusions": [],
        "pipeline": ui.pin(Path(__file__)),
    }
    if args.gate:
        review["pilot_gate"] = ui.pin(args.gate)
    new_json(args.output / "review-draft.json", review)
    new_json(
        args.output / "prepare-performance.json",
        {
            "schema_version": 1,
            "build": BUILD,
            "elapsed_seconds": time.monotonic() - started,
            "frames_per_video": scan["all_frames_scanned"],
            "native_videos": 2,
            "manual_review_time_included": False,
            "pipeline": ui.pin(Path(__file__)),
        },
    )
    print(
        json.dumps(
            {
                "status": "actual_review_required",
                "review_draft": str(args.output / "review-draft.json"),
                "automatic_boundary_checks_passed": scan["automatic_boundary_checks_passed"],
            }
        )
    )


def validate_review(review_path, accepted):
    r = ui.read(review_path)
    base = review_path.parent
    ui.require(r["build"] == BUILD and r["review_mode"] == REVIEW_MODE, "Review uses another policy")
    ui.require(
        r.get("verified_by") and r.get("verified_utc") and r.get("method"),
        "Explicit reviewer/time/method required",
    )
    ui.require(
        r["pipeline"]["sha256"] == ui.common.sha256(Path(__file__)),
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
    ui.require(
        s["script"]["sha256"] == ui.common.sha256(HERE / "scan_native_video_partial_v1.py")
        and s["policy"] == scanner.POLICY,
        "Scanner source/policy differs",
    )
    ui.require(
        s["automatic_boundary_checks_passed"] and not s["failures"] and not s["ambiguous_frames"],
        "Ambiguous video requires separate full review, not automatic acceptance",
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
    ui.require(
        0 <= pts[first] - ct["entity"]["start_ms"] <= 40,
        "Candidate visible start exceeds the independently tested pixel-boundary limit",
    )
    if ui.common.exception_fact(run) is None:
        ui.require(
            pts[last] - ct["entity"]["start_ms"] <= run["record_time_ms"],
            "Ordinary candidate race images extend beyond source finish",
        )
    else:
        ui.require(pts[last] <= ct["camera"]["end_ms"], "Measured visibility exceeds the native clip")
        # Preserve the measured visible tail. The separate calibrated join
        # intersects this window with the source race and known input interval.
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
    ui.ref(r["source_clock_proof"], base)
    return r, manifest, run, candidates, s, pts, pulse


def evidence_for(r, run, manifest, candidate, review_path, role):
    c, refs, _ = candidate
    obs = {"verified_by": r["verified_by"], "verified_utc": r["verified_utc"]}
    e = {
        "schema_version": 1,
        "purpose": "production" if role == "clean" else "diagnostic",
        "run_id": run["run_id"],
        "ghost_sha256": run["ghost_sha256"],
        "map_sha256": manifest["map"]["sha256"],
        "settings": ui.SETTINGS,
        "native_replay": ui.pin(refs["native_replay"]),
        "original_action_metadata": ui.pin(refs["action_metadata"]),
        "replay_action_metadata": ui.pin(refs["replay_action_metadata"]),
        "map_comparison": ui.pin(refs["map_comparison"]),
        "native_video": ui.pin(refs["video"]),
        "file_closure": ui.pin(refs["closure"]),
        "imported_clip": ui.pin(refs["clip"]),
        "imported_clip_inspection": ui.pin(refs["inspection"]),
        "reexported_clip": ui.pin(refs["readback"]),
        "reexported_clip_inspection": ui.pin(refs["readback_inspection"]),
        "settings_observation": {
            **obs,
            "evidence_files": [ui.pin(refs["settings_observation"]), ui.pin(refs["readback_inspection"])],
            "settings": ui.SETTINGS,
            "unobserved_settings": ["dof", "motion_blur", "tile_split"],
        },
        "scene_observation": {
            **obs,
            "evidence_files": [ui.pin(refs["readback_proof"]), r[role + "_candidate"]],
            "native_replay_sha256": ui.common.sha256(refs["native_replay"]),
            "imported_clip_sha256": ui.common.sha256(refs["clip"]),
            "reexported_clip_sha256": ui.common.sha256(refs["readback"]),
            "unchanged_until_render": True,
        },
        "completion_observation": {
            **obs,
            "evidence_files": [ui.pin(refs["completion_observation"]), ui.pin(refs["closure"])],
            "native_ui_returned": True,
        },
        "explicit_review": ui.pin(review_path),
        "review_mode": REVIEW_MODE,
        "status": "native_export_evidence_complete_calibration_separate",
    }
    if ui.common.exception_fact(run) is not None:
        e["source_admission"] = ui.source_admission(run, refs["action_metadata"])
    return e


def accept(args):
    r, m, run, candidates, s, pts, pulse = validate_review(args.review, args.accepted)
    out = args.review.parent
    started = time.monotonic()
    receipt = out / "acceptance-receipt.json"
    if receipt.exists():
        prior = ui.read(receipt)
        ui.require(prior["review"] == ui.pin(args.review), "Resume review differs")
        for item in prior["evidence_files"]:
            ui.ref(item, out)
        folder = args.accepted / run["run_id"]
        record = ui.read(folder / "result.json")
        ui.common.validate_render_result(record, m, run, folder)
        print(json.dumps({"status": "skipped_verified", "receipt": str(receipt)}))
        return
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
        "calibration_status": "Separate measured pulse plus original source clock calibration required",
    }
    if ui.common.exception_fact(run) is not None:
        obs.update(
            source_admission=ui.source_admission(run, candidates["clean"][1]["action_metadata"]),
            full_native_visibility_preserved=True,
            visible_nominal_tail_past_finish_ms=max(0, pts[last] - start - run["record_time_ms"]),
            training_window_requires_measured_clock_intersection=True,
        )
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
                "Combine per-run original record/race corroboration for a conservative30ms total.",
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
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("prepare")
    for field in (
        "manifest",
        "clean-candidate",
        "clock-candidate",
        "source-clock-proof",
        "output",
        "accepted",
    ):
        a.add_argument("--" + field, type=Path, required=True)
    a.add_argument("--ordinal", type=int, required=True)
    a.add_argument("--gate", type=Path)
    a = sub.add_parser("accept")
    a.add_argument("review", type=Path)
    a.add_argument("--accepted", type=Path, required=True)
    a = p.parse_args()
    if a.command == "prepare":
        prepare(a)
    else:
        accept(a)


if __name__ == "__main__":
    main()
