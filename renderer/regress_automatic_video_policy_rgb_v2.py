#!/usr/bin/env python3
"""Validate combined v2 decisions on actual LOO markers and altered ROI controls.

Fresh exact-RGB OCR transcripts use unchanged recognition code. New
negative pixels go through the frozen v1 timing detector and OCR again. No
production media or acceptance records are modified.
"""

import argparse
import json
from pathlib import Path
import time
import numpy as np
import automatic_video_policy_v2 as p

ui = p.ui


def negative(report_path, row, output):
    glyph = p.glyph_module()
    inference = ui.read(report_path)
    glyph.verify_report(inference, glyph.load_bank(ui.ref(inference["bank"], report_path.parent)))
    phase = inference["phase"]
    marker = next(x for x in row["markers"] if x["phase"] == phase)
    with np.load(ui.ref(inference["pixels"], report_path.parent), allow_pickle=False) as z:
        ca, cb = z["clean"], z["clock"]
    # Preserve exact RGB glyph pixels and their original spatial ROI, adding
    # an empty frame at either side solely for synthetic boundary controls.
    clean = np.zeros((len(ca) + 2, 50, 240, 3), np.uint8)
    clock = clean.copy()
    clean[1:-1, 16:38, 10:230] = ca
    clock[1:-1, 16:38, 10:230] = cb
    pts = np.arange(len(clean)) * 20
    pulse = {
        "pulses": [
            {
                "phase": phase,
                "native_text": marker["expected_text"],
                "serialized_clip_interval_ms": [20, len(ca) * 20],
                "first_visible_frame": 1,
                "last_visible_frame": len(ca),
            }
        ]
    }
    output.mkdir(parents=True, exist_ok=False)
    try:
        observed = p.v1.marker_checks(clean, clock, pts, pulse, output)[0]
        decision = p.reconcile_marker(marker["expected_text"], observed["ocr_texts"], inference)
        status = {
            "rejected": not decision["accepted"],
            "marker_decision": decision,
            "fresh_ocr_texts": observed["ocr_texts"],
            "ocr_evidence_files": observed["evidence_files"],
        }
    except ValueError as exc:
        # These are actual deterministic failures of the synthetic altered
        # marker window. Other exceptions stop this validation job.
        allowed = (
            "Noncontiguous diagnostic marker",
            "Marker timing changed",
            "Unexpected diagnostic text outside serialized marker intervals",
        )
        ui.require(str(exc) in allowed, "Unexpected negative-control error: " + str(exc))
        status = {"rejected": True, "timing_detector_rejection": str(exc), "fresh_ocr_performed": False}
    record = {
        "schema_version": 1,
        "ordinal": row["ordinal"],
        "phase": phase,
        "expected_native_text": marker["expected_text"],
        "historical_marker_cache": row["cache"],
        "synthetic_pixels": True,
        "source_glyph_inference": ui.pin(report_path),
        "source_pixels": inference["pixels"],
        "policy_tools": p.tools_identity(),
        **status,
    }
    p.new_json(output / "decision.json", record)
    return ui.pin(output / "decision.json")


