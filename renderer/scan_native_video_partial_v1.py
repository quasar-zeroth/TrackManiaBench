#!/usr/bin/env python3
"""Deterministic whole-video screening for this fixed Summer2020-01 scene.

Never asserts human review or accepts a dataset. The overview/race thresholds
were fixed from manually reviewed pilot runs1..3 before inspecting runs4..5.
The ambiguity band intentionally fails closed. No source image is modified.
"""

from __future__ import annotations
import argparse
import csv
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import native_ui_queue_partial_v1 as ui

BUILD = "native-overview-scan-record-start-1"
MAP_UID = "XJ_JEjWGoAexDWe8qfaOjEcq5l8"
POLICY = {
    "roi_xyxy": [200, 160, 440, 340],
    "overview_mae_max": 12.0,
    "race_mae_min": 30.0,
    "abrupt_roi_delta_gt": 40.0,
    "static_roi_delta_lt": 0.1,
    "static_run_min_frames": 5,
    "max_unobserved_prefix_ms": 40,
    "max_unobserved_suffix_ms": 40,
    "threshold_development_runs": [1, 2, 3],
    "prospective_validation_runs": [4, 5],
}


def contiguous_groups(indices):
    groups = []
    for i in indices:
        if not groups or i != groups[-1][-1] + 1:
            groups.append([int(i)])
        else:
            groups[-1].append(int(i))
    return groups


def classify(pts, overview_mae, deltas, black, entity_start, finish_ms):
    """Pure classification; independent manually reviewed windows test this."""
    black = set(black)
    ambiguous = [
        i
        for i, v in enumerate(overview_mae)
        if i not in black and POLICY["overview_mae_max"] < v < POLICY["race_mae_min"]
    ]
    race = [i for i, v in enumerate(overview_mae) if i not in black and v >= POLICY["race_mae_min"]]
    groups = contiguous_groups(race)
    failures = []
    if ambiguous:
        failures.append("ambiguous_overview_vs_race_pixels")
    if len(groups) != 1:
        failures.append("race_scene_not_one_contiguous_interval")
    first, last = (race[0], race[-1]) if race else (None, None)
    if first is not None:
        if not 0 <= pts[first] - entity_start <= POLICY["max_unobserved_prefix_ms"]:
            failures.append("race_start_outside_calibrated_pilot_boundary_tolerance")
        next_pts = pts[last + 1] if last + 1 < len(pts) else None
        if (
            next_pts is None
            or not 0
            <= finish_ms - min(finish_ms, next_pts - entity_start)
            <= POLICY["max_unobserved_suffix_ms"]
        ):
            failures.append("race_end_outside_calibrated_pilot_boundary_tolerance")
        if any(first <= i <= last for i in black):
            failures.append("black_frame_within_candidate_race")
    abrupt = [
        i
        for i, d in enumerate(deltas)
        if first is not None and first < i <= last and d > POLICY["abrupt_roi_delta_gt"]
    ]
    static_groups = [
        g
        for g in contiguous_groups(
            [
                i
                for i, d in enumerate(deltas)
                if first is not None and first < i <= last and d < POLICY["static_roi_delta_lt"]
            ]
        )
        if len(g) >= POLICY["static_run_min_frames"]
    ]
    static = [i for group in static_groups for i in group]
    flags = sorted(set(abrupt + static + ambiguous + [i for i in black if i > 1]))
    return {
        "automatic_boundary_checks_passed": not failures,
        "failures": failures,
        "first_usable_frame_index": first,
        "last_usable_frame_index": last,
        "race_candidate_groups": [[g[0], g[-1]] for g in groups],
        "ambiguous_frames": ambiguous,
        "abrupt_frames": abrupt,
        "static_groups": [[g[0], g[-1]] for g in static_groups],
        "flagged_frames_require_review": flags,
    }


