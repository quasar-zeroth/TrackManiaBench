#!/usr/bin/env python3
"""Conservative visible-setting pixel equivalence; never accepts a video.

Compares fixed, readable fields with an actual manually reviewed native export.
A mismatch requires visual review. Preset selection and unexposed settings are
outside this proof; native scene, guarded UI sequence and media checks remain.
"""

import argparse
import json
import os
from pathlib import Path
import numpy as np
from PIL import Image
import native_ui_queue_partial_v1 as ui

ROOT = Path(__file__).resolve().parent.parent
REFERENCE_IMAGE = Path(os.environ.get("TRACKMANIABENCH_SETTINGS_IMAGE", "missing-settings-reference.png"))
REFERENCE_IMAGE_SHA = os.environ.get("TRACKMANIABENCH_SETTINGS_IMAGE_SHA256", "")
REFERENCE_REVIEW = Path(os.environ.get("TRACKMANIABENCH_SETTINGS_REVIEW", "missing-settings-review.json"))
REFERENCE_REVIEW_SHA = os.environ.get("TRACKMANIABENCH_SETTINGS_REVIEW_SHA256", "")
MAX_CHANNEL_DIFFERENCE = 1
FIELDS = {
    "shoot_dialog_title": (791, 215, 1136, 246),
    "dimensions_fps_format": (836, 393, 968, 514),
    "antialias": (1652, 248, 1781, 278),
    "persistence_duration": (1652, 296, 1781, 348),
    "sharpen_stereo_audio": (1652, 356, 1781, 437),
    "player_labels": (1652, 459, 1781, 488),
    "reflections": (1652, 549, 1781, 628),
    "video_mode_and_cq": (1652, 725, 1781, 830),
}
VISIBLE_VALUES = {
    "width": 640,
    "height": 360,
    "fps": 50,
    "file_format": "Webm",
    "antialias": "4x",
    "persistence": 0,
    "persistence_duration_seconds": 0,
    "sharpen": False,
    "stereo": "None",
    "audio_stream": False,
    "player_labels": False,
    "car_reflections": False,
    "subsample_reflections": False,
    "raycast_reflections": False,
    "video_mode": "CQ",
    "cq_level": 10,
}


def rgb(path):
    with Image.open(path) as im:
        ui.require(im.size == (1920, 1080), "Settings evidence has another screen size")
        return np.asarray(im.convert("RGB"), dtype=np.int16)


def compare(reference, candidate):
    ui.require(
        reference.shape == candidate.shape == (1080, 1920, 3), "Settings pixel array dimensions differ"
    )
    fields = []
    for name, (x0, y0, x1, y1) in FIELDS.items():
        delta = np.abs(candidate[y0:y1, x0:x1].astype(np.int16) - reference[y0:y1, x0:x1].astype(np.int16))
        maximum = int(delta.max())
        fields.append(
            {
                "field": name,
                "rectangle_xyxy": [x0, y0, x1, y1],
                "max_rgb_channel_difference": maximum,
                "mean_rgb_channel_difference": float(delta.mean()),
                "channels_exceeding_tolerance": int((delta > MAX_CHANNEL_DIFFERENCE).sum()),
                "equivalent": maximum <= MAX_CHANNEL_DIFFERENCE,
            }
        )
    return {"passed": all(f["equivalent"] for f in fields), "fields": fields}


def verify(candidate_path):
    candidate_path = Path(candidate_path).resolve(strict=True)
    c = ui.read(candidate_path)
    ui.require(
        c.get("status") == "native_video_candidate"
        and c["identity"]["size"] == [1920, 1080]
        and c["identity"]["origin"] == [0, 0],
        "Expected guarded native candidate identity",
    )
    reference = ui.ref({"path": str(REFERENCE_IMAGE), "sha256": REFERENCE_IMAGE_SHA}, ROOT)
    reviewed = ui.ref({"path": str(REFERENCE_REVIEW), "sha256": REFERENCE_REVIEW_SHA}, ROOT)
    r = ui.read(reviewed)
    ui.require(
        r["settings_reviewed"] is True and r["settings_observed"] == ui.SETTINGS,
        "Reference lacks its actual manual settings review",
    )
    reference_candidate = ui.ref(r["clock_candidate"], reviewed.parent)
    rc = ui.read(reference_candidate)
    ui.require(
        ui.ref(rc["settings_observation"], reference_candidate.parent) == reference,
        "Reviewed reference candidate settings differ",
    )
    observed = ui.ref(c["settings_observation"], candidate_path.parent)
    result = compare(rgb(reference), rgb(observed))
    return {
        "schema_version": 1,
        "status": "visible_settings_pixel_equivalent"
        if result["passed"]
        else "actual_settings_review_required",
        "review_mode": "automated_pixel_equivalence_to_manually_reviewed_reference",
        "candidate": ui.pin(candidate_path),
        "settings_observation": ui.pin(observed),
        "reference_image": ui.pin(reference),
        "reference_manual_review": ui.pin(reviewed),
        "classifier": ui.pin(Path(__file__)),
        "max_allowed_rgb_channel_difference": MAX_CHANNEL_DIFFERENCE,
        **result,
        "visible_values_if_equivalent": VISIBLE_VALUES if result["passed"] else None,
        "preset_selection_verified": False,
        "unexposed_settings_verified": False,
        "new_human_settings_review_claimed": False,
        "native_video_acceptance_asserted": False,
        "limits": [
            "Only listed visible value fields and dialog-title pixels are compared; the foreground scene and shoot basename are excluded.",
            "The1-level RGB allowance covers measured screenshot quantization; larger changes fail closed, including a single changed pixel.",
            "Quality preset selection is not visibly encoded by these fields. Existing reviewed setup/guarded sequence provenance remains required.",
            "Depth of field, motion blur and tile splitting remain unobserved. No disabled-state claim is inferred.",
            "Scene/readback identity, actual video properties, marker readability, completion and sampled/anomaly review remain separate requirements.",
        ],
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("candidate", type=Path)
    p.add_argument("output", type=Path)
    a = p.parse_args()
    ui.require(not a.output.exists(), "Refuse overwrite of settings equivalence evidence")
    result = verify(a.candidate)
    ui.common.write_json(a.output, result)
    print(json.dumps({"status": result["status"], "output": str(a.output), "passed": result["passed"]}))
    raise SystemExit(0 if result["passed"] else 2)
