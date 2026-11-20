#!/usr/bin/env python3
"""Save only hash-bound historical marker ROIs for isolated glyph research."""

import argparse
from pathlib import Path
import subprocess
import numpy as np
import marker_ocr_consensus_probe_v1 as p

c = p.c


def frames(video, indices):
    ranges = []
    for start, end in indices:
        ranges.append(f"between(n\\,{start}\\,{end})")
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
        "select=" + ("+".join(ranges)) + ",format=rgb24,crop=240:50:200:155",
        "-vsync",
        "0",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "-",
    ]
    proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    c.require(not proc.stderr.strip(), "Crop decoder reported error")
    n = sum(b - a + 1 for a, b in indices)
    c.require(len(proc.stdout) == n * 240 * 50 * 3, "Wrong marker frame count")
    return np.frombuffer(proc.stdout, np.uint8).reshape(n, 50, 240, 3)[:, 16:38, 10:230].copy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    c.require(not a.output.exists(), "Cache exists")
    a.output.mkdir(parents=True)
    rows = []
    for path in sorted(a.baseline.glob("rank*/regression.json")):
        d = c.read(path)
        c.verified(d["actual_historical_review"])
        dp = Path(d["actual_historical_review"]["path"]).parent / "review-draft.json"
        draft = c.read(dp)
        clean = c.read_verified(draft["clean_candidate"])
        clock = c.read_verified(draft["clock_candidate"])
        ranges = [(m["first_visible_frame"], m["last_visible_frame"]) for m in d["markers"]]
        ca = frames(c.verified(clean["video"]), ranges)
        cb = frames(c.verified(clock["video"]), ranges)
        data = {}
        offset = 0
        markers = []
        for m, (first, last) in zip(d["markers"], ranges):
            n = last - first + 1
            data[m["phase"] + "_clean"] = ca[offset : offset + n]
            data[m["phase"] + "_clock"] = cb[offset : offset + n]
            offset += n
            markers.append(
                {k: m[k] for k in ("phase", "expected_text", "first_visible_frame", "last_visible_frame")}
            )
        dst = a.output / f"rank{d['ordinal']:03d}.npz"
        np.savez_compressed(dst, **data)
        rows.append(
            {
                "ordinal": d["ordinal"],
                "run_id": draft["run_id"],
                "cache": c.pin(dst),
                "markers": markers,
                "baseline": c.pin(path),
                "historical_review": d["actual_historical_review"],
                "draft": c.pin(dp),
                "videos": {"clean": clean["video"], "clock": clock["video"]},
            }
        )
        print({"cached": d["ordinal"], "retained_pixels_shape": list(ca.shape)}, flush=True)
    c.write_new(
        a.output / "index.json",
        {
            "schema_version": 1,
            "scope": "historical_marker_pixels_only_not_a_dataset",
            "rows": rows,
            "decoder": c.pin(Path(__file__)),
            "frame_crop": [210, 171, 430, 193],
            "production_outputs_modified": False,
        },
    )


if __name__ == "__main__":
    main()
