#!/usr/bin/env python3
"""Isolated cropped marker OCR research; never accepts or changes a dataset."""

import argparse
from collections import Counter
from pathlib import Path
import re
import subprocess
import sys
import time
import numpy as np
from PIL import Image
import automatic_video_policy_v1 as frozen
import pipeline_common_v1 as c


def recognize(texts, minimum=2):
    """Infer a candidate before inspecting any expected label."""
    labels = [frozen.normalize_text(t) for t in texts]
    labels = [t for t in labels if re.fullmatch(r"clip\d+ms", t)]
    counts = Counter(labels)
    ranked = counts.most_common()
    if not ranked:
        return {"candidate": None, "counts": {}, "parseable": 0}
    label, n = ranked[0]
    runner = ranked[1][1] if len(ranked) > 1 else 0
    ok = n >= minimum and n > runner and n / len(labels) >= 0.5
    return {"candidate": label if ok else None, "counts": dict(counts), "parseable": len(labels)}


def crop_frames(video, first, last):
    # ffmpeg decodes sequentially internally, emits only requested small ROIs.
    # At most13*50*240*3 bytes are held here, never full-video RGB.
    vf = f"select=between(n\\,{first}\\,{last}),crop=240:50:200:155"
    raw = subprocess.run(
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
            "-vf",
            vf,
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
        check=True,
    )
    c.require(not raw.stderr.strip(), "Crop decode warning/error")
    expected = (last - first + 1) * 50 * 240 * 3
    c.require(len(raw.stdout) == expected, "Wrong cropped frame count")
    return np.frombuffer(raw.stdout, np.uint8).reshape(last - first + 1, 50, 240, 3)


def masks(clean, clock):
    ca = clean.astype(np.int16)
    cb = clock.astype(np.int16)
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
            yield f"{kind}-{t}", mask[:, 16:38, 10:230]


