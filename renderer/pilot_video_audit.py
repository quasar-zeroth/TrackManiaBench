#!/usr/bin/env python3
"""Repeatable local pilot evidence preparation. Never sends game/UI inputs.

The audit/mosaic commands make no visual acceptance claim. The accept command
requires an explicit, hash-bound review of actual screenshots and every race
frame; it calls the frozen native UI validator and keeps calibration separate.
Only ordinals1..5 are supported. Full-cohort release uses the existing gate.
"""

from __future__ import annotations
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import native_ui_queue_partial_v1 as ui
import video_audit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
pin = ui.pin
read = ui.read
require = ui.require
write = ui.common.write_json


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def run_source(ordinal):
    require(1 <= ordinal <= 5, "This helper only accepts the five pilot runs")
    require(
        os.environ.get("TRACKMANIABENCH_ACTIONS_MANIFEST"),
        "Set TRACKMANIABENCH_ACTIONS_MANIFEST to the verified action manifest",
    )
    manifest = read(Path(os.environ["TRACKMANIABENCH_ACTIONS_MANIFEST"]))
    run = next(r for r in manifest["runs"] if r["ordinal"] == ordinal)
    original = Path(run["actions"]["metadata_path"]).resolve(strict=True)
    require(ui.common.sha256(original) == run["actions"]["metadata_sha256"], "Action metadata changed")
    ui.metadata(original, run["ghost_sha256"], run, manifest["map"]["uid"])
    return manifest, run, original


def inspect(ordinal, clip, inspection, role):
    _, run, original = run_source(ordinal)
    return ui.clip_inspection(
        clip,
        inspection,
        run,
        original.parent / "visual_samples.csv",
        "production" if role == "clean" else "diagnostic",
    )


def frame_metadata(video):
    return json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_streams",
                "-of",
                "json",
                str(video),
            ]
        )
    )


def audit(ordinal, role, video, clip, inspection, output):
    timeline = inspect(ordinal, clip, inspection, role)
    output.mkdir(parents=True, exist_ok=True)
    verification = ui.common.video_check(video, timeline["expected_native_duration_seconds"])
    write(output / "format-verification.json", verification)
    probe = frame_metadata(video)
    count = len(probe["frames"])
    start = timeline["entity"]["start_ms"] / 20
    end = (timeline["entity"]["start_ms"] + timeline["serialized_scene_timing"]["record_end_ms"]) / 20
    chosen = {0, 1, 2, count // 2, count - 1}
    for boundary in (start, end):
        chosen.update(range(int(boundary) - 2, int(boundary) + 3))
    if role == "clock":
        for track in read(inspection)["tracks"]:
            for block in track["blocks"]:
                if block["class"] == "CGameCtnMediaBlockText":
                    for event in (min(block["key_times_ms"]), max(block["key_times_ms"])):
                        chosen.update(range(int(event / 20) - 1, int(event / 20) + 3))
    video_audit.audit(video, output, sorted(i for i in chosen if 0 <= i < count))
    write(
        output / "scene-source-verification.json",
        {
            "schema_version": 1,
            "status": "source_verified_visual_review_required",
            "ordinal": ordinal,
            "role": role,
            "video": pin(video),
            "clip": pin(clip),
            "inspection": pin(inspection),
            "timeline": timeline,
            "audit_source": pin(Path(__file__)),
        },
    )


def mosaics(video, output, first, last, roi=(224, 180, 416, 340)):
    """Every requested original-pixel crop, preserving indices/PTS and source hash."""
    output.mkdir(parents=True, exist_ok=True)
    probe = frame_metadata(video)
    frames = probe["frames"]
    stream = probe["streams"][0]
    require(0 <= first <= last < len(frames), "Invalid review frame interval")
    width, height = stream["width"], stream["height"]
    x0, y0, x1, y1 = roi
    require(0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height, "Invalid crop")
    cropw, croph = x1 - x0, y1 - y0
    columns, rows, label = 8, 12, 22
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
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        stdout=subprocess.PIPE,
    )
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    pages = []
    sheet = None
    page_start = None
    counter = 0
    for index, frame in enumerate(frames):
        raw = process.stdout.read(width * height * 3)
        require(len(raw) == width * height * 3, "Short RGB decode")
        if not first <= index <= last:
            continue
        slot = counter % (columns * rows)
        if slot == 0:
            sheet = Image.new("RGB", (columns * cropw, rows * (croph + label)), "black")
            draw = ImageDraw.Draw(sheet)
            page_start = index
        x = (slot % columns) * cropw
        y = (slot // columns) * (croph + label)
        source = Image.frombytes("RGB", (width, height), raw)
        sheet.paste(source.crop(roi), (x, y + label))
        draw.text(
            (x + 2, y + 2),
            f"{index} | {float(frame['best_effort_timestamp_time']) * 1000:.0f} ms",
            font=font,
            fill="white",
        )
        counter += 1
        if slot == columns * rows - 1 or index == last:
            path = output / f"frames-{page_start:04d}-{index:04d}.png"
            sheet.save(path)
            pages.append({"first_frame_index": page_start, "last_frame_index": index, **pin(path)})
    require(not process.stdout.read(1) and process.wait() == 0, "RGB decode failed")
    report = {
        "schema_version": 1,
        "status": "every_frame_mosaic_requires_visual_review",
        "video": pin(video),
        "first_frame_index": first,
        "last_frame_index": last,
        "frames": counter,
        "roi_xyxy": list(roi),
        "original_pixels_unscaled": True,
        "labels_outside_source_pixels": True,
        "pages": pages,
        "script": pin(Path(__file__)),
    }
    write(output / "mosaic-manifest.json", report)
    return report


def decode_equality(native, accepted, output):
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    for label, path in [("native", native), ("accepted", accepted)]:
        out = output / (label + "-decoded-frames.framemd5")
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-vsync",
                "0",
                "-f",
                "framemd5",
                "-y",
                str(out),
            ],
            check=True,
        )
        paths.append(out)

    # Compare complete framemd5 payload, including stream timebase and PTS/DTS.
    def data(p):
        return [l for l in p.read_text().splitlines() if not l.startswith("#software")]

    require(data(paths[0]) == data(paths[1]), "Accepted remux changed decoded frames or timestamps")
    report = {
        "schema_version": 1,
        "passed": True,
        "native_video": pin(native),
        "accepted_video": pin(accepted),
        "frames": sum(not l.startswith("#") for l in data(paths[0])),
        "all_decoded_frame_hashes_pts_dts_duration_size_equal": True,
        "evidence_files": [pin(p) for p in paths],
    }
    write(output / "native-accepted-decode-equality.json", report)
    return report


