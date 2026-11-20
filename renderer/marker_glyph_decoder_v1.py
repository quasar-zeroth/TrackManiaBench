#!/usr/bin/env python3
"""Supplemental native marker glyph decoder with independent digit inference.

The calibrated reference bank models white fill and dark outline. This module has
no expected-label input during inference and cannot certify scene/time identity.
The caller must bind cached ROIs to actual video frames and serialized markers.
"""

import argparse
import io
import os
from pathlib import Path
import re
import numpy as np
import pipeline_common_v1 as c

BUILD = "native-glyph-compositing-decoder-1"
LIMITS = {
    "minimum_relative_digit_margin": 0.20,
    "maximum_anchor_mse": 1000.0,
    "maximum_best_digit_mse": 750.0,
    "minimum_layout_margin": 500.0,
    "minimum_overlay_improvement_fraction": 0.35,
}


def number(text):
    return re.search(r"\d+", text).group()


def starts(length):
    return [int(round((88 if length == 4 else 80) + 16.5 * j)) for j in range(length)]


def letters(length):
    return (
        [("C", 16, 37), ("L", 38, 53), ("I", 55, 60), ("P", 63, 81), ("m", 164, 187), ("s", 188, 202)]
        if length == 4
        else [("C", 8, 29), ("L", 30, 45), ("I", 47, 52), ("P", 54, 73), ("m", 172, 195), ("s", 196, 210)]
    )


def moments(a, b):
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    return np.stack(
        [a.mean((0, 3)), b.mean((0, 3)), (a * a).mean((0, 3)), (b * b).mean((0, 3)), (a * b).mean((0, 3))]
    )


def fit(stats):
    m = np.mean(stats, axis=0)
    x, y, x2, y2, xy = m
    slope = np.clip((xy - x * y + 3) / (np.maximum(x2 - x * x, 0) + 3), 0, 1)
    intercept = np.clip(y - slope * x, 0, 255 * (1 - slope))
    return slope, intercept


def error(model, stat):
    s, b = model
    x, y, x2, y2, xy = stat
    return float(np.mean(y2 + s * s * x2 + b * b - 2 * s * xy - 2 * b * y + 2 * s * b * x))


def templates(rows, holdout):
    groups = {}
    owners = {}
    for row, data in rows:
        if row["ordinal"] == holdout:
            continue
        for marker in row["markers"]:
            phase = marker["phase"]
            digits = number(marker["expected_text"])
            n = len(digits)
            ca = data[phase + "_clean"]
            cb = data[phase + "_clock"]
            for slot, (digit, start) in enumerate(zip(digits, starts(n))):
                key = ("digit", digit, n, slot % 2)
                groups.setdefault(key, []).append(
                    moments(ca[:, :, start : start + 17], cb[:, :, start : start + 17])
                )
                owners.setdefault(key, set()).add(row["ordinal"])
            for char, left, right in letters(n):
                key = ("letter", char, n)
                groups.setdefault(key, []).append(moments(ca[:, :, left:right], cb[:, :, left:right]))
                owners.setdefault(key, set()).add(row["ordinal"])
    fitted = {key: fit(stats) for key, stats in groups.items()}
    c.require(
        {k[1] for k in fitted if k[0] == "digit"} == set("0123456789"),
        "All ten digits are required; no expected-value pruning",
    )
    return fitted, owners


def predict(ca, cb, models):
    layouts = []
    for length in (4, 5):
        anchors = []
        for char, left, right in letters(length):
            key = ("letter", char, length)
            c.require(key in models, "Missing layout anchor")
            anchors.append(error(models[key], moments(ca[:, :, left:right], cb[:, :, left:right])))
        digitresults = []
        for slot, start in enumerate(starts(length)):
            ranked = []
            for digit in "0123456789":
                options = []
                for key, model in models.items():
                    if key[:2] != ("digit", digit):
                        continue
                    for shift in (-1, 0, 1):
                        stat = moments(
                            ca[:, :, start + shift : start + shift + 17],
                            cb[:, :, start + shift : start + shift + 17],
                        )
                        options.append((error(model, stat), list(key), shift))
                best = min(options, key=lambda x: x[0])
                ranked.append({"digit": digit, "mse": best[0], "template_group": best[1], "offset": best[2]})
            ranked.sort(key=lambda x: x["mse"])
            digitresults.append(
                {
                    "winner": ranked[0]["digit"],
                    "absolute_margin": ranked[1]["mse"] - ranked[0]["mse"],
                    "relative_margin": (ranked[1]["mse"] - ranked[0]["mse"]) / max(ranked[1]["mse"], 1),
                    "ranking": ranked,
                }
            )
        candidate = "CLIP " + "".join(x["winner"] for x in digitresults) + " ms"
        # Layout comes only from fixed CLIP/ms anchors, independent of any target.
        layouts.append(
            {
                "digits": length,
                "anchor_mse": float(np.mean(anchors)),
                "candidate": candidate,
                "digit_results": digitresults,
            }
        )
    layouts.sort(key=lambda x: x["anchor_mse"])
    return {
        "candidate": layouts[0]["candidate"],
        "layout_margin": layouts[1]["anchor_mse"] - layouts[0]["anchor_mse"],
        "layouts": layouts,
    }


