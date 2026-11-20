#!/usr/bin/env python3
"""Verify saved corrected training pixels against every pinned source video."""

import argparse
from pathlib import Path
import numpy as np
import marker_glyph_cache_rgb_v2 as crop
import automatic_video_policy_v2 as p

ui = p.ui


def verify(cache, output):
    index = ui.read(cache)
    original = ui.read(ui.ref(index["original_geometry_cache"], cache.parent))
    ui.require(
        [r["ordinal"] for r in index["rows"]] == [r["ordinal"] for r in original["rows"]],
        "Corrected training cohort changed",
    )
    ui.require(
        index["decoder"] == ui.pin(Path(crop.__file__)) and index["filter"] == crop.FILTER,
        "Corrected extraction tool changed",
    )
    cases = []
    for row, old in zip(index["rows"], original["rows"]):
        for key in ("run_id", "ordinal", "historical_review", "draft", "videos", "markers"):
            ui.require(row[key] == old[key], "Original training identity changed: " + key)
        report = ui.read(ui.ref(row["baseline"], cache.parent))
        ranges = [(m["first_visible_frame"], m["last_visible_frame"]) for m in row["markers"]]
        with np.load(ui.ref(row["cache"], cache.parent), allow_pickle=False) as glyph:
            with np.load(ui.ref(row["exact_rgb_marker_crops"], cache.parent), allow_pickle=False) as full:
                for kind in ("clean", "clock"):
                    actual = crop.full_crops(ui.ref(row["videos"][kind], cache.parent), ranges)
                    offset = 0
                    for marker, (first, last) in zip(row["markers"], ranges):
                        n = last - first + 1
                        name = marker["phase"] + "_" + kind
                        ui.require(
                            np.array_equal(actual[offset : offset + n], full[name]),
                            "Saved RGB marker crop differs from source video",
                        )
                        ui.require(
                            np.array_equal(actual[offset : offset + n, 16:38, 10:230], glyph[name]),
                            "Saved glyph crop differs from source video",
                        )
                        offset += n
        ui.require(report["policy_tools"] == p.v1.tools_identity(), "OCR source/tool identity changed")
        cases.append(
            {
                "ordinal": row["ordinal"],
                "baseline": row["baseline"],
                "pixels": row["cache"],
                "source_videos": row["videos"],
                "all_six_marker_arrays_exact": True,
            }
        )
        print({"source_pixels_verified": row["ordinal"]}, flush=True)
    doc = {
        "schema_version": 1,
        "status": "all_corrected_marker_pixels_source_verified",
        "cases": cases,
        "cache": ui.pin(cache),
        "verifier": ui.pin(Path(__file__)),
        "extractor": ui.pin(Path(crop.__file__)),
        "fixed_training_cohort_verified": True,
        "marker_arrays_verified": len(cases) * 6,
        "production_outputs_modified": False,
    }
    p.new_json(output, doc)
    return doc


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    verify(a.cache.resolve(), a.output.resolve())
