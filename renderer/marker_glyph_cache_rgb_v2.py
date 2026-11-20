#!/usr/bin/env python3
"""Re-extract the fixed historical corpus using production RGB crop geometry."""

import argparse
import hashlib
from pathlib import Path
import subprocess
import numpy as np
import automatic_video_policy_v1 as p

c = p.ui.common

FILTER = "format=rgb24,crop=240:50:200:155"


def full_crops(video, indices=None):
    select = (
        "" if indices is None else "select=" + ("+".join(f"between(n\\,{a}\\,{b})" for a, b in indices)) + ","
    )
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-xerror",
        "-threads",
        "1",
        "-i",
        str(video),
        "-map",
        "0:v:0",
        "-vf",
        select + FILTER,
        "-vsync",
        "0",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "-",
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    p.ui.require(not result.stderr.strip(), "RGB crop decoder reported an error")
    p.ui.require(len(result.stdout) % (50 * 240 * 3) == 0, "Incomplete RGB marker crop")
    data = np.frombuffer(result.stdout, np.uint8).reshape(-1, 50, 240, 3)
    if indices is not None:
        p.ui.require(len(data) == sum(b - a + 1 for a, b in indices), "Selected crop frame count differs")
    return data


def frames(video, indices):
    return full_crops(video, indices)[:, 16:38, 10:230].copy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--original-cache", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    ui = p.ui
    ui.require(not a.output.exists(), "Corrected cache exists")
    a.output.mkdir(parents=True)
    original = ui.read(a.original_cache)
    ui.require(len(original["rows"]) >= 2, "At least two reviewed fitting runs required")
    rows = []
    for row in original["rows"]:
        old = ui.read(ui.ref(row["baseline"], a.original_cache.parent))
        review = ui.read(ui.ref(row["historical_review"], a.original_cache.parent))
        pulse = ui.read(ui.ref(review["pulses"], a.original_cache.parent))
        ui.require(
            review["marker_texts_reviewed"] == [m["native_text"] for m in pulse["pulses"]],
            "Unreviewed historical training label",
        )
        ca = full_crops(ui.ref(row["videos"]["clean"], a.original_cache.parent))
        cb = full_crops(ui.ref(row["videos"]["clock"], a.original_cache.parent))
        ui.require(ca.shape == cb.shape, "Historical paired crop dimensions differ")
        pts = p.frame_pts(ui.ref(row["videos"]["clock"], a.original_cache.parent))
        target = a.output / f"rank{row['ordinal']:03d}"
        target.mkdir()
        markers = p.marker_checks(ca, cb, pts, pulse, target)
        arrays = {}
        ocr_arrays = {}
        for marker in markers:
            phase = marker["phase"]
            first = marker["first_visible_frame"]
            last = marker["last_visible_frame"]
            previous = next(m for m in old["markers"] if m["phase"] == phase)
            ui.require(
                all(
                    marker[k] == previous[k]
                    for k in ("expected_text", "first_visible_frame", "last_visible_frame")
                ),
                "Historical marker identity or timing changed",
            )
            for kind, data in (("clean", ca), ("clock", cb)):
                arrays[phase + "_" + kind] = data[first : last + 1, 16:38, 10:230].copy()
                ocr_arrays[phase + "_" + kind] = data[first : last + 1].copy()
        pixels = target / "glyph-pixels.npz"
        np.savez_compressed(pixels, **arrays)
        fullpixels = target / "marker-crops.npz"
        np.savez_compressed(fullpixels, **ocr_arrays)
        record = {
            "ordinal": row["ordinal"],
            "run_id": row["run_id"],
            "actual_historical_review": row["historical_review"],
            "original_geometry_regression": row["baseline"],
            "markers": markers,
            "all_exact_labels_passed": all(m["exact_text_verified"] for m in markers),
            "exact_rgb_marker_crops": ui.pin(fullpixels),
            "exact_glyph_pixels": ui.pin(pixels),
            "decoder": ui.pin(Path(__file__)),
            "filter": FILTER,
            "full_frame_rgb_crop_xyxy": [200, 155, 440, 205],
            "glyph_global_xyxy": [210, 171, 430, 193],
            "source_videos": row["videos"],
            "source_pulses": review["pulses"],
            "frame_count": len(ca),
            "all_frame_marker_timing_rechecked": True,
            "clean_crop_stream_sha256": hashlib.sha256(ca.tobytes()).hexdigest(),
            "clock_crop_stream_sha256": hashlib.sha256(cb.tobytes()).hexdigest(),
            "policy_tools": p.tools_identity(),
            "manual_review_performed": False,
        }
        baseline = target / "regression.json"
        p.new_json(baseline, record)
        rows.append(
            {
                **row,
                "cache": ui.pin(pixels),
                "baseline": ui.pin(baseline),
                "original_geometry_cache": row["cache"],
                "original_geometry_baseline": row["baseline"],
                "exact_rgb_marker_crops": ui.pin(fullpixels),
            }
        )
        print(
            {"corrected_cache": row["ordinal"], "ocr_exact": [m["exact_text_verified"] for m in markers]},
            flush=True,
        )
    p.new_json(
        a.output / "index.json",
        {
            "schema_version": 1,
            "scope": "historical_marker_pixels_only_not_a_dataset",
            "rows": rows,
            "decoder": ui.pin(Path(__file__)),
            "frame_crop": [210, 171, 430, 193],
            "filter": FILTER,
            "original_geometry_cache": ui.pin(a.original_cache),
            "fixed_training_ordinals": [r["ordinal"] for r in original["rows"]],
            "geometry_correction": "RGB conversion before odd-y crop matches production full-frame RGB numpy slicing; original YUV crop rounded y155 to154.",
            "policy_tools": p.tools_identity(),
            "production_outputs_modified": False,
        },
    )


if __name__ == "__main__":
    main()