def scan(video, clip, inspection, proof, manifest_path, ordinal, action_metadata, output):
    ui.require(not (output / "scan.json").exists(), "Refuse overwrite of existing scan evidence")
    output.mkdir(parents=True, exist_ok=True)
    manifest = ui.read(manifest_path)
    run = next(r for r in manifest["runs"] if r["ordinal"] == ordinal)
    ui.require(manifest["map"]["uid"] == MAP_UID, "This image-boundary policy is specific to Summer2020-01")
    ui.metadata(action_metadata, run["ghost_sha256"], run, MAP_UID)
    timeline = ui.clip_inspection(
        clip, inspection, run, action_metadata.parent / "visual_samples.csv", "production"
    )
    comparison = ui.read(proof)
    ui.require(
        comparison.get("all_exposed_block_fields_equal") is True
        and comparison.get("native_readback_sha256") == ui.common.sha256(clip),
        "Actual native readback proof differs",
    )
    for key in ("imported_inspection", "native_readback_inspection"):
        ui.ref(comparison[key], proof.parent)
    fmt = ui.common.video_check(video, timeline["expected_native_duration_seconds"])
    probe = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=best_effort_timestamp_time",
                "-of",
                "json",
                str(video),
            ]
        )
    )
    pts = [float(f["best_effort_timestamp_time"]) * 1000 for f in probe["frames"]]
    process = subprocess.Popen(
        [
            "ffmpeg",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(video),
            "-map",
            "0:v:0",
            "-vsync",
            "0",
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            "-",
        ],
        stdout=subprocess.PIPE,
    )
    x0, y0, x1, y1 = POLICY["roi_xyxy"]
    rois = []
    stats = []
    black = []
    for index, t in enumerate(pts):
        raw = process.stdout.read(640 * 360)
        ui.require(len(raw) == 640 * 360, "Short full native decode")
        pixels = np.frombuffer(raw, np.uint8).reshape(360, 640)
        rois.append(pixels[y0:y1, x0:x1].astype(np.int16))
        if pixels.max() == 0:
            black.append(index)
        stats.append(
            {
                "frame_index": index,
                "pts_ms": t,
                "gray_min": int(pixels.min()),
                "gray_max": int(pixels.max()),
                "gray_mean": float(pixels.mean()),
                "decoded_gray_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    ui.require(not process.stdout.read(1) and process.wait() == 0, "Decode failed or extra frames")
    start = timeline["entity"]["start_ms"]
    ui.require(start == 200, "This validated boundary policy expects the200ms native preroll")
    refs = [i for i, t in enumerate(pts) if t <= start and i not in black]
    ui.require(len(refs) >= 4, "Insufficient actual nonblack preroll reference frames")
    reference = refs[-1]
    a = np.stack(rois)
    overview = np.abs(a - a[reference]).mean(axis=(1, 2))
    delta = np.r_[0, np.abs(a[1:] - a[:-1]).mean(axis=(1, 2))]
    result = classify(pts, overview, delta, black, start, run["record_time_ms"])
    for i, row in enumerate(stats):
        row.update(overview_roi_mae=float(overview[i]), previous_roi_mae=float(delta[i]))
    csvpath = output / "all-frame-screening.csv"
    with csvpath.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=stats[0])
        writer.writeheader()
        writer.writerows(stats)
    selected = {0, 1, reference}
    first, last = result["first_usable_frame_index"], result["last_usable_frame_index"]
    if first is not None:
        selected.update(int(round(v)) for v in np.linspace(first, last, 9))
        for bound in (first, last):
            selected.update(range(bound - 2, bound + 3))
    for flag in result["flagged_frames_require_review"]:
        selected.update(range(flag - 2, flag + 3))
    selected = sorted(i for i in selected if 0 <= i < len(pts))
    rgb = subprocess.Popen(
        [
            "ffmpeg",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(video),
            "-map",
            "0:v:0",
            "-vsync",
            "0",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        stdout=subprocess.PIPE,
    )
    evidence = []
    selected_set = set(selected)
    sheets = []
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    for index, t in enumerate(pts):
        raw = rgb.stdout.read(640 * 360 * 3)
        ui.require(len(raw) == 640 * 360 * 3, "Short RGB decode")
        if index not in selected_set:
            continue
        image = Image.frombytes("RGB", (640, 360), raw)
        path = output / f"frame-{index:06d}.png"
        image.save(path)
        slot = len(evidence) % 24
        if slot == 0:
            sheet = Image.new("RGB", (1280, 8 * 206), "black")
            draw = ImageDraw.Draw(sheet)
        x = (slot % 4) * 320
        y = (slot // 4) * 206
        sheet.paste(image.resize((320, 180), Image.BILINEAR), (x, y + 26))
        draw.text(
            (x + 3, y + 3),
            f"frame{index} | PTS{t:.0f}ms"
            + (" FLAG" if index in result["flagged_frames_require_review"] else ""),
            font=font,
            fill="white",
        )
        evidence.append({"frame_index": index, "pts_ms": t, **ui.pin(path)})
        if slot == 23 or index == selected[-1]:
            target = output / f"sampled-review-{len(sheets) + 1:02d}.png"
            sheet.save(target)
            sheets.append(ui.pin(target))
    ui.require(not rgb.stdout.read(1) and rgb.wait() == 0, "RGB review decode failed")
    report = {
        "schema_version": 1,
        "build": BUILD,
        "status": "automated_scan_complete_sampled_visual_review_required",
        "review_mode": "automated_scan_plus_sampled_visual_review",
        "manual_review_complete": False,
        "reviewed_contiguous_race_visibility": False,
        "ordinal": ordinal,
        "run_id": run["run_id"],
        "map_uid": MAP_UID,
        "map_sha256": manifest["map"]["sha256"],
        "ghost_sha256": run["ghost_sha256"],
        "video": ui.pin(video),
        "native_clip": ui.pin(clip),
        "native_inspection": ui.pin(inspection),
        "native_readback_proof": ui.pin(proof),
        "action_metadata": ui.pin(action_metadata),
        "timeline": timeline,
        "format_verification": fmt,
        "policy": POLICY,
        "script": ui.pin(Path(__file__)),
        "all_frames_scanned": len(pts),
        "all_black_frames": black,
        "overview_reference_frame": reference,
        "all_frame_metrics": ui.pin(csvpath),
        **result,
        "required_manual_frame_indices": selected,
        "manual_frame_indices": [],
        "selected_frame_evidence": evidence,
        "review_sheets": sheets,
        "all_raw_frames_and_actions_retained": True,
        "dataset_acceptance_asserted": False,
        "limitations": [
            "Pixel difference identifies departure from the preroll overview, not semantic object identity.",
            "Strict native source/readback, diagnostic timing and sampled/anomaly review remain required.",
            "Thresholds are specific to this map, camera, resolution and export profile. Ambiguous runs fail closed.",
        ],
    }
    ui.common.write_json(output / "scan.json", report)
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "ordinal",
                    "all_frames_scanned",
                    "automatic_boundary_checks_passed",
                    "first_usable_frame_index",
                    "last_usable_frame_index",
                    "flagged_frames_require_review",
                    "failures",
                )
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("video", "clip", "inspection", "proof", "manifest", "action-metadata", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--ordinal", type=int, required=True)
    args = parser.parse_args()
    scan(
        args.video,
        args.clip,
        args.inspection,
        args.proof,
        args.manifest,
        args.ordinal,
        args.action_metadata,
        args.output,
    )