def regress(validation_root, cache, output, wait_seconds):
    ui.require(not output.exists(), "Joint regression output exists")
    output.mkdir(parents=True)
    initial_tools = p.tools_identity()
    index = ui.read(cache)
    ui.require(index["filter"] == "format=rgb24,crop=240:50:200:155", "Incorrect crop geometry")
    ocr_identity = index["decoder"]
    ui.ref(ocr_identity, cache.parent)
    rows = {r["ordinal"]: r for r in index["rows"]}
    ui.require(len(rows) >= 2, "At least two fitting runs required")
    processed = {}
    deadline = time.monotonic() + wait_seconds
    validation_path = validation_root / "validation.json"
    while True:
        for path in sorted(validation_root.glob("rank*/negative-*/glyph-*-report.json")):
            if str(path) in processed:
                continue
            ordinal = int(path.parent.parent.name.removeprefix("rank"))
            target = output / "negative-controls" / path.parent.parent.name / path.parent.name
            processed[str(path)] = negative(path, rows[ordinal], target)
            print(
                json.dumps(
                    {
                        "negative": str(path),
                        "rejected": ui.read(ui.ref(processed[str(path)], output))["rejected"],
                    }
                ),
                flush=True,
            )
        if validation_path.exists():
            break
        ui.require(time.monotonic() < deadline, "Timed out waiting for complete glyph validation")
        time.sleep(2)
    validation = ui.read(validation_path)
    controls = validation["negative_controls"]
    ui.require(controls and all(x["rejected"] for x in controls), "Glyph negative controls did not pass")
    # A final manifest may appear just after its final per-case output. Ensure
    # every listed control, including any future expanded cases, was evaluated.
    for control in controls:
        path = ui.ref(control["inference_report"], validation_root)
        if str(path) not in processed:
            row = rows[control["ordinal"]]
            target = output / "negative-controls" / f"rank{row['ordinal']:03d}" / path.parent.name
            processed[str(path)] = negative(path, row, target)
    cases = []
    wrong_labels = []
    for case in validation["cases"]:
        path = ui.ref(case["report"], validation_root)
        record = ui.read(path)
        ordinal = case["ordinal"]
        baseline_path = ui.ref(rows[ordinal]["baseline"], cache.parent)
        baseline = ui.read(baseline_path)
        ui.require(
            baseline["filter"] == index["filter"]
            and baseline["policy_tools"] == p.v1.tools_identity()
            and baseline["all_frame_marker_timing_rechecked"] is True,
            "Fresh exact-RGB OCR evidence differs",
        )
        with np.load(ui.ref(baseline["exact_glyph_pixels"], cache.parent), allow_pickle=False) as actual:
            with np.load(ui.ref(rows[ordinal]["cache"], cache.parent), allow_pickle=False) as fitted:
                ui.require(
                    set(actual.files) == set(fitted.files)
                    and all(np.array_equal(actual[k], fitted[k]) for k in actual.files),
                    "Fresh OCR and glyph pixels differ",
                )
        ui.require(len(record["markers"]) == 3, "Incomplete LOO marker case")
        results = []
        for marker in record["markers"]:
            phase = marker["phase"]
            old = next(x for x in baseline["markers"] if x["phase"] == phase)
            ui.require(
                marker["expected_text"] == old["expected_text"], "Actual historical text identity differs"
            )
            inference = marker["inference"]
            bank = p.glyph_module().load_bank(ui.ref(inference["bank"], path.parent))
            ui.require(
                ordinal not in bank["manifest"]["training_ordinals"],
                "LOO glyph bank includes the held-out run",
            )
            p.glyph_module().verify_report(inference, bank)
            for ref in old["evidence_files"]:
                ui.ref(ref, baseline_path.parent)
            decision = p.reconcile_marker(old["expected_text"], old["ocr_texts"], inference)
            changed = old["expected_text"].replace("CLIP ", "CLIP 9", 1)
            wrong_labels.append(not p.reconcile_marker(changed, old["ocr_texts"], inference)["accepted"])
            other = next(x["expected_text"] for x in baseline["markers"] if x["phase"] != phase)
            wrong_labels.append(not p.reconcile_marker(other, old["ocr_texts"], inference)["accepted"])
            results.append(
                {
                    "phase": phase,
                    "expected_text": old["expected_text"],
                    "decision": decision,
                    "original_ocr": old,
                    "glyph_inference": inference,
                    "expected_not_used_for_glyph_inference": True,
                }
            )
        out = output / f"rank{ordinal:03d}.json"
        p.new_json(
            out,
            {
                "ordinal": ordinal,
                "original_ocr_report": ui.pin(baseline_path),
                "glyph_loo_report": ui.pin(path),
                "markers": results,
            },
        )
        cases.append({"ordinal": ordinal, "report": ui.pin(out)})
    accepted = sum(
        m["decision"]["accepted"] for x in cases for m in ui.read(ui.ref(x["report"], output))["markers"]
    )
    negative_passed = all(ui.read(ui.ref(x, output))["rejected"] for x in processed.values()) and all(
        wrong_labels
    )
    ui.require(initial_tools == p.tools_identity(), "Policy or decoder changed during regression")
    result = {
        "schema_version": 2,
        "policy_version": p.POLICY_VERSION,
        "status": "joint_marker_policy_validated"
        if len(cases) == len(rows) and accepted == len(rows) * 3 and negative_passed
        else "joint_marker_policy_failed",
        "policy_tools": initial_tools,
        "actual_marker_cases": len(cases) * 3,
        "accepted_correct_labels": accepted,
        "negative_controls_passed": negative_passed,
        "actual_altered_roi_controls": len(processed),
        "wrong_numeric_or_reordered_label_cases": len(wrong_labels),
        "all_wrong_labels_rejected": all(wrong_labels),
        "cases": cases,
        "negative_controls": list(processed.values()),
        "glyph_validation": ui.pin(validation_path),
        "fresh_rgb_ocr_extractor": ocr_identity,
        "cache": ui.pin(cache),
        "source": ui.pin(Path(__file__)),
        "manual_review_performed": False,
        "limitations": [
            "Glyph thresholds and OCR strength criteria were calibrated using the reviewed fitting runs; these are not untouched statistical test results.",
            "OCR preprocessing families are correlated transforms, not independent statistical confidence.",
            "Exact source video and actual serialized marker timing checks remain required for production acceptance.",
        ],
    }
    p.new_json(output / "validation.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("glyph-validation-root", "cache", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=1800)
    a = parser.parse_args()
    r = regress(a.glyph_validation_root.resolve(), a.cache.resolve(), a.output.resolve(), a.wait_seconds)
    print(
        json.dumps(
            {
                k: r[k]
                for k in (
                    "status",
                    "actual_marker_cases",
                    "accepted_correct_labels",
                    "negative_controls_passed",
                )
            }
        )
    )
    raise SystemExit(0 if r["status"] == "joint_marker_policy_validated" else 2)


if __name__ == "__main__":
    main()