def infer_roi(ca, cb, bank):
    ca = np.asarray(ca)
    cb = np.asarray(cb)
    c.require(
        ca.dtype == np.uint8
        and cb.dtype == np.uint8
        and ca.shape == cb.shape
        and ca.ndim == 4
        and ca.shape[1:] == (22, 220, 3)
        and 2 <= len(ca) <= 16,
        "Unexpected exact marker ROI shape/dtype",
    )
    raw = predict(ca, cb, bank["models"])
    best = raw["layouts"][0]
    reasons = []
    if best["anchor_mse"] > LIMITS["maximum_anchor_mse"]:
        reasons.append("prefix_suffix_not_recognized")
    if raw["layout_margin"] < LIMITS["minimum_layout_margin"]:
        reasons.append("ambiguous_digit_count")
    for j, digit in enumerate(best["digit_results"]):
        if digit["relative_margin"] < LIMITS["minimum_relative_digit_margin"]:
            reasons.append(f"ambiguous_digit_{j}")
        top = digit["ranking"][0]
        if top["mse"] > LIMITS["maximum_best_digit_mse"]:
            reasons.append(f"poor_glyph_fit_{j}")
        x = starts(best["digits"])[j] + top["offset"]
        stat = moments(ca[:, :, x : x + 17], cb[:, :, x : x + 17])
        xm, ym, x2, y2, xy = stat
        baseline = float(np.mean(y2 + x2 - 2 * xy))
        improvement = 1 - top["mse"] / max(baseline, 1.0)
        digit["no_overlay_mse"] = baseline
        digit["overlay_improvement_fraction"] = improvement
        if improvement < LIMITS["minimum_overlay_improvement_fraction"]:
            reasons.append(f"no_glyph_contrast_{j}")
    confident = not reasons
    return {
        "schema_version": 1,
        "build": BUILD,
        "decoded_text": raw["candidate"] if confident else None,
        "raw_candidate": raw["candidate"],
        "confident": confident,
        "margin": min(v["relative_margin"] for v in best["digit_results"]),
        "rejection_reasons": reasons,
        "diagnostics": raw,
        "limits": LIMITS,
        "expected_text_used_for_inference": False,
        "all_ten_digits_compared": True,
        "both_four_and_five_digit_layouts_compared": True,
        "bank": bank["manifest_pin"],
        "decoder": c.pin(Path(__file__)),
    }


def save_arrays(path, clean, clock):
    buffer = io.BytesIO()
    np.savez_compressed(buffer, clean=clean, clock=clock)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as f:
        f.write(buffer.getvalue())
        f.flush()
        os.fsync(f.fileno())
    c.sync_directory(path.parent)


def infer(clean_crops, clock_crops, first, last, bank, output=None, phase=None):
    """Inputs match v1's complete N×50×240×3 marker ROI arrays; inclusive frames."""
    a = np.asarray(clean_crops)
    b = np.asarray(clock_crops)
    c.require(
        type(first) is int
        and type(last) is int
        and 0 <= first <= last < len(a)
        and a.shape == b.shape
        and a.shape[1:] == (50, 240, 3),
        "Bad marker frame bounds or source crop dimensions",
    )
    ca = a[first : last + 1, 16:38, 10:230].copy()
    cb = b[first : last + 1, 16:38, 10:230].copy()
    report = infer_roi(ca, cb, bank)
    if output is not None:
        c.require(phase in ("start", "middle", "finish"), "Explicit marker phase required for saved evidence")
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        pixels = output / f"glyph-{phase}-roi.npz"
        save_arrays(pixels, ca, cb)
        report.update(
            phase=phase,
            first_visible_frame=first,
            last_visible_frame=last,
            roi_xyxy_in_50x240=[10, 16, 230, 38],
            pixels=c.pin(pixels),
        )
        file = output / f"glyph-{phase}-report.json"
        c.write_new(file, report)
        return {**report, "report": c.pin(file)}
    return report


