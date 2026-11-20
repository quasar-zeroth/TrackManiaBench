#!/usr/bin/env python3
"""Attach verified source-clock proofs without changing prepared scene artifacts.

The output is a new projection manifest. Native import, actual video review and
per-run measured video calibration remain required and are never inferred here.
"""

import argparse
import copy
from collections import Counter
import json
from pathlib import Path
import sys
import join_dataset as j
import verify_source_clocks_v2 as clocks


def finalize(args):
    source = Path(args.scenes).resolve(strict=True)
    scenes = j.read(source)
    count = scenes.get("requested_count")
    j.require(type(count) is int and count > 0, "Positive scene cohort count required")
    j.require(
        scenes.get("status") == "native_sources_and_scenes_verified_clock_pending"
        and scenes.get("requested_count") == scenes.get("prepared_count") == count
        and scenes.get("selected_ordinals") == list(range(1, count + 1)),
        "Complete prepared scene manifest required",
    )
    actions_ref = j.evidence(scenes["actions_manifest"], source.parent)
    actions = j.read(actions_ref["path"])
    j.require(
        actions.get("status") == "actions_verified" and len(actions.get("runs", [])) == count,
        "Original complete action cohort required",
    )
    by_id = {r["run_id"]: r for r in actions["runs"]}
    rows = []
    methods = Counter()
    classes = Counter()
    for row in scenes["runs"]:
        run = row["run"]
        rid = run["run_id"]
        j.require(
            run == by_id[rid]
            and row["status"] == "native_sources_and_scenes_verified_clock_pending"
            and row["source_clock_verified"] is False,
            "Prepared run source/clock status differs",
        )
        for ref in row["files"]:
            j.evidence(ref, source.parent)
        folder = Path(row["outputs"][0]["clip"]["path"]).parent
        prepared_ref = j.pin(folder / "prepared.json")
        j.require(
            j.read(prepared_ref["path"]) == row,
            "Prepared manifest row differs from immutable source preparation",
        )
        proof_path = Path(args.clocks).resolve(strict=True) / (rid + ".json")
        proof_ref = j.pin(proof_path)
        proof = j.read(proof_path)
        verified = clocks.validate_proof(proof)
        j.require(
            j.evidence(proof["source_notices"], proof_path.parent)
            == j.evidence(row["source_notices"], source.parent),
            "Clock raw notices differ from prepared source",
        )
        j.require(
            j.evidence(proof["action_metadata"], proof_path.parent)
            == j.evidence(row["source_identity"]["original_source"]["metadata"], source.parent),
            "Clock action metadata differs from original source",
        )
        result = copy.deepcopy(row)
        result.update(
            status="native_replay_sources_and_controlled_scenes_verified_import_pending",
            original_scene_preparation=prepared_ref,
            source_clock=proof_ref,
            source_clock_evidence_verified=True,
            source_clock_verified=verified["evidence_class"] == "direct_checkpoint_notices",
            source_clock_evidence_class=verified["evidence_class"],
            source_clock_method=verified["method"],
        )
        rows.append(result)
        methods[verified["method"]] += 1
        classes[verified["evidence_class"]] += 1
    j.require(
        [r["run"]["ordinal"] for r in rows] == list(range(1, count + 1)),
        "Prepared cohort missing/duplicates/order differs",
    )
    result = copy.deepcopy(scenes)
    result.update(
        status="native_cohort_prepared_import_pending",
        source_clock_evidence_verified=True,
        source_clock_verified=classes["conditional_control_reflection"] == 0,
        runs=rows,
        scene_preparation_manifest=j.pin(source),
        source_clock_verifier=j.pin(clocks.__file__),
        finalizer=j.pin(__file__),
        source_clock_methods=dict(methods),
        source_clock_evidence_classes=dict(classes),
        native_import_verified=False,
        video_alignment_verified=False,
    )
    output = Path(args.output).resolve()
    j.require(output != source, "Preserve original scene manifest")
    j.checked_write(output, j.encoded(result))
    return dict(status=result["status"], runs=count, manifest=str(output), source_clock_methods=dict(methods))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("scenes", "clocks", "output"):
        p.add_argument("--" + key, required=True)
    args = p.parse_args()
    try:
        print(json.dumps(finalize(args)))
    except (OSError, ValueError, KeyError, TypeError) as e:
        print("Rejected: " + str(e), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
