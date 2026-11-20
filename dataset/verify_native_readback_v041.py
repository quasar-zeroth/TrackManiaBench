#!/usr/bin/env python3
"""Compare a native editor re-export with a supported controlled Clip.Gbx.

Allows the editor to reorder tracks, but requires every exposed track/block
field, player entity selection and original sample value to remain identical.
Successful serialization checks do not assert video renderability or alignment.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import join_dataset as j


def normalized_tracks(doc):
    tracks = []
    for track in doc["tracks"]:
        t = {k: v for k, v in track.items() if k not in ("track_index", "blocks")}
        j.require(len(track["blocks"]) == 1, "Controlled scene requires exactly one block per track")
        t["blocks"] = [{k: v for k, v in track["blocks"][0].items() if k != "block_index"}]
        tracks.append(t)
    return sorted(tracks, key=lambda t: j.encoded(t))


def compare(intended, actual):
    j.require(intended["container_class"] == actual["container_class"], "Native container class changed")
    # Version0.4.1 preserves the original Clip inspection fields while explicitly
    # supporting hash-bound positive source starts and observed source ends.
    for doc in (intended, actual):
        j.require(
            doc["schema_version"] == 1
            and doc["gbx_net_version"] == "2.5.1"
            and doc["inspector_version"] in ("0.4.1",),
            "Unsupported inspector schema/version",
        )
    j.require(
        intended["local_player_clip_ent_index"] == actual["local_player_clip_ent_index"],
        "Native player entity selection changed",
    )
    a, b = normalized_tracks(intended), normalized_tracks(actual)
    classes = [t["blocks"][0]["class"] for t in a]
    j.require(
        classes.count("CGameCtnMediaBlockEntity") == 1 and classes.count("CGameCtnMediaBlockCameraGame") == 1,
        "Controlled scene requires exactly one entity and one game camera",
    )
    j.require(
        set(classes)
        <= set(("CGameCtnMediaBlockEntity", "CGameCtnMediaBlockCameraGame", "CGameCtnMediaBlockText")),
        "Unexpected controlled scene block class",
    )
    j.require(
        classes.count("CGameCtnMediaBlockText") in (0, 3),
        "Controlled scene must be clean or have exactly three clock pulses",
    )
    j.require(a == b, "Native readback track/block fields differ from intended scene")
    return len(a)


def verify(args):
    intended_clip = Path(args.intended_clip).resolve(strict=True)
    intended_inspection = Path(args.intended_inspection).resolve(strict=True)
    readback = Path(args.readback).resolve(strict=True)
    inspector = Path(args.inspector).resolve(strict=True)
    intended = j.read(intended_inspection)
    j.require(intended["source_sha256"] == j.sha(intended_clip), "Intended clip/inspection SHA-256 mismatch")
    prefix = Path(args.output_prefix).resolve()
    inspection = prefix.with_name(prefix.name + ".inspected.json")
    proof = prefix.with_name(prefix.name + "-comparison.json")
    identity = {
        k: j.pin(v)
        for k, v in dict(
            intended_clip=intended_clip,
            intended_inspection=intended_inspection,
            native_readback=readback,
            inspector=inspector,
            validator=Path(__file__).resolve(),
        ).items()
    }
    if inspection.exists():
        actual = j.read(inspection)
        j.require(actual["source_sha256"] == j.sha(readback), "Existing readback inspection SHA-256 differs")
    else:
        inspection.parent.mkdir(parents=True, exist_ok=True)
        command = ([str(Path(args.dotnet).resolve(strict=True))] if args.dotnet else []) + [
            str(inspector),
            str(readback),
            str(inspection),
        ]
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        actual = j.read(inspection)
    j.require(actual["source_sha256"] == j.sha(readback), "Inspector readback SHA-256 differs")
    count = compare(intended, actual)
    result = dict(
        schema_version=1,
        status="native_controlled_scene_readback_verified",
        verified_utc=datetime.now(timezone.utc).isoformat(),
        inputs=identity,
        all_exposed_block_fields_equal=True,
        all_exposed_track_fields_equal=True,
        native_track_reordering_allowed=True,
        controlled_track_count=count,
        imported_inspection=j.pin(intended_inspection),
        native_readback_inspection=j.pin(inspection),
        source_sha256=j.sha(intended_clip),
        native_readback_sha256=j.sha(readback),
        video_renderability_verified=False,
        video_alignment_verified=False,
    )
    if proof.exists():
        old = j.read(proof)
        j.require(
            old.get("inputs") == identity and old.get("native_readback_inspection") == j.pin(inspection),
            "Completed readback proof source/tooling/output hashes changed",
        )
        j.require(
            old["all_exposed_track_fields_equal"] and old["all_exposed_block_fields_equal"],
            "Existing proof was not successful",
        )
    else:
        j.checked_write(proof, j.encoded(result))
    return dict(
        status=result["status"], proof=str(proof), inspection=str(inspection), controlled_track_count=count
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--readback", required=True)
    for name in ("intended-clip", "intended-inspection", "inspector", "output-prefix"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--dotnet")
    args = p.parse_args()
    try:
        print(json.dumps(verify(args)))
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print("Rejected: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