def probe_phase(clean, clock, first, last, ocr, folder, perframe):
    allviews = list(masks(clean, clock))
    reports = {}
    # Fixed bounded grid committed before the reviewed reference runs are examined.
    for consensus in (0.6, 0.8, 0.9, 1.0):
        views = []
        for name, mask in allviews:
            image = mask[1:-1].mean(axis=0) >= consensus
            text = ocr.text(image)
            p = folder / f"consensus-{consensus}-{name}.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(np.uint8(~image) * 255).save(p)
            views.append({"preprocessing": name, "text": text, "image": c.pin(p)})
        reports[f"consensus_{consensus}"] = {**recognize([v["text"] for v in views]), "views": views}
    if perframe:
        views = []
        framevotes = []
        for local, frame in enumerate(range(first, last + 1), 1):
            texts = []
            for name, mask in allviews:
                text = ocr.text(mask[local])
                texts.append(text)
                views.append({"frame_index": frame, "preprocessing": name, "text": text})
            vote = recognize(texts)
            framevotes.append({"frame_index": frame, **vote})
        # Count one vote per distinct decoded frame, never nine correlated
        # preprocessing views as nine temporally independent observations.
        reports["distinct_frame_votes"] = {
            **recognize([v["candidate"] or "" for v in framevotes]),
            "frame_votes": framevotes,
            "views": views,
        }
    boundary = []
    for local in (0, len(clean) - 1):
        views = [{"preprocessing": name, "text": ocr.text(mask[local])} for name, mask in allviews]
        boundary.append(
            {"relative_frame": local, "recognition": recognize([v["text"] for v in views]), "views": views}
        )
    return reports, boundary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--ordinals", type=int, nargs="*")
    p.add_argument("--per-frame", action="store_true")
    a = p.parse_args()
    c.require(not a.output.exists(), "Research output already exists")
    a.output.mkdir(parents=True)
    sources = {
        "prototype": c.pin(Path(__file__)),
        "policy": c.pin(Path(frozen.__file__)),
        "ocr_model": c.pin(frozen.ASSET),
        "ocr_library": c.pin(frozen.TESSERACT),
    }
    reports = []
    ocr = frozen.OCR()
    started = time.monotonic()
    try:
        for baseline in sorted(a.baseline.glob("rank*/regression.json")):
            old = c.read(baseline)
            ordinal = old["ordinal"]
            if a.ordinals and ordinal not in a.ordinals:
                continue
            review = c.read_verified(old["actual_historical_review"])
            draftpath = Path(old["actual_historical_review"]["path"]).parent / "review-draft.json"
            draft = c.read(draftpath)
            c.require(draft["ordinal"] == ordinal, "Historical identity mismatch")
            clean = c.read_verified(draft["clean_candidate"])
            clock = c.read_verified(draft["clock_candidate"])
            cv = c.verified(clean["video"])
            kv = c.verified(clock["video"])
            marker = next(v for v in old["markers"] if v["phase"] == "finish")
            first, last = marker["first_visible_frame"], marker["last_visible_frame"]
            ca = crop_frames(cv, first - 1, last + 1)
            cb = crop_frames(kv, first - 1, last + 1)
            folder = a.output / f"rank{ordinal:03d}"
            folder.mkdir()
            report, boundary = probe_phase(ca, cb, first, last, ocr, folder, a.per_frame)
            target = frozen.normalize_text(marker["expected_text"])
            number = int(re.search(r"\d+", target).group())
            wrongs = sorted(
                {f"clip{number + d}ms" for d in (-100, -20, -1, 1, 20, 100)}
                | {
                    frozen.normalize_text(v["expected_text"])
                    for v in old["markers"]
                    if v["phase"] != "finish"
                }
            )
            for value in report.values():
                value["expected_exact"] = value["candidate"] == target
                value["wrong_number_or_phase_acceptances"] = [s for s in wrongs if value["candidate"] == s]
            doc = {
                "schema_version": 1,
                "status": "prototype_research_only_no_acceptance",
                "ordinal": ordinal,
                "run_id": draft["run_id"],
                "baseline": c.pin(baseline),
                "draft": c.pin(draftpath),
                "actual_historical_review": old["actual_historical_review"],
                "videos": {"clean": clean["video"], "clock": clock["video"]},
                "expected_text": marker["expected_text"],
                "baseline_passed": marker["exact_text_verified"],
                "first_visible_frame": first,
                "last_visible_frame": last,
                "recognized_without_expected_label": True,
                "methods": report,
                "inactive_adjacent_frames": boundary,
                "negative_expected_strings": wrongs,
                "sources": sources,
                "maximum_retained_crop_bytes": int(ca.nbytes + cb.nbytes),
            }
            c.write_new(folder / "report.json", doc)
            reports.append(c.pin(folder / "report.json"))
            print({"ordinal": ordinal, "results": {k: v["candidate"] for k, v in report.items()}}, flush=True)
    finally:
        ocr.close()
    c.require(sources["policy"] == c.pin(Path(frozen.__file__)), "Policy source changed during research")
    docs = [c.read_verified(r) for r in reports]
    methods = {}
    for name in docs[0]["methods"]:
        methods[name] = {
            "exact": sum(d["methods"][name]["expected_exact"] for d in docs),
            "total": len(docs),
            "failed_ordinals": [d["ordinal"] for d in docs if not d["methods"][name]["expected_exact"]],
            "wrong_number_or_phase_acceptances": sum(
                len(d["methods"][name]["wrong_number_or_phase_acceptances"]) for d in docs
            ),
        }
    summary = {
        "schema_version": 1,
        "status": "prototype_research_complete_no_production_policy_change",
        "reports": reports,
        "methods": methods,
        "sources": sources,
        "seconds": time.monotonic() - started,
        "limitations": [
            "Historical corpus only; no acceptance or claimed independent production pass.",
            "Wrong expected labels test decision logic; no fabricated wrong text was inserted into a native video.",
            "Temporal consensus views are correlated; distinct_frame_votes counts each actual frame at most once.",
        ],
    }
    c.write_new(a.output / "summary.json", summary)
    print(methods, flush=True)


if __name__ == "__main__":
    main()