def load_bank(path):
    path = Path(path).resolve(strict=True)
    doc = c.read(path)
    reference_pin = None
    if doc.get("status") == "supplemental_glyph_bank_validated":
        reference_pin = c.pin(path)
        c.verified(doc["validation"])
        path = c.verified(doc["bank"])
        doc = c.read(path)
    c.require(
        doc["schema_version"] == 1 and doc["build"] == BUILD and doc["limits"] == LIMITS,
        "Glyph bank policy differs",
    )
    c.require(doc["decoder"] == c.pin(Path(__file__)), "Glyph decoder source changed")
    c.verified(doc["training_cache_index"])
    models = {}
    with np.load(c.verified(doc["models"]), allow_pickle=False) as arrays:
        for i, ref in enumerate(doc["model_keys"]):
            key = tuple(ref)
            s = arrays[f"slope_{i}"]
            b = arrays[f"intercept_{i}"]
            c.require(
                s.shape == b.shape
                and s.ndim == 2
                and s.shape[0] == 22
                and np.isfinite(s).all()
                and np.isfinite(b).all(),
                "Corrupt glyph model",
            )
            models[key] = (s.copy(), b.copy())
    c.require({k[1] for k in models if k[0] == "digit"} == set("0123456789"), "Missing candidate digit class")
    return {"models": models, "manifest": doc, "manifest_pin": c.pin(path), "reference_pin": reference_pin}


def verify_report(report, bank):
    """Recompute every score from small immutable ROI bytes, no video decode."""
    if isinstance(report, (str, Path)):
        report = c.read(report)
    if isinstance(report, dict) and set(report) >= {"path", "sha256"}:
        report = c.read_verified(report)
    report = dict(report)
    report.pop("report", None)
    c.require(
        report["bank"] == bank["manifest_pin"] and report["decoder"] == c.pin(Path(__file__)),
        "Glyph proof tool or bank mismatch",
    )
    with np.load(c.verified(report["pixels"]), allow_pickle=False) as z:
        ca = z["clean"]
        cb = z["clock"]
    c.require(
        report["last_visible_frame"] - report["first_visible_frame"] + 1 == len(ca)
        and report["first_visible_frame"] >= 0,
        "Cached glyph frame bounds differ",
    )
    c.require(
        report["phase"] in ("start", "middle", "finish")
        and report["roi_xyxy_in_50x240"] == [10, 16, 230, 38],
        "Glyph cache phase/ROI differs",
    )
    expected = infer_roi(ca, cb, bank)
    for key, value in expected.items():
        c.require(report[key] == value, "Glyph score/decision changed: " + key)
    return report


def build_bank(cache_path, output):
    c.require(not output.exists(), "Glyph bank destination exists")
    index = c.read(cache_path)
    rows = []
    c.require(
        len(index["rows"]) >= 2 and len({r["ordinal"] for r in index["rows"]}) == len(index["rows"]),
        "At least two unique manually reviewed fitting runs required",
    )
    for row in index["rows"]:
        c.verified(row["historical_review"])
        c.verified(row["baseline"])
        c.verified(row["draft"])
        with np.load(c.verified(row["cache"]), allow_pickle=False) as z:
            data = {k: z[k] for k in z.files}
        rows.append((row, data))
    fitted, owners = templates(rows, None)
    output.mkdir(parents=True)
    keys = sorted(fitted)
    arrays = {}
    for i, key in enumerate(keys):
        arrays[f"slope_{i}"], arrays[f"intercept_{i}"] = fitted[key]
    file = output / "models.npz"
    buffer = io.BytesIO()
    np.savez_compressed(buffer, **arrays)
    with file.open("xb") as f:
        f.write(buffer.getvalue())
        f.flush()
        os.fsync(f.fileno())
    doc = {
        "schema_version": 1,
        "build": BUILD,
        "limits": LIMITS,
        "decoder": c.pin(Path(__file__)),
        "training_cache_index": c.pin(cache_path),
        "training_ordinals": sorted(r["ordinal"] for r in index["rows"]),
        "models": c.pin(file),
        "model_keys": [list(k) for k in keys],
        "model_training_ordinals": [sorted(owners[k]) for k in keys],
        "threshold_scope": "Fitted to reviewed runs; leave-one-run-out and separate untouched holdout validation are required.",
        "inference_conditioned_on_expected_text": False,
        "training_labels_from_existing_human_review": True,
    }
    c.write_new(output / "bank.json", doc)
    return doc


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("build-bank")
    q.add_argument("--cache", type=Path, required=True)
    q.add_argument("--output", type=Path, required=True)
    q = sub.add_parser("verify-report")
    q.add_argument("report", type=Path)
    q.add_argument("--bank", type=Path, required=True)
    a = p.parse_args()
    if a.cmd == "build-bank":
        print(c.encoded(build_bank(a.cache, a.output)).decode())
    else:
        print(c.encoded(verify_report(a.report, load_bank(a.bank))).decode())


if __name__ == "__main__":
    main()
