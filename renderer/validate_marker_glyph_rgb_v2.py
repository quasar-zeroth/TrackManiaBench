#!/usr/bin/env python3
"""Validate unchanged glyph inference against corrected production RGB geometry."""

import argparse
import io
from pathlib import Path
import re
import time
import numpy as np
import marker_glyph_decoder_v1 as g
import marker_glyph_cache_rgb_v2 as crop

c = g.c
ROOT = Path(__file__).resolve().parents[1]


def write_fold(folder, models, owners, index, heldout):
    folder.mkdir(parents=True)
    keys = sorted(models)
    arrays = {}
    for i, key in enumerate(keys):
        arrays[f"slope_{i}"], arrays[f"intercept_{i}"] = models[key]
    buffer = io.BytesIO()
    np.savez_compressed(buffer, **arrays)
    (folder / "models.npz").write_bytes(buffer.getvalue())
    doc = {
        "schema_version": 1,
        "build": g.BUILD,
        "limits": g.LIMITS,
        "decoder": c.pin(Path(g.__file__)),
        "models": c.pin(folder / "models.npz"),
        "model_keys": [list(k) for k in keys],
        "model_training_ordinals": [sorted(owners[k]) for k in keys],
        "training_cache_index": c.pin(index),
        "purpose": "leave_one_run_out_validation_only",
        "held_out_ordinal": heldout,
        "training_ordinals": sorted(set().union(*owners.values())),
    }
    c.require(heldout not in doc["training_ordinals"], "Held-out leakage")
    c.write_new(folder / "bank.json", doc)
    return g.load_bank(folder / "bank.json")


def save_report(ca, cb, first, last, bank, output, phase):
    output.mkdir(parents=True, exist_ok=True)
    report = g.infer_roi(ca, cb, bank)
    pixels = output / f"glyph-{phase}-roi.npz"
    g.save_arrays(pixels, ca, cb)
    report.update(
        phase=phase,
        first_visible_frame=first,
        last_visible_frame=last,
        roi_xyxy_in_50x240=[10, 16, 230, 38],
        pixels=c.pin(pixels),
    )
    file = output / f"glyph-{phase}-report.json"
    c.write_new(file, report)
    g.verify_report(c.pin(file), bank)
    return report, c.pin(file)


