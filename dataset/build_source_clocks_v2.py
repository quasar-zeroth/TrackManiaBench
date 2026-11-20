#!/usr/bin/env python3
"""Create resumable source-clock evidence without changing prepared scenes.

Direct checkpoint and conditional control evidence remain distinct. This never
approves videos, calibrations, or training rows.
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import control_clock_corroboration_v1 as controls
import join_dataset as j
import verify_source_clocks_v2 as clocks


def prepare(row, output, packages):
    run = row["run"]
    rid = run["run_id"]
    j.require(
        row["status"] == "native_sources_and_scenes_verified_clock_pending"
        and row["source_clock_verified"] is False,
        "Unexpected scene preparation status",
    )
    folder = Path(row["outputs"][0]["clip"]["path"]).parent
    j.require(
        j.read(folder / "prepared.json") == row, "Prepared scene row differs from preserved run manifest"
    )
    for ref in row["files"]:
        j.evidence(ref, folder)
    raw_ref = j.evidence(row["source_notices"], folder)
    meta_ref = j.evidence(row["source_identity"]["original_source"]["metadata"], folder)
    j.require(
        meta_ref["sha256"] == run["actions"]["metadata_sha256"],
        "Source metadata differs from frozen action cohort",
    )
    raw = j.read(raw_ref["path"])
    meta = j.read(meta_ref["path"])
    try:
        result = clocks.verify(raw, meta)
    except ValueError:
        j.require(rid in packages, "No reviewed conditional evidence for this source")
        result = controls.verify(raw, meta, packages[rid])
    result.update(
        schema_version=2,
        source_notices=raw_ref,
        action_metadata=meta_ref,
        verifier_sha256=j.sha(clocks.__file__),
    )
    clocks.validate_proof(result)
    destination = output / (rid + ".json")
    resumed = destination.exists()
    j.checked_write(destination, j.encoded(result))
    return dict(
        ordinal=run["ordinal"], run_id=rid, evidence_class=result["evidence_class"], proof=j.pin(destination)
    ), resumed


def build(args):
    source = Path(args.scenes).resolve(strict=True)
    scenes = j.read(source)
    count = scenes.get("requested_count")
    j.require(type(count) is int and count > 0, "Positive scene cohort count required")
    j.require(
        scenes.get("status") == "native_sources_and_scenes_verified_clock_pending"
        and scenes.get("requested_count") == count,
        "Expected preserved full-cohort scene preparation",
    )
    actions_ref = j.evidence(scenes["actions_manifest"], source.parent)
    actions = j.read(actions_ref["path"])
    j.require(
        actions.get("status") == "actions_verified" and len(actions.get("runs", [])) == count,
        "Complete source action manifest required",
    )
    by_id = {r["run_id"]: r for r in actions["runs"]}
    ordinals = [r["run"]["ordinal"] for r in scenes["runs"]]
    j.require(
        ordinals == scenes["selected_ordinals"]
        and len(set(ordinals)) == len(ordinals) == scenes["prepared_count"],
        "Missing, duplicate, or reordered prepared sources",
    )
    index_ref = controls.evidence_index_reference() if controls.EVIDENCE_INDEX else None
    index = j.read(index_ref["path"]) if index_ref else {"runs": []}
    packages = {r["run_id"]: r["evidence"] for r in index["runs"]}
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    counts = Counter()
    resumed = 0
    for row in scenes["runs"]:
        j.require(row["run"] == by_id[row["run"]["run_id"]], "Prepared source differs from frozen cohort")
        entry, old = prepare(row, output, packages)
        rows.append(entry)
        counts[entry["evidence_class"]] += 1
        resumed += old
        print(
            json.dumps(
                dict(
                    ordinal=entry["ordinal"],
                    run_id=entry["run_id"],
                    evidence_class=entry["evidence_class"],
                    resumed=old,
                )
            ),
            flush=True,
        )
    report = dict(
        schema_version=1,
        status="source_clock_evidence_verified",
        requested_count=count,
        verified_count=len(rows),
        selected_ordinals=ordinals,
        scenes=j.pin(source),
        actions_manifest=actions_ref,
        control_evidence_index=index_ref,
        verifier=j.pin(clocks.__file__),
        conditional_validator=j.pin(controls.__file__),
        builder=j.pin(__file__),
        evidence_classes=dict(counts),
        runs=rows,
        video_alignment_verified=False,
        interpretation="Direct checkpoint evidence and conditional reflected-control evidence retain separate precision and training eligibility. No video has been approved by this source-only step.",
    )
    report_path = Path(args.report).resolve()
    j.checked_write(report_path, j.encoded(report))
    return dict(
        status=report["status"],
        verified_count=len(rows),
        resumed=resumed,
        evidence_classes=dict(counts),
        report=str(report_path),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("scenes", "output", "report"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args)))
    except (OSError, ValueError, KeyError, TypeError) as e:
        print("Rejected: " + str(e), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
