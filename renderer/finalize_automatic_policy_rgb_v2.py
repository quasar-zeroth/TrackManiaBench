#!/usr/bin/env python3
"""Bind independently validated scene and glyph evidence into a reusable policy.

This does not create human reviews or accept datasets. Failed, missing or mixed
validation evidence cannot be promoted into an automatic reference.
"""

import argparse
from pathlib import Path
import automatic_video_policy_v2 as p

ui = p.ui


def finalize(scene, bank_path, glyph_validation, joint_validation, output):
    scene_document, _ = p.v1.validate_reference(scene)
    bank = p.glyph_module().load_bank(bank_path)
    glyph = ui.read(glyph_validation)
    joint = ui.read(joint_validation)
    ui.require(glyph["bank"] == ui.pin(bank_path), "Glyph validation tested another bank")
    ui.require(glyph["training_cache"] == bank["manifest"]["training_cache_index"], "Fitting cache differs")
    ui.require(glyph["decoder"] == p.tools_identity()["glyph_decoder"], "Glyph decoder changed")
    ui.require(
        not glyph["loo_confident_wrong"]
        and glyph["all_negative_images_rejected"]
        and glyph["all_wrong_string_phase_labels_rejected"],
        "Glyph negative/holdout safety failed",
    )
    ui.require(
        glyph["untouched_marker_count"] > 0 and glyph["untouched_exact"] == glyph["untouched_marker_count"],
        "Untouched holdout labels failed",
    )
    ui.require(
        joint["glyph_validation"] == ui.pin(glyph_validation) and joint["policy_tools"] == p.tools_identity(),
        "Joint validation uses different tools or glyph evidence",
    )
    ui.require(
        joint["status"] == "joint_marker_policy_validated"
        and joint["actual_marker_cases"] == joint["accepted_correct_labels"] == glyph["loo_cases"]
        and joint["actual_marker_cases"] >= 6
        and joint["negative_controls_passed"]
        and joint["all_wrong_labels_rejected"]
        and joint["actual_altered_roi_controls"] > 0,
        "Joint marker positives and actual altered-image negatives did not pass",
    )
    for report in [glyph, joint]:

        def check(value):
            if isinstance(value, dict):
                if {"path", "sha256"} <= value.keys():
                    ui.ref(value, output.parent)
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        check(report)
    ui.require(not output.exists(), "Reference directory already exists")
    output.mkdir(parents=True)
    glyph_ref = output / "glyph-reference.json"
    p.new_json(
        glyph_ref,
        {
            "schema_version": 1,
            "status": "supplemental_glyph_bank_validated",
            "bank": ui.pin(bank_path),
            "validation": ui.pin(glyph_validation),
            "decoder": p.tools_identity()["glyph_decoder"],
        },
    )
    reference = {
        "schema_version": 2,
        "status": "reference_validated",
        "policy_version": p.POLICY_VERSION,
        "map": scene_document["map"],
        "build": p.BUILD,
        "review_mode": p.MODE,
        "policy": p.POLICY,
        "tools": p.tools_identity(),
        "scene_reference": ui.pin(scene),
        "glyph_reference": ui.pin(glyph_ref),
        "marker_validation": ui.pin(joint_validation),
        "manual_review_performed": False,
        "scope": "Only the reviewed map, native camera, font geometry and render profile.",
    }
    p.new_json(output / "reference.json", reference)
    p.validate_reference(output / "reference.json")
    return ui.pin(output / "reference.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ("scene-reference", "bank", "glyph-validation", "joint-validation", "output"):
        ap.add_argument("--" + name, type=Path, required=True)
    a = ap.parse_args()
    print(finalize(a.scene_reference, a.bank, a.glyph_validation, a.joint_validation, a.output))
