#!/usr/bin/env python3
"""Calibrate fixed boundary checks against explicitly reviewed pilot windows."""

import argparse
import csv
import datetime
import json
from pathlib import Path
import scan_native_video_partial_v1 as scan
import native_ui_queue_partial_v1 as ui


def validate(index_path, output, expected_count):
    index = ui.read(index_path)
    rows = []
    map_hash = None
    ui.require(
        1 <= expected_count <= 5 and len(index) == expected_count,
        "Provide one review for every requested pilot",
    )
    ui.require(
        sorted(x["ordinal"] for x in index) == list(range(1, expected_count + 1)),
        "Pilot ordinals must be contiguous from one",
    )
    for item in index:
        sp = ui.ref(item["scan"], index_path.parent)
        wp = ui.ref(item["review"], index_path.parent)
        s, w = ui.read(sp), ui.read(wp)
        ui.require(
            s["script"] == ui.pin(Path(scan.__file__)) and s["policy"] == scan.POLICY,
            "Scanner source or policy changed",
        )
        ui.require(s["ordinal"] == item["ordinal"], "Review index ordinal differs")
        ui.ref(s["video"], sp.parent)
        ui.ref(w["native_video"], wp.parent)
        ui.require(
            w.get("verified_by") and w.get("verified_utc") and w.get("method"),
            "Actual reviewer, time and method required",
        )
        ui.require(
            w["reviewed_contiguous_race_visibility"] is True
            and w["frames_reviewed"] == w["last_usable_frame_index"] - w["first_usable_frame_index"] + 1,
            "Every pilot race frame requires actual review",
        )
        ui.require(s["video"]["sha256"] == w["native_video"]["sha256"], "Scan and review videos differ")
        ui.require(
            all(s[k] == w[k] for k in ("first_usable_frame_index", "last_usable_frame_index")),
            "Automatic/manual boundaries disagree",
        )
        ui.require(w.get("unresolved_issues") == [], "Pilot review has unresolved issues")
        ui.require(w.get("evidence_files"), "Pilot review must pin actual reviewed images or mosaics")
        for ref in w["evidence_files"]:
            ui.ref(ref, wp.parent)
        map_hash = s["map_sha256"] if map_hash is None else map_hash
        ui.require(s["map_sha256"] == map_hash, "Pilot scans span different maps")
        metrics = list(csv.DictReader(ui.ref(s["all_frame_metrics"], sp.parent).open()))
        derived = scan.classify(
            [float(x["pts_ms"]) for x in metrics],
            [float(x["overview_roi_mae"]) for x in metrics],
            [float(x["previous_roi_mae"]) for x in metrics],
            s["all_black_frames"],
            s["timeline"]["entity"]["start_ms"],
            s["timeline"]["entity"]["race_time_ms"],
        )
        ui.require(
            derived["automatic_boundary_checks_passed"] and all(s[k] == v for k, v in derived.items()),
            "Boundary decision cannot be reproduced",
        )
        rows.append(
            {
                "ordinal": item["ordinal"],
                "scan": ui.pin(sp),
                "independent_manual_review": ui.pin(wp),
                "window_equal": True,
            }
        )
    ui.require(not output.exists(), "Validation evidence already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "schema_version": 1,
        "passed": True,
        "status": "specific_map_scanner_matches_reviewed_pilot_windows",
        "verified_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "map_sha256": map_hash,
        "scanner": ui.pin(Path(scan.__file__)),
        "policy": scan.POLICY,
        "validator": ui.pin(Path(__file__)),
        "pilots": rows,
        "scope": "Only this map, camera and export profile; no universal object-detection claim.",
    }
    ui.common.write_json(output, value)
    print(json.dumps({"passed": True, "pilots": len(rows), "output": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-count", type=int, default=5)
    args = parser.parse_args()
    validate(args.index, args.output, args.expected_count)
