#!/usr/bin/env python3
"""Deterministic image QA, distinct from historical explicit human review.

The closed scene policy is validated against immutable manually accepted runs.
Every candidate frame is decoded; unexplained images and OCR disagreements are
quarantined. This module never writes accepted videos or changes native scenes.
"""

from __future__ import annotations
import argparse
import csv
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import numpy as np
from PIL import Image
import production_video_audit_conditional_v2 as historical
import settings_pixels_v1 as settings

ui = historical.ui
ROOT = Path(__file__).resolve().parent.parent
MODE = "deterministic_decoded_image_policy_v1"
BUILD = "automatic-native-image-policy-1"
POLICY = {
    "scene_progress_radius": 0.035,
    "scene_reference_stride": 4,
    "world_mae_max": 30.0,
    "car_mae_max": 35.0,
    "world_and_car_joint_mae_max": 28.0,
    "near_black_mean_max": 3.0,
    "near_black_max_max": 16,
    "freeze_delta_max": 0.10,
    "freeze_run_frames": 5,
    "max_unobserved_prefix_ms": 40,
    "max_unobserved_suffix_ms": 40,
    "max_clean_clock_world_mae": 12.0,
    "max_clean_clock_car_mae": 12.0,
    "occlusion_world_mae_max": 50.0,
    "occlusion_car_mae_max": 52.0,
    "occlusion_joint_mae_max": 48.0,
    "occlusion_max_consecutive_frames": 3,
    "ocr_min_exact_votes": 2,
    "ocr_exact_fraction_of_parseable_min": 0.5,
    "marker_added_pixels_gt": 100,
    "completion_max_channel_difference": 1,
}
COMPLETION_ROIS = ((270, 800, 750, 845), (0, 1018, 640, 1080))
ASSET = Path(
    os.environ.get("TRACKMANIABENCH_TESSDATA", "/usr/share/tesseract-ocr/5/tessdata/eng.traineddata")
)
ASSET_SHA = os.environ.get("TRACKMANIABENCH_TESSDATA_SHA256", "")
TESSERACT = Path(
    os.environ.get("TRACKMANIABENCH_TESSERACT_LIBRARY", "/usr/lib/x86_64-linux-gnu/libtesseract.so.5")
).resolve()
CAMERA_AUDIT = Path(os.environ.get("TRACKMANIABENCH_CAMERA_AUDIT", "missing-camera-audit.json"))
CAMERA_AUDIT_SHA = os.environ.get("TRACKMANIABENCH_CAMERA_AUDIT_SHA256", "")