def wrong_labels(text, phases):
    digits = re.search(r"\d+", text).group()
    values = set(phases)
    for i in range(len(digits)):
        for d in "0123456789":
            values.add("CLIP " + digits[:i] + d + digits[i + 1 :] + " ms")
    values.discard(text)
    return sorted(values)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--bank", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--holdouts", type=Path, required=True)
    a = ap.parse_args()
    c.require(not a.output.exists(), "Validation exists")
    a.output.mkdir(parents=True)
    index = c.read(a.cache)
    rows = []
    started = time.monotonic()
    for row in index["rows"]:
        with np.load(c.verified(row["cache"]), allow_pickle=False) as z:
            rows.append((row, {k: z[k] for k in z.files}))
    fullbank = g.load_bank(a.bank)
    cases = []
    negatives = []
    confidentwrong = []
    quarantines = []
    wrongtests = 0
    for row, data in rows:
        ordinal = row["ordinal"]
        folder = a.output / f"rank{ordinal:03d}"
        models, owners = g.templates(rows, ordinal)
        bank = write_fold(folder / "fold-bank", models, owners, a.cache, ordinal)
        markers = []
        for marker in row["markers"]:
            phase = marker["phase"]
            ca = data[phase + "_clean"]
            cb = data[phase + "_clock"]
            first = marker["first_visible_frame"]
            last = marker["last_visible_frame"]
            expected = marker["expected_text"]
            report, ref = save_report(ca, cb, first, last, bank, folder / phase, phase)
            item = {
                "phase": phase,
                "expected_text": expected,
                "inference": report,
                "inference_report": ref,
                "exact": report["decoded_text"] == expected,
            }
            if report["confident"] and report["decoded_text"] != expected:
                confidentwrong.append({"ordinal": ordinal, "phase": phase})
            if not report["confident"]:
                quarantines.append(
                    {
                        "ordinal": ordinal,
                        "phase": phase,
                        "raw_candidate": report["raw_candidate"],
                        "reasons": report["rejection_reasons"],
                    }
                )
            wrongs = wrong_labels(expected, [x["expected_text"] for x in row["markers"]])
            wrongtests += len(wrongs)
            item["wrong_string_or_phase_labels"] = wrongs
            item["wrong_string_acceptances"] = [
                s for s in wrongs if report["confident"] and report["decoded_text"] == s
            ]
            markers.append(item)
            # Every LOO phase gets actual no-marker and wrong-view controls. Additional
            # corruption controls cover all phases of each difficult finish source.
            controls = {
                "no_marker": (ca, ca),
                "wrong_view_mirrored": (ca[:, :, ::-1].copy(), cb[:, :, ::-1].copy()),
            }
            if True:
                rng = np.random.default_rng(ordinal * 10 + len(phase))
                controls["corrupt_noise"] = (ca, rng.integers(0, 256, cb.shape, dtype=np.uint8))
                damaged = cb.copy()
                length = len(re.search(r"\d+", expected).group())
                left = g.starts(length)[length // 2]
                damaged[:, :, left : left + 17] = ca[:, :, left : left + 17]
                controls["missing_digit"] = (ca, damaged)
            for kind, (na, nb) in controls.items():
                result, rref = save_report(
                    na, nb, first, last, bank, folder / ("negative-" + phase + "-" + kind), phase
                )
                negatives.append(
                    {
                        "ordinal": ordinal,
                        "phase": phase,
                        "kind": kind,
                        "synthetic_pixels": True,
                        "rejected": not result["confident"],
                        "inference_report": rref,
                        "decoded_text": result["decoded_text"],
                    }
                )
        file = folder / "case.json"
        c.write_new(
            file,
            {
                "ordinal": ordinal,
                "run_id": row["run_id"],
                "held_out_run_excluded": True,
                "fold_bank": bank["manifest_pin"],
                "markers": markers,
                "baseline": row["baseline"],
            },
        )
        cases.append({"ordinal": ordinal, "report": c.pin(file)})
        print(
            {
                "completed_loo": ordinal,
                "confident_wrong_so_far": len(confidentwrong),
                "negative_failures_so_far": sum(not n["rejected"] for n in negatives),
            },
            flush=True,
        )
    # Holdout pulse records must be excluded from every fitting bank.
    untouched = []
    holdouts = c.read(a.holdouts)
    c.require(
        holdouts and len({x["ordinal"] for x in holdouts}) == len(holdouts),
        "Unique untouched holdouts are required",
    )
    for heldout in holdouts:
        ordinal = heldout["ordinal"]
        source = c.verified(heldout["pulses"])
        c.require(
            ordinal not in fullbank["manifest"]["training_ordinals"], "Holdout leaked into glyph fitting bank"
        )
        doc = c.read(source)
        ranges = [(m["first_visible_frame"], m["last_visible_frame"]) for m in doc["pulses"]]
        ca = crop.frames(c.verified(doc["clean_native_video"]), ranges)
        cb = crop.frames(c.verified(doc["diagnostic_video"]), ranges)
        offset = 0
        markers = []
        for marker, (first, last) in zip(doc["pulses"], ranges):
            n = last - first + 1
            phase = marker["phase"]
            report, ref = save_report(
                ca[offset : offset + n],
                cb[offset : offset + n],
                first,
                last,
                fullbank,
                a.output / f"untouched-pilot{ordinal:02d}" / phase,
                phase,
            )
            offset += n
            markers.append(
                {
                    "phase": phase,
                    "expected_text": marker["native_text"],
                    "inference": report,
                    "inference_report": ref,
                    "exact": report["decoded_text"] == marker["native_text"],
                }
            )
        file = a.output / f"untouched-pilot{ordinal:02d}" / "case.json"
        c.write_new(
            file,
            {
                "ordinal": ordinal,
                "source_pulse_review": c.pin(source),
                "excluded_from_fitting": True,
                "markers": markers,
            },
        )
        untouched.append({"ordinal": ordinal, "report": c.pin(file)})
        print(
            {
                "untouched_pilot": ordinal,
                "results": [(m["phase"], m["inference"]["decoded_text"], m["exact"]) for m in markers],
            },
            flush=True,
        )
    exact = sum(m["exact"] for r in cases for m in c.read_verified(r["report"])["markers"])
    untouchedexact = sum(m["exact"] for r in untouched for m in c.read_verified(r["report"])["markers"])
    report = {
        "schema_version": 1,
        "status": "supplemental_glyph_validation_completed",
        "decoder": c.pin(Path(g.__file__)),
        "validator": c.pin(Path(__file__)),
        "crop_extractor": c.pin(Path(crop.__file__)),
        "bank": c.pin(a.bank),
        "training_cache": c.pin(a.cache),
        "limits": g.LIMITS,
        "cases": cases,
        "loo_cases": len(cases) * 3,
        "loo_confident_exact": exact,
        "loo_confident_wrong": confidentwrong,
        "loo_quarantines": quarantines,
        "negative_controls": negatives,
        "all_negative_images_rejected": all(n["rejected"] for n in negatives),
        "wrong_string_phase_tests": wrongtests,
        "all_wrong_string_phase_labels_rejected": all(
            not m["wrong_string_acceptances"] for r in cases for m in c.read_verified(r["report"])["markers"]
        ),
        "untouched_cases": untouched,
        "untouched_marker_count": len(untouched) * 3,
        "untouched_exact": untouchedexact,
        "seconds": time.monotonic() - started,
        "threshold_fitting_scope": "Fixed decoder limits with leave-one-run-out fitting checks and separately supplied holdouts. Holdouts must not be selected after examining failures.",
        "limitations": [
            "Finite closed-font validation does not prove all future background conditions. Unconfident predictions must quarantine or defer to independently passing primary OCR.",
            "Glyphs certify text appearance only; serialized identity, actual source clocks, video timing, paired-scene equality and visibility remain separately required.",
        ],
    }
    c.write_new(a.output / "validation.json", report)
    print(
        {
            "loo_exact": exact,
            "loo_total": len(cases) * 3,
            "loo_wrong": confidentwrong,
            "loo_quarantine": quarantines,
            "negative_failures": [n for n in negatives if not n["rejected"]],
            "untouched_exact": untouchedexact,
        },
        flush=True,
    )


if __name__ == "__main__":
    main()
