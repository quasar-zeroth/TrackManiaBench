#!/usr/bin/env python3
"""Preserve native frame PTS, measure every frame, and extract review PNGs.

This produces evidence, not a visual/clock acceptance decision. Source video
bytes are never modified. PNGs contain decoded source pixels; contact-sheet
labels are outside those pixels. numpy and Pillow are optional audit-only deps.
"""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFont


def digest(path):
    with Path(path).open("rb") as stream:
        value = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def audit(video, output, requested):
    output.mkdir(parents=True, exist_ok=True)
    probe = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_streams",
                "-show_frames",
                "-of",
                "json",
                str(video),
            ]
        )
    )
    probe_path = output / "all-video-frame-metadata.json"
    write(probe_path, probe)
    frames = probe["frames"]
    stream = probe["streams"][0]
    width, height = stream["width"], stream["height"]
    process = subprocess.Popen(
        [
            "ffmpeg",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(video),
            "-map",
            "0:v:0",
            "-vsync",
            "0",
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            "-",
        ],
        stdout=subprocess.PIPE,
    )
    stats = []
    for index, frame in enumerate(frames):
        pixels = process.stdout.read(width * height)
        if len(pixels) != width * height:
            raise ValueError("Decoded frame count differs from ffprobe")
        array = np.frombuffer(pixels, dtype=np.uint8)
        stats.append(
            {
                "frame_index": index,
                "pts_seconds": frame["best_effort_timestamp_time"],
                "gray_min": int(array.min()),
                "gray_max": int(array.max()),
                "gray_mean": float(array.mean()),
                "fraction_gray_below_8": float((array < 8).mean()),
                "gray_sha256": hashlib.sha256(pixels).hexdigest(),
            }
        )
    if process.stdout.read(1) or process.wait() != 0:
        raise ValueError("Video decode failed or produced extra frames")
    stats_path = output / "every-frame-luma.csv"
    with stats_path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=stats[0].keys())
        writer.writeheader()
        writer.writerows(stats)
    all_black = [x["frame_index"] for x in stats if x["gray_max"] == 0]
    first_nonblack = next((x["frame_index"] for x in stats if x["gray_max"] > 0), None)
    default = [0, 1, 2, 3, 4, len(frames) // 2, len(frames) - 3, len(frames) - 2, len(frames) - 1]
    indices = sorted(set(requested or default))
    if any(i < 0 or i >= len(frames) for i in indices):
        raise ValueError("Selected frame out of range")
    expression = "+".join("eq(n,%d)" % i for i in indices)
    for index in indices:
        target = output / ("frame-%06d.png" % index)
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(video),
                "-vf",
                "select='eq(n,%d)'" % index,
                "-vsync",
                "0",
                "-frames:v",
                "1",
                "-y",
                str(target),
            ],
            check=True,
        )
    selected = [
        {
            "frame_index": i,
            "pts_seconds": frames[i]["best_effort_timestamp_time"],
            "path": str((output / ("frame-%06d.png" % i)).resolve()),
            "sha256": digest(output / ("frame-%06d.png" % i)),
        }
        for i in indices
    ]
    write(output / "selected-frame-evidence.json", selected)
    label_height = 36
    columns = min(3, len(indices))
    sheet = Image.new(
        "RGB", (columns * width, ((len(indices) + columns - 1) // columns) * (height + label_height)), "black"
    )
    draw = ImageDraw.Draw(sheet)
    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    font = ImageFont.truetype(str(font_path), 21) if font_path.exists() else ImageFont.load_default()
    for slot, item in enumerate(selected):
        x, y = (slot % columns) * width, (slot // columns) * (height + label_height)
        sheet.paste(Image.open(item["path"]).convert("RGB"), (x, y + label_height))
        draw.text(
            (x + 8, y + 5),
            "frame %d | PTS %ss" % (item["frame_index"], item["pts_seconds"]),
            fill="white",
            font=font,
        )
    sheet.save(output / "boundary-contact-sheet.png")
    report = {
        "schema_version": 1,
        "status": "decoded_frame_audit_visual_acceptance_not_asserted",
        "video": {"path": str(video.resolve()), "sha256": digest(video)},
        "decoded_frames": len(frames),
        "all_black_gray_frames": all_black,
        "first_nonblack_frame": first_nonblack,
        "first_nonblack_pts_seconds": None
        if first_nonblack is None
        else frames[first_nonblack]["best_effort_timestamp_time"],
        "all_frame_metadata": {"path": str(probe_path.resolve()), "sha256": digest(probe_path)},
        "all_frame_luma": {"path": str(stats_path.resolve()), "sha256": digest(stats_path)},
        "analysis_pixel_format": "ffmpeg gray 8-bit conversion; full decoded frames retained as selected PNG evidence",
        "frames_or_timestamps_dropped": False,
        "video_modified": False,
        "audit_source_sha256": digest(__file__),
    }
    write(output / "frame-audit.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--frames", type=int, nargs="+")
    args = parser.parse_args()
    audit(args.video, args.output, args.frames)
