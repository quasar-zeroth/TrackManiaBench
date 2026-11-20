#!/usr/bin/env python3
"""Pack all required native audit samples into60-tile cross-run review pages.

Contact sheets are previews only; originals remain hash-bound and available.
This never records a review, clears an anomaly, or changes acceptance flags.
"""

import argparse
import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import native_ui_queue as ui


def build(reviews, output):
    ui.require(not output.exists(), "Review book output must be new")
    output.mkdir(parents=True)
    cards = []
    settings = []
    markers = []
    seen = set()
    for path in reviews:
        path = path.resolve(strict=True)
        r = ui.read(path)
        ui.require(r["run_id"] not in seen, "Duplicate run in review book")
        seen.add(r["run_id"])
        scanpath = ui.ref(r["scan"], path.parent)
        s = ui.read(scanpath)
        ui.require(
            r["required_manual_frame_indices"] == s["required_manual_frame_indices"],
            "Review/scan selections differ",
        )
        refs = {f["frame_index"]: f for f in s["selected_frame_evidence"]}
        for i in r["required_manual_frame_indices"]:
            frame = refs[i]
            source = ui.ref(frame, scanpath.parent)
            cards.append(
                {
                    "ordinal": r["ordinal"],
                    "run_id": r["run_id"],
                    "frame_index": i,
                    "pts_ms": frame["pts_ms"],
                    "flagged": i in s["flagged_frames_require_review"],
                    "source": ui.pin(source),
                    "review": ui.pin(path),
                }
            )
        pulsepath = ui.ref(r["pulses"], path.parent)
        pulse = ui.read(pulsepath)
        markers.append(
            {
                "ordinal": r["ordinal"],
                "run_id": r["run_id"],
                "source": ui.pin(ui.ref(pulse["detection"]["review_sheet"], pulsepath.parent)),
                "required_texts": [p["native_text"] for p in pulse["pulses"]],
                "review": ui.pin(path),
            }
        )
        for role in ("clean", "clock"):
            cp = ui.ref(r[role + "_candidate"], path.parent)
            c = ui.read(cp)
            for phase in ("settings", "completion"):
                settings.append(
                    {
                        "ordinal": r["ordinal"],
                        "run_id": r["run_id"],
                        "role": role,
                        "phase": phase,
                        "source": ui.pin(ui.ref(c[phase + "_observation"], cp.parent)),
                        "review": ui.pin(path),
                    }
                )
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
    pages = []
    for start in range(0, len(cards), 60):
        batch = cards[start : start + 60]
        rows = math.ceil(len(batch) / 6)
        image = Image.new("RGB", (1536, rows * 172), "black")
        draw = ImageDraw.Draw(image)
        for slot, card in enumerate(batch):
            x = (slot % 6) * 256
            y = (slot // 6) * 172
            source = Image.open(card["source"]["path"])
            ui.require(source.size == (640, 360), "Unexpected source image size")
            image.paste(source.resize((256, 144), Image.BILINEAR), (x, y + 28))
            label = f"#{card['ordinal']:03d} f{card['frame_index']} {card['pts_ms']:.0f}ms" + (
                " FLAG" if card["flagged"] else ""
            )
            draw.text((x + 2, y + 3), label, font=font, fill="#ffaaaa" if card["flagged"] else "white")
        target = output / f"samples-{len(pages) + 1:04d}.png"
        image.save(target)
        pages.append({"page": len(pages) + 1, "image": ui.pin(target), "cards": batch})
    ui.common.write_json(
        output / "review-book.json",
        {
            "schema_version": 1,
            "status": "actual_visual_review_required",
            "review_mode": "automated_scan_plus_sampled_visual_review",
            "runs": len(reviews),
            "samples": len(cards),
            "tiles_per_full_page": 60,
            "sample_pages": pages,
            "marker_sheets": markers,
            "settings_completion_originals": settings,
            "all_original_pngs_preserved": True,
            "sample_thumbnail_size": [256, 144],
            "labels_outside_source_pixels": True,
            "review_flags_changed": False,
            "accepted": False,
            "generator": ui.pin(Path(__file__)),
            "instruction": "Inspect every sample tile, each marker sheet and settings/completion originals. Enlarge any unclear or flagged source PNG. Record the exact reviewed indices and individual anomaly resolutions in each immutable review copy; this book never marks them reviewed.",
        },
    )
    print({"runs": len(reviews), "samples": len(cards), "sample_pages": len(pages), "output": str(output)})


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("reviews", type=Path, nargs="+")
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    build(a.reviews, a.output)
