#!/usr/bin/env python3
"""Build cache, pilot or production queue configs from verified native scenes."""

import argparse
import datetime
import json
from pathlib import Path
import admission as a


def build(cohort_path, plans_path, output, queue_id, mode, reviewer):
    cohort_path = cohort_path.resolve(strict=True)
    cohort = a.read(cohort_path)
    manifest = a.checked(cohort["actions_manifest"])
    actions = a.read(manifest)
    a.require(actions.get("status") == "actions_verified", "Actions have not passed extraction checks")
    count = actions.get("requested_count", len(actions["runs"]))
    a.require(1 <= count <= 250 and count == len(actions["runs"]), "Cohort must contain 1..250 runs")
    rows = {row["run"]["ordinal"]: row for row in cohort["runs"]}
    a.require(set(rows) == set(range(1, count + 1)), "Native scenes must cover the requested cohort")
    plan_doc = a.read(plans_path)
    plans = plan_doc.get("plans", plan_doc)
    a.require(
        set(plans)
        in (
            {"clear", "import", "export", "prepare", "render"},
            {"refresh", "clear", "import", "export", "prepare", "render"},
        ),
        "Incomplete UI plans",
    )
    paths = {k: str((plans_path.parent / Path(v)).resolve(strict=True)) for k, v in plans.items()}
    if mode == "cache":
        a.require(count >= 2, "A/B cache proof needs two distinct downloaded runs")
        pairs = [(1, "clean"), (2, "clean"), (1, "clean"), (1, "clock"), (1, "clean")]
    else:
        ordinals = range(1, min(5, count) + 1) if mode == "pilot" else range(6, count + 1)
        pairs = [(n, role) for n in ordinals for role in ("clean", "clock")]
    a.require(pairs, "No jobs selected; a cohort of at most five runs is covered by pilot mode")
    jobs = []
    source = {r["ordinal"]: r for r in actions["runs"]}
    for index, (ordinal, role) in enumerate(pairs):
        row = rows[ordinal]
        item = next(v for v in row["outputs"] if v["role"] == role)
        clip = a.checked(item["clip"])
        inspection = a.checked(item["inspection"])
        metadata = Path(source[ordinal]["actions"]["metadata_path"]).resolve(strict=True)
        a.require(
            a.pin(metadata)["sha256"] == source[ordinal]["actions"]["metadata_sha256"],
            "Original action metadata changed",
        )
        job = {
            "job_id": f"{mode}-{index + 1:04d}-rank{ordinal:03d}-{role}",
            "ordinal": ordinal,
            "role": role,
            "clip": str(clip),
            "inspection": str(inspection),
            "action_metadata": str(metadata),
            "provenance": str(clip.with_name(clip.name + ".provenance.json").resolve(strict=True)),
        }
        for key, field in [
            ("native_replay", "native_replay"),
            ("map_comparison", "replay_comparison"),
            ("replay_action_metadata", "native_action_metadata"),
        ]:
            job[key] = a.pin(a.checked(row[field]))
        jobs.append(job)
    a.require(reviewer.strip(), "Record the actual UI-plan reviewer")
    result = {
        "schema_version": 1,
        "queue_id": queue_id,
        "manifest": str(manifest),
        "source_native_cohort": a.pin(cohort_path),
        "jobs": jobs,
        "plans": paths,
        "plans_reviewed_by": reviewer,
        "plans_reviewed_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "mode": mode,
        "accepted": False,
        "aligned": False,
    }
    a.require(not output.exists(), "Configuration already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"config": str(output), "jobs": len(jobs), "mode": mode, "gui_inputs": 0}))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("cohort", "plans", "output"):
        p.add_argument("--" + key, type=Path, required=True)
    for key in ("queue-id", "reviewer"):
        p.add_argument("--" + key, required=True)
    p.add_argument("--mode", choices=("cache", "pilot", "production"), required=True)
    args = p.parse_args()
    build(args.cohort, args.plans, args.output, args.queue_id, args.mode, args.reviewer)