def accept(review_path, output, auditdir):
    review_path = review_path.resolve(strict=True)
    review = read(review_path)
    manifest, run, original = run_source(review["ordinal"])
    require(review.get("run_id") == run["run_id"], "Review run identity differs")
    require(review.get("verified_by") and review.get("verified_utc"), "Missing explicit reviewer/time")
    for key in (
        "settings_screenshot_reviewed",
        "completion_screenshot_reviewed",
        "scene_unchanged_until_render",
        "every_frame_window_reviewed",
    ):
        require(review.get(key) is True, "Actual review required: " + key)
    refs = {k: ui.ref(v, review_path.parent) for k, v in review["files"].items()}
    required = {
        "native_video",
        "file_closure",
        "imported_clip",
        "imported_clip_inspection",
        "reexported_clip",
        "reexported_clip_inspection",
        "readback_comparison",
        "settings_screenshot",
        "completion_screenshot",
        "mosaic_manifest",
        "frame_audit",
        "replay_action_metadata",
        "map_comparison",
    }
    require(required <= refs.keys(), "Missing actual export/review artifacts")
    comparison = read(refs["readback_comparison"])
    require(
        comparison.get("all_exposed_block_fields_equal") is True,
        "Native readback changed exposed entity/camera fields",
    )
    require(
        comparison.get("source_sha256") == ui.common.sha256(refs["imported_clip"])
        and comparison.get("native_readback_sha256") == ui.common.sha256(refs["reexported_clip"]),
        "Native readback comparison belongs to another clip",
    )
    for key in ("imported_inspection", "native_readback_inspection"):
        ui.ref(comparison[key], refs["readback_comparison"].parent)
    replay_meta = refs["replay_action_metadata"]
    map_check = refs["map_comparison"]
    replay = ui.ref(read(map_check)["native_replay"], map_check.parent)
    timeline = inspect(
        review["ordinal"], refs["reexported_clip"], refs["reexported_clip_inspection"], "clean"
    )
    mosaic = read(refs["mosaic_manifest"])
    require(
        mosaic["video"]["sha256"] == ui.common.sha256(refs["native_video"]),
        "Reviewed mosaic belongs to another video",
    )
    first, last = review["first_usable_frame_index"], review["last_usable_frame_index"]
    require(
        mosaic["first_frame_index"] == first and mosaic["last_frame_index"] == last,
        "Reviewed mosaic interval differs",
    )
    for page in mosaic["pages"]:
        ui.ref(page, refs["mosaic_manifest"].parent)
    obs = {"verified_by": review["verified_by"], "verified_utc": review["verified_utc"]}
    e = {
        "schema_version": 1,
        "purpose": "production",
        "run_id": run["run_id"],
        "ghost_sha256": run["ghost_sha256"],
        "map_sha256": manifest["map"]["sha256"],
        "settings": ui.SETTINGS,
        "native_replay": pin(replay),
        "original_action_metadata": pin(original),
        "replay_action_metadata": pin(replay_meta),
        "map_comparison": pin(map_check),
        **{
            k: pin(refs[k])
            for k in (
                "native_video",
                "file_closure",
                "imported_clip",
                "imported_clip_inspection",
                "reexported_clip",
                "reexported_clip_inspection",
            )
        },
        "settings_observation": {
            **obs,
            "evidence_files": [pin(refs["settings_screenshot"]), pin(refs["reexported_clip_inspection"])],
            "settings": ui.SETTINGS,
            "unobserved_settings": ["dof", "motion_blur", "tile_split"],
            "policy": "Unexposed settings remain null. Camera/HUD from actual serialized readback; visible export controls directly reviewed.",
        },
        "scene_observation": {
            **obs,
            "evidence_files": [pin(refs["readback_comparison"]), pin(refs["settings_screenshot"])],
            "native_replay_sha256": ui.common.sha256(replay),
            "imported_clip_sha256": ui.common.sha256(refs["imported_clip"]),
            "reexported_clip_sha256": ui.common.sha256(refs["reexported_clip"]),
            "unchanged_until_render": True,
        },
        "completion_observation": {
            **obs,
            "evidence_files": [pin(refs["completion_screenshot"]), pin(refs["file_closure"])],
            "native_ui_returned": True,
        },
        "explicit_review": pin(review_path),
        "status": "native_export_evidence_complete_calibration_separate",
        "pilot_gate_passed": False,
    }
    auditdir.mkdir(parents=True, exist_ok=False)
    evidence = auditdir / "native-export-evidence.json"
    require(not evidence.exists(), "Refuse overwrite of existing accepted source evidence")
    write(evidence, e)
    accepted = ui.accept(manifest, run, output, evidence)
    folder = output / run["run_id"]
    decode_equality(refs["native_video"], folder / "video.webm", auditdir)
    before = {p.name: (ui.common.sha256(p), p.stat().st_mtime_ns) for p in folder.iterdir() if p.is_file()}
    resumed = ui.accept(manifest, run, output, evidence)
    after = {p.name: (ui.common.sha256(p), p.stat().st_mtime_ns) for p in folder.iterdir() if p.is_file()}
    require(before == after and resumed["status"] == "skipped_verified", "Resume altered accepted artifact")
    write(
        auditdir / "resume-proof.json",
        {
            "schema_version": 1,
            "passed": True,
            "first_accept": accepted,
            "resume": resumed,
            "accepted_files_hash_and_mtime_unchanged": True,
            "files": before,
        },
    )
    frames = frame_metadata(refs["native_video"])["frames"]
    start = timeline["entity"]["start_ms"]
    first_pts = float(frames[first]["best_effort_timestamp_time"]) * 1000
    last_pts = float(frames[last]["best_effort_timestamp_time"]) * 1000
    write(
        auditdir / "observation-window-verification.json",
        {
            "schema_version": 1,
            "status": "contiguous_native_race_scene_reviewed",
            **obs,
            "native_video": pin(refs["native_video"]),
            "accepted_video": pin(folder / "video.webm"),
            "accepted_result": pin(folder / "result.json"),
            "first_usable_frame_index": first,
            "last_usable_frame_index": last,
            "first_usable_video_pts_ms": first_pts,
            "last_usable_video_pts_ms": last_pts,
            "first_usable_nominal_race_ms": first_pts - start,
            "last_usable_nominal_race_ms": last_pts - start,
            "frames_reviewed": last - first + 1,
            "reviewed_contiguous_race_visibility": True,
            "method": review["method"],
            "natural_occlusions": review.get("natural_occlusions", []),
            "terminal_observation_gap_ms": run["record_time_ms"] - (last_pts - start),
            "raw_frames_and_actions_retained": True,
            "terminal_transition_fabricated": False,
            "evidence_files": [pin(review_path), pin(refs["mosaic_manifest"]), pin(refs["frame_audit"])]
            + [pin(Path(p["path"])) for p in mosaic["pages"]],
            "calibration_status": "Separate per-run measured pulse and source-clock calibration required",
        },
    )
    print(json.dumps({"accept": accepted, "resume": resumed}))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("audit")
    a.add_argument("--ordinal", type=int, required=True)
    a.add_argument("--role", choices=["clean", "clock"], required=True)
    for k in ("video", "clip", "inspection", "output"):
        a.add_argument("--" + k, type=Path, required=True)
    a = sub.add_parser("mosaic")
    a.add_argument("video", type=Path)
    a.add_argument("output", type=Path)
    a.add_argument("--first", type=int, required=True)
    a.add_argument("--last", type=int, required=True)
    a = sub.add_parser("accept")
    a.add_argument("review", type=Path)
    a.add_argument("--accepted", type=Path, required=True)
    a.add_argument("--audit-output", type=Path, required=True)
    args = p.parse_args()
    if args.command == "audit":
        audit(args.ordinal, args.role, args.video, args.clip, args.inspection, args.output)
    elif args.command == "mosaic":
        mosaics(args.video, args.output, args.first, args.last)
    else:
        accept(args.review, args.accepted, args.audit_output)


if __name__ == "__main__":
    main()