def new_json(path, value):
    path = Path(path)
    ui.require(not path.exists(), "Refuse overwrite: " + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    ui.common.write_json(path, value)


def tools_identity():
    return {
        "policy": ui.pin(Path(__file__)),
        "preparation": ui.pin(Path(historical.__file__)),
        "settings": ui.pin(Path(settings.__file__)),
        "ocr_model": ui.pin(ASSET),
        "ocr_library": ui.pin(TESSERACT),
    }


def frame_pts(video):
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
    pts = np.array([float(x["best_effort_timestamp_time"]) * 1000 for x in probe["frames"]])
    ui.require(
        len(pts) > 3 and pts[0] == 0 and np.all(np.abs(np.diff(pts) - 20) < 0.001),
        "Missing, reordered, duplicated or non-50FPS decoded timestamps",
    )
    return pts


def descriptor(rgb):
    image = Image.fromarray(rgb)
    world = np.asarray(image.crop((0, 0, 640, 150)).resize((16, 5), Image.BOX)).reshape(-1)
    road = np.asarray(image.crop((80, 205, 560, 350)).resize((16, 5), Image.BOX)).reshape(-1)
    car = np.asarray(image.crop((220, 210, 420, 340)).resize((10, 7), Image.BOX)).reshape(-1)
    return np.r_[world, road, car].astype(np.uint8)


WORLD = 480


def decode_features(video):
    """Streaming decode: retain descriptors and the small marker ROI only."""
    pts = frame_pts(video)
    features = []
    means = []
    maxima = []
    deltas = []
    hashes = []
    crops = []
    prior = None
    proc = subprocess.Popen(
        [
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
            "-vsync",
            "0",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        for index in range(len(pts)):
            raw = proc.stdout.read(640 * 360 * 3)
            ui.require(len(raw) == 640 * 360 * 3, "Short or corrupt decoded frame")
            pixels = np.frombuffer(raw, np.uint8).reshape(360, 640, 3)
            features.append(descriptor(pixels))
            means.append(float(pixels.mean()))
            maxima.append(int(pixels.max()))
            roi = pixels[160:340, 200:440].astype(np.int16)
            deltas.append(float(np.abs(roi - prior).mean()) if prior is not None else 0.0)
            prior = roi
            hashes.append(hashlib.sha256(raw).hexdigest())
            crops.append(pixels[155:205, 200:440].copy())
        ui.require(not proc.stdout.read(1), "Extra decoded frames")
        errors = proc.stderr.read().decode(errors="replace")
        ui.require(proc.wait() == 0 and not errors.strip(), "Decode error: " + errors[:500])
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return {
        "pts": pts,
        "features": np.array(features),
        "means": np.array(means),
        "maxima": np.array(maxima),
        "deltas": np.array(deltas),
        "hashes": hashes,
        "crops": np.array(crops),
    }


def scene_scores(features, progress, bank, exclude_ordinal=None):
    scores = []
    matched = []
    for f, t in zip(features, progress):
        eligible = np.abs(bank["progress"] - t) <= POLICY["scene_progress_radius"]
        if exclude_ordinal is not None:
            eligible &= bank["ordinal"] != exclude_ordinal
        indices = np.flatnonzero(eligible)
        ui.require(len(indices) > 0, "No independently reviewed scene reference at progress")
        delta = np.abs(bank["features"][indices].astype(np.int16) - f.astype(np.int16))
        world = delta[:, :WORLD].mean(axis=1)
        car = delta[:, WORLD:].mean(axis=1)
        joint = (world + car) / 2
        regular = np.maximum.reduce(
            [
                world / POLICY["world_mae_max"],
                car / POLICY["car_mae_max"],
                joint / POLICY["world_and_car_joint_mae_max"],
            ]
        )
        j = int(np.argmin(regular))
        if regular[j] > 1 and "occlusion" in bank:
            occlusion = np.maximum.reduce(
                [
                    world / POLICY["occlusion_world_mae_max"],
                    car / POLICY["occlusion_car_mae_max"],
                    joint / POLICY["occlusion_joint_mae_max"],
                ]
            )
            occlusion = np.where(bank["occlusion"][indices], occlusion, np.inf)
            other = int(np.argmin(occlusion))
            if occlusion[other] <= 1:
                j = other
        scores.append([float(world[j]), float(car[j]), float(joint[j])])
        matched.append(int(indices[j]))
    return np.array(scores), matched


def image_decision(metrics):
    reasons = []
    first, last = metrics["first"], metrics["last"]
    n = len(metrics["pts"])
    if not 0 <= first <= last < n:
        return ["invalid_visibility_indices"]
    if not 0 <= metrics["pts"][first] - metrics["entity_start_ms"] <= 40:
        reasons.append("visible_start_outside_40ms")
    if metrics["pts"][last] > metrics["camera_end_ms"]:
        reasons.append("visibility_exceeds_camera_clip")
    next_time = metrics["pts"][last + 1] if last + 1 < n else metrics["pts"][last] + 20
    if max(0, metrics["finish_ms"] - (next_time - metrics["entity_start_ms"])) > 40:
        reasons.append("visible_finish_missing_more_than_40ms")
    if any(x > 1 for x in metrics["black_frames"]):
        reasons.append("unexpected_black_frame")
    visible = np.arange(first, last + 1)
    if any(metrics["means"][i] <= 3 or metrics["maxima"][i] <= 16 for i in visible):
        reasons.append("near_black_race_frame")
    static = historical.scanner.contiguous_groups(
        [int(i) for i in visible[1:] if metrics["deltas"][i] < 0.10]
    )
    if any(len(g) >= 5 for g in static):
        reasons.append("frozen_race_frames")
    scores = np.array(metrics["scene_scores"])
    if len(scores) != len(visible) or not np.isfinite(scores).all():
        reasons.append("incomplete_scene_comparison")
    else:
        in_race = np.array(
            [0 <= metrics["pts"][i] - metrics["entity_start_ms"] < metrics["finish_ms"] for i in visible]
        )
        if not scene_admission(
            scores[in_race],
            np.asarray(metrics.get("reviewed_occlusion_matches", [False] * len(scores)))[in_race],
            np.asarray(metrics.get("candidate_abrupt_neighborhood", [False] * len(scores)))[in_race],
        )[0]:
            reasons.append("unrecognized_chase_camera_scene_or_anomaly")
    if metrics["clean_clock_world_mae"] > 12 or metrics["clean_clock_car_mae"] > 12:
        reasons.append("clean_clock_scene_mismatch")
    if metrics.get("scanner_failures"):
        reasons.append("prepared_boundary_scan_ambiguous")
    return reasons


def scene_admission(scores, reviewed_occlusion_matches, candidate_abrupt_neighborhood):
    scores = np.asarray(scores)
    regular = (
        (scores[:, 0] <= POLICY["world_mae_max"])
        & (scores[:, 1] <= POLICY["car_mae_max"])
        & (scores[:, 2] <= POLICY["world_and_car_joint_mae_max"])
    )
    supported = (
        (scores[:, 0] <= POLICY["occlusion_world_mae_max"])
        & (scores[:, 1] <= POLICY["occlusion_car_mae_max"])
        & (scores[:, 2] <= POLICY["occlusion_joint_mae_max"])
    )
    supported &= np.asarray(reviewed_occlusion_matches, dtype=bool) & np.asarray(
        candidate_abrupt_neighborhood, dtype=bool
    )
    exceptions = np.flatnonzero(~regular).tolist()
    groups = historical.scanner.contiguous_groups(exceptions)
    for group in groups:
        first, last = group[0], group[-1]
        if (
            len(group) > POLICY["occlusion_max_consecutive_frames"]
            or first == 0
            or last == len(scores) - 1
            or not supported[group].all()
        ):
            return False, exceptions
        if not regular[first - 1] or not regular[last + 1]:
            return False, exceptions
    return True, exceptions


class OCR:
    def __init__(self):
        ui.require(ui.common.sha256(ASSET) == ASSET_SHA, "OCR model changed")
        os.environ.setdefault("OMP_THREAD_LIMIT", "1")
        self.lib = ctypes.CDLL(str(TESSERACT))
        l = self.lib
        l.TessBaseAPICreate.restype = ctypes.c_void_p
        for name, args, ret in [
            ("TessBaseAPIInit3", [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p], ctypes.c_int),
            ("TessBaseAPISetPageSegMode", [ctypes.c_void_p, ctypes.c_int], None),
            ("TessBaseAPISetVariable", [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p], ctypes.c_int),
            (
                "TessBaseAPISetImage",
                [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int],
                None,
            ),
            ("TessBaseAPIGetUTF8Text", [ctypes.c_void_p], ctypes.c_void_p),
            ("TessDeleteText", [ctypes.c_void_p], None),
            ("TessBaseAPIDelete", [ctypes.c_void_p], None),
        ]:
            f = getattr(l, name)
            f.argtypes = args
            f.restype = ret
        self.api = l.TessBaseAPICreate()
        ui.require(
            l.TessBaseAPIInit3(self.api, str(ASSET.parent).encode(), b"eng") == 0, "OCR initialization failed"
        )
        l.TessBaseAPISetPageSegMode(self.api, 7)
        l.TessBaseAPISetVariable(self.api, b"user_defined_dpi", b"300")

    def text(self, mask):
        pixels = np.asarray(Image.fromarray(np.uint8(~mask) * 255).resize((880, 88), Image.BICUBIC)).copy()
        self.lib.TessBaseAPISetImage(self.api, pixels.ctypes.data, 880, 88, 1, 880)
        ptr = self.lib.TessBaseAPIGetUTF8Text(self.api)
        try:
            return ctypes.string_at(ptr).decode().strip()
        finally:
            self.lib.TessDeleteText(ptr)

    def close(self):
        self.lib.TessBaseAPIDelete(self.api)


def normalize_text(text):
    return re.sub(r"\s+", "", text).lower()


def marker_text_decision(expected, texts):
    target = normalize_text(expected)
    valid = [normalize_text(s) for s in texts if re.fullmatch(r"clip\d+ms", normalize_text(s))]
    exact = sum(s == target for s in valid)
    competing = max((valid.count(label) for label in set(valid) if label != target), default=0)
    return bool(
        exact >= POLICY["ocr_min_exact_votes"]
        and exact > competing
        and exact / max(1, len(valid)) >= POLICY["ocr_exact_fraction_of_parseable_min"]
    )


def marker_checks(clean, clock, pts, pulse, output):
    a, b = clean.astype(np.int16), clock.astype(np.int16)
    added = ((b.min(axis=3) > 190) & ((b - a).mean(axis=3) > 70)).sum(axis=(1, 2))
    reports = []
    ocr = OCR()
    try:
        for p in pulse["pulses"]:
            start, end = p["serialized_clip_interval_ms"]
            active = np.flatnonzero((pts >= start - 60) & (pts <= end + 60) & (added > 100))
            ui.require(
                len(active) > 0 and list(active) == list(range(int(active[0]), int(active[-1]) + 1)),
                "Noncontiguous diagnostic marker",
            )
            first, last = int(active[0]), int(active[-1])
            ui.require(
                first == p["first_visible_frame"]
                and last == p["last_visible_frame"]
                and 0 <= pts[first] - start <= 20
                and 0 <= pts[last] - end <= 20,
                "Marker timing changed",
            )
            ca, cb = a[first : last + 1], b[first : last + 1]
            texts = []
            refs = []
            for kind, thresholds in [
                ("threshold", (170, 180, 190)),
                ("diff", (0.3, 0.5, 0.7)),
                ("alpha", (0.3, 0.5, 0.7)),
            ]:
                for t in thresholds:
                    if kind == "threshold":
                        mask = cb.min(axis=3) > t
                    elif kind == "diff":
                        mask = (cb.min(axis=3) > 160) & ((cb - ca).mean(axis=3) > t * 100)
                    else:
                        denom = 255 - ca
                        ratio = (cb - ca) / np.maximum(denom, 1)
                        mask = (np.max(np.where(denom > 50, ratio, 0), axis=3) > t) & (cb.min(axis=3) > 160)
                    consensus = np.mean(mask, axis=0)[16:38, 10:230] >= 0.6
                    texts.append(ocr.text(consensus))
                    filename = output / f"marker-{p['phase']}-{kind}-{str(t).replace('.', '_')}.png"
                    Image.fromarray(np.uint8(~consensus) * 255).save(filename)
                    refs.append(ui.pin(filename))
            reports.append(
                {
                    "phase": p["phase"],
                    "expected_text": p["native_text"],
                    "ocr_texts": texts,
                    "exact_text_verified": marker_text_decision(p["native_text"], texts),
                    "first_visible_frame": first,
                    "last_visible_frame": last,
                    "onset_quantization_ms": float(pts[first] - start),
                    "evidence_files": refs,
                }
            )
    finally:
        ocr.close()
    allowed = np.zeros(len(pts), dtype=bool)
    for p in pulse["pulses"]:
        start, end = p["serialized_clip_interval_ms"]
        allowed |= (pts >= start) & (pts <= end + 20)
    ui.require(
        not np.any((added > 100) & ~allowed), "Unexpected diagnostic text outside serialized marker intervals"
    )
    return reports


def completion_pixels(path):
    a = settings.rgb(path)
    return np.concatenate([a[y0:y1, x0:x1].reshape(-1) for x0, y0, x1, y1 in COMPLETION_ROIS]).astype(
        np.uint8
    )


def verify_camera(timeline):
    c = timeline["camera"]
    ui.require(
        c.get("class") == "CGameCtnMediaBlockCameraGame"
        and c.get("clip_entity_id") == 1
        and c.get("game_camera") == "External",
        "Actual native camera is not the admitted external chase camera",
    )


def baseline_entries(accepted, audit_root):
    entries = []
    for result in sorted(accepted.glob("*/result.json")):
        record = ui.read(result)
        ordinal = record["run"]["ordinal"]
        folder = audit_root / record["run"]["run_id"]
        observation = folder / "observation-window-verification.json"
        if not (folder / "acceptance-receipt.json").exists():
            continue
        obs = ui.read(observation)
        ui.require(
            obs.get("reviewed_contiguous_race_visibility") is True,
            "Reference lacks actual historical visibility review",
        )
        ui.require(
            obs["accepted_result"] == ui.pin(result)
            and obs["accepted_video"] == ui.pin(result.parent / "video.webm"),
            "Historical reference accepted identity changed",
        )
        entry = {
            "ordinal": ordinal,
            "run_id": record["run"]["run_id"],
            "record_time_ms": record["run"]["record_time_ms"],
            "map": record["map"],
            "accepted_result": ui.pin(result),
            "video": ui.pin(result.parent / "video.webm"),
            "observation": ui.pin(observation),
            "first": obs["first_usable_frame_index"],
            "last": obs["last_usable_frame_index"],
            "entity_start_ms": obs["first_usable_video_pts_ms"] - obs["first_usable_nominal_race_ms"],
        }
        review = folder / "explicit-review.json"
        r = ui.read(review)
        ui.require(
            r.get("sampled_continuity_reviewed") is True and r.get("verified_by"),
            "Reference lacks explicit actual review",
        )
        entry["explicit_review"] = ui.pin(review)
        entries.append(entry)
    ui.require(
        len(entries) >= 2, "Reference requires at least two independently reviewed runs for holdout checks"
    )
    ui.require(all(e["map"] == entries[0]["map"] for e in entries), "Scene reference spans different maps")
    return sorted(entries, key=lambda x: x["ordinal"])


def build_reference(accepted, audit_root, output):
    ui.require(not output.exists(), "Reference destination exists")
    output.mkdir(parents=True)
    camera = ui.ref({"path": str(CAMERA_AUDIT), "sha256": CAMERA_AUDIT_SHA}, ROOT)
    entries = baseline_entries(accepted, audit_root)
    all_features = []
    all_progress = []
    all_ordinals = []
    all_occlusion = []
    decoded = {}
    completion = []
    for entry in entries:
        n = entry["ordinal"]
        data = decode_features(ui.ref(entry["video"], ROOT))
        first, last = entry["first"], entry["last"]
        progress = (data["pts"][first : last + 1] - entry["entity_start_ms"]) / entry["record_time_ms"]
        features = data["features"][first : last + 1]
        np.savez_compressed(output / f"rank{n:03d}-all-features.npz", features=features, progress=progress)
        decoded[n] = (features, progress)
        flags = []
        selected = np.arange(0, len(features), POLICY["scene_reference_stride"])
        selected = np.unique(
            np.r_[
                selected,
                len(features) - 1,
                [i - first for i in range(first, last + 1) if any(abs(i - j) <= 2 for j in flags)],
            ]
        )
        if entry.get("explicit_review"):
            r = ui.read(ui.ref(entry["explicit_review"], ROOT))
            scan = ui.read(ui.ref(r["scan"], ROOT))
            flags = scan["abrupt_frames"]
            # Preserve every actually reviewed flagged neighborhood in the bank.
            selected = np.unique(
                np.r_[
                    selected,
                    [i - first for i in range(first, last + 1) if any(abs(i - j) <= 2 for j in flags)],
                ]
            )
        all_features.extend(features[selected])
        all_progress.extend(progress[selected])
        all_ordinals.extend([n] * len(selected))
        all_occlusion.extend([any(abs(int(i) + first - j) <= 2 for j in flags) for i in selected])
        entry["actual_abrupt_frames"] = flags
        if entry.get("explicit_review"):
            r = ui.read(ui.ref(entry["explicit_review"], ROOT))
            for role in ("clean", "clock"):
                cp = ui.ref(r[role + "_candidate"], ROOT)
                c = ui.read(cp)
                completion.append(completion_pixels(ui.ref(c["completion_observation"], cp.parent)))
        print(json.dumps({"reference_decoded": n, "frames": len(data["pts"])}), flush=True)
    bank = {
        "features": np.array(all_features),
        "progress": np.array(all_progress),
        "ordinal": np.array(all_ordinals),
        "completion": np.array(completion),
        "occlusion": np.array(all_occlusion, dtype=bool),
    }
    np.savez_compressed(output / "reference-bank.npz", **bank)
    holdouts = []
    for entry in entries:
        scores, matches = scene_scores(*decoded[entry["ordinal"]], bank, exclude_ordinal=entry["ordinal"])
        flags = entry["actual_abrupt_frames"]
        if entry["ordinal"] <= 5:
            # Original five were reviewed at least as thoroughly; their abrupt
            # neighborhoods are independently derived by saved frame deltas below.
            flags = entry.get("actual_abrupt_frames", [])
        near = [any(abs(i + entry["first"] - j) <= 4 for j in flags) for i in range(len(scores))]
        in_race = decoded[entry["ordinal"]][1] < 1
        passed, exceptions = scene_admission(
            scores[in_race],
            np.array([bool(bank["occlusion"][j]) for j in matches])[in_race],
            np.array(near)[in_race],
        )
        holdouts.append(
            {
                "ordinal": entry["ordinal"],
                "frames": len(scores),
                "max_world_car_joint_mae": scores.max(axis=0).tolist(),
                "occlusion_exception_frame_indices": [i + entry["first"] for i in exceptions],
                "passed": bool(passed),
            }
        )
    report = {
        "schema_version": 2,
        "review_mode": MODE,
        "build": BUILD,
        "status": "reference_validated" if all(x["passed"] for x in holdouts) else "reference_failed",
        "map": entries[0]["map"],
        "policy": POLICY,
        "tools": tools_identity(),
        "camera_audit": ui.pin(camera),
        "historical_references": entries,
        "bank": ui.pin(output / "reference-bank.npz"),
        "leave_one_run_out": holdouts,
        "manual_review_performed": False,
        "limits": [
            "Empirical scene identity within this frozen map/camera/profile, not general semantic recognition.",
            "Leave-one-run-out regression checks generalization across the reviewed reference runs; policy thresholds are fixed before future candidates.",
            "All novel or ambiguous diagnostics quarantine rather than invent a human review.",
        ],
    }
    new_json(output / "reference.json", report)
    return report


def validate_reference(path):
    r = ui.read(path)
    ui.require(
        r.get("status") == "reference_validated" and r.get("policy") == POLICY,
        "Image reference did not pass validation",
    )
    ui.require(r["tools"] == tools_identity(), "Automatic image policy or pinned dependencies changed")
    for key in ("camera_audit", "bank"):
        ui.ref(r[key], path.parent)
    for entry in r["historical_references"]:
        for key in ("accepted_result", "video", "observation", "explicit_review"):
            if key in entry:
                ui.ref(entry[key], path.parent)
    ui.require(
        len(r["historical_references"]) >= 2
        and len(r["leave_one_run_out"]) == len(r["historical_references"])
        and {x["ordinal"] for x in r["leave_one_run_out"]}
        == {x["ordinal"] for x in r["historical_references"]}
        and all(x["passed"] for x in r["leave_one_run_out"]),
        "Incomplete historical regression",
    )
    return r, {k: v for k, v in np.load(ui.ref(r["bank"], path.parent), allow_pickle=False).items()}


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
        markers = marker_checks(clean["crops"], clock["crops"], clean["pts"], pulse, output)
        new_json(
            output / "marker-identity.json",
            {
                "markers": markers,
                "method": "fixed_tesseract_4_consensus_9_binary_preprocessing_views",
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
    validate_reference(ui.ref(r["reference"], base))
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
    marks = ui.read(ui.ref(r["marker_identity"], base))["markers"]
    ui.require(
        len(marks) == 3
        and all(
            x["exact_text_verified"] and marker_text_decision(x["expected_text"], x["ocr_texts"])
            for x in marks
        ),
        "Marker text decision failed",
    )
    for m in marks:
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
    b = sub.add_parser("build-reference")
    for k in ("accepted", "audit-root", "output"):
        b.add_argument("--" + k, type=Path, required=True)
    e = sub.add_parser("evaluate")
    e.add_argument("draft", type=Path)
    for k in ("reference", "output", "accepted"):
        e.add_argument("--" + k, type=Path, required=True)
    v = sub.add_parser("verify")
    v.add_argument("record", type=Path)
    a = p.parse_args()
    if a.command == "build-reference":
        r = build_reference(a.accepted, a.audit_root, a.output)
    elif a.command == "evaluate":
        r = evaluate(a.draft, a.reference, a.output, a.accepted)
    else:
        r = verify_record(a.record)
    print(
        json.dumps(
            {
                "status": r["status"],
                "run_id": r.get("run_id"),
                "unresolved_issues": r.get("unresolved_issues", []),
            }
        )
    )
    raise SystemExit(0 if r["status"] in ("reference_validated", "automatic_checks_passed") else 2)


if __name__ == "__main__":
    main()
