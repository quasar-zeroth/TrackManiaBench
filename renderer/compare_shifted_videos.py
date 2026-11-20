#!/usr/bin/env python3
"""Measure same-render scene correspondence at a declared frame shift.

Photometric correspondence supports relative scene timing; it is not itself a
measurement of source race time. All videos and timestamps remain unchanged.
"""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFont


def pin(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def decode(video, indices):
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
    pts = [float(f["best_effort_timestamp_time"]) * 1000 for f in probe["frames"]]
    process = subprocess.Popen(
        [
            "ffmpeg",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(video),
            "-vsync",
            "0",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        stdout=subprocess.PIPE,
    )
    road = []
    cars = []
    selected = {}
    for index in range(len(pts)):
        raw = process.stdout.read(640 * 360 * 3)
        if len(raw) != 640 * 360 * 3:
            raise ValueError("Unexpected source frame count/size")
        pixels = np.frombuffer(raw, np.uint8).reshape(360, 640, 3)
        road.append(pixels[150:340, 80:560].copy())
        cars.append(pixels[210:340, 220:420].copy())
        if index in indices:
            selected[index] = pixels.copy()
    if process.stdout.read(1) or process.wait() != 0:
        raise ValueError("Decode failed or extra frames")
    return pts, road, cars, selected


def main(a, b, output, shift):
    output.mkdir(parents=True, exist_ok=True)

    def frame_count(video):
        probe = json.loads(
            subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-count_frames",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=nb_read_frames",
                    "-of",
                    "json",
                    str(video),
                ]
            )
        )
        return int(probe["streams"][0]["nb_read_frames"])

    lo = max(2, 2 - shift)
    hi = min(frame_count(a) - 2, frame_count(b) - shift - 3)
    if hi < lo:
        raise ValueError("No overlapping frames for the requested shift")
    selected = sorted(set(np.linspace(lo, hi, min(9, hi - lo + 1), dtype=int).tolist()))
    ap, ar, ac, ai = decode(a, selected)
    bp, br, bc, bi = decode(b, [i + shift for i in selected])
    offsets = list(range(shift - 2, shift + 3))
    rows = []
    for i in range(max(2, 2 - shift), len(ap) - 1):
        if i + max(offsets) >= len(bp):
            break
        row = {
            "original_frame": i,
            "original_pts_ms": ap[i],
            "shifted_frame": i + shift,
            "shifted_pts_ms": bp[i + shift],
            "actual_pts_shift_ms": bp[i + shift] - ap[i],
        }
        for offset in offsets:
            row["road_mae_shift_%d" % offset] = float(np.abs(ar[i].astype(np.int16) - br[i + offset]).mean())
            row["car_mae_shift_%d" % offset] = float(np.abs(ac[i].astype(np.int16) - bc[i + offset]).mean())
        row["road_best_shift"] = min(offsets, key=lambda n: row["road_mae_shift_%d" % n])
        row["car_best_shift"] = min(offsets, key=lambda n: row["car_mae_shift_%d" % n])
        rows.append(row)
    path = output / "all-overlap-frame-comparison.csv"
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    font = ImageFont.truetype(str(font_path), 21) if font_path.exists() else ImageFont.load_default()
    canvas = Image.new("RGB", (1280, len(selected) * 396))
    draw = ImageDraw.Draw(canvas)
    evidence = []
    for row, i in enumerate(selected):
        for x, pixels, label, index, pts in [
            (0, ai[i], "Original", i, ap[i]),
            (640, bi[i + shift], "Shifted", i + shift, bp[i + shift]),
        ]:
            canvas.paste(Image.fromarray(pixels), (x, row * 396 + 36))
            draw.text(
                (x + 8, row * 396 + 5), "%s frame%d PTS%.0fms" % (label, index, pts), fill="white", font=font
            )
            filename = output / ("%s-frame-%06d.png" % (label.lower(), index))
            Image.fromarray(pixels).save(filename)
            evidence.append({"role": label, "frame_index": index, "pts_ms": pts, **pin(filename)})
    canvas.save(output / "matched-scene-contact-sheet.png")
    summary = {
        "schema_version": 1,
        "status": "relative_scene_correspondence_measured_race_mapping_not_asserted",
        "original_video": pin(a),
        "shifted_video": pin(b),
        "declared_frame_shift": shift,
        "frames_compared": len(rows),
        "actual_pts_shift_range_ms": [
            min(r["actual_pts_shift_ms"] for r in rows),
            max(r["actual_pts_shift_ms"] for r in rows),
        ],
        "regions": {"car_xyxy": [220, 210, 420, 340], "near_road_xyxy": [80, 150, 560, 340]},
        "same_resolution_rgb_mean_absolute_error_0_to255": {
            region: {
                str(n): float(np.mean([r["%s_mae_shift_%d" % (region, n)] for r in rows])) for n in offsets
            }
            for region in ("road", "car")
        },
        "best_offset_frame_counts": {
            region: {str(n): sum(r[region + "_best_shift"] == n for r in rows) for n in offsets}
            for region in ("road", "car")
        },
        "per_frame_metrics": pin(path),
        "selected_original_pixel_frames": evidence,
        "all_native_frames_preserved": True,
        "race_clock_mapping_verified": False,
        "limits": [
            "Masks reduce sky, flags, billboards, and arch-clock changes but do not isolate every dynamic pixel.",
            "Lossy encoders and scene animation can differ between exports.",
            "This is relative scene correspondence; serialized clip/source trajectory evidence is needed for race timing.",
        ],
        "verifier_source": pin(__file__),
    }
    (output / "relative-scene-verification.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "selected_original_pixel_frames"}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path)
    parser.add_argument("shifted", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--shift-frames", type=int, required=True)
    args = parser.parse_args()
    main(args.original, args.shifted, args.output, args.shift_frames)
