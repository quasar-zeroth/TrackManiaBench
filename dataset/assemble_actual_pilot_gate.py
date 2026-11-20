#!/usr/bin/env python3
"""Assemble a gate from five real joined runs and an explicit reviewer attestation.

The program redecodes and audits the joins. It never manufactures visual,
repeat-render or resume evidence, nor marks an incomplete review as passed.
"""

import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import join_dataset_automatic_v2 as j


def review_checks(review, base, required):
    j.require(
        review.get("schema_version") == 1 and review.get("passed") is True,
        "A completed schema-1 pilot review is required",
    )
    j.require(
        review.get("verified_by") and review.get("verified_utc"),
        "Review must identify who verified it and when",
    )
    refs = [j.evidence(ref, base) for ref in review.get("evidence_files", [])]
    j.require(refs, "Review needs hash-pinned evidence files")
    checks = review.get("checks", {})
    for name in required:
        check = checks.get(name, {})
        j.require(
            check.get("passed") is True
            and isinstance(check.get("evidence"), str)
            and check["evidence"].strip(),
            "Incomplete review check: " + name,
        )
        local = [j.evidence(ref, base) for ref in check.get("evidence_files", [])]
        j.require(local, "Each review check needs its own hash-pinned evidence: " + name)
        refs.extend(local)
    return checks, refs


def assemble(args):
    joined_path = Path(args.dataset).resolve(strict=True)
    joined = j.read(joined_path)
    j.require(
        joined.get("status") == "dataset_verified"
        and joined.get("requested_count") == 5
        and len(joined["runs"]) == 5,
        "Exactly five verified pilot joins are required",
    )
    inputs = joined["inputs"]
    actions_path = Path(inputs["actions"]).resolve(strict=True)
    actions = j.read(actions_path)
    render_root = Path(inputs["render_root"]).resolve(strict=True)
    j.require(
        actions.get("requested_count") == 5 and len(actions["runs"]) == 5,
        "Pilot action manifest must contain exactly five runs",
    )
    review_path = Path(args.review).resolve(strict=True)
    review = j.read(review_path)
    backend = joined["renderer_backend"]
    checks, refs = review_checks(review, review_path.parent, j.renderer.required_gate_checks(backend))
    j.require(
        review.get("dataset_manifest") == j.pin(joined_path), "Review concerns a different joined dataset"
    )
    audit = io.StringIO()
    with redirect_stdout(audit):
        j.execute(
            SimpleNamespace(
                actions=str(actions_path),
                render_root=str(render_root),
                calibrations=inputs["calibrations"],
                output=str(joined_path.parent),
                count=5,
                gate=None,
                split_seed=joined["split_seed"],
                split_fractions=joined.get("split_fractions", "0.8,0.1,0.1"),
                audit=True,
            )
        )
    output = Path(args.output).resolve()
    audit_path = output.with_name(output.stem + ".join-audit.jsonl")
    j.checked_write(audit_path, audit.getvalue().encode())
    refs.extend(
        [j.pin(joined_path), j.pin(actions_path), j.pin(review_path), j.pin(audit_path), j.pin(__file__)]
    )
    source_runs = {r["ordinal"]: r for r in actions["runs"]}
    pilots = []
    for row in sorted(joined["runs"], key=lambda r: r["ordinal"]):
        run = source_runs[row["ordinal"]]
        j.require(run["run_id"] == row["run_id"], "Pilot action/join identity differs")
        summary = j.read(row["summary"]["path"])
        pilot = dict(
            ordinal=run["ordinal"],
            run_id=run["run_id"],
            ghost_sha256=run["ghost_sha256"],
            action_metadata=j.pin(run["actions"]["metadata_path"], run["actions"]["metadata_sha256"]),
            render_result=j.evidence(summary["render_result"], joined_path.parent),
            video=j.evidence(summary["video"], joined_path.parent),
            calibration=j.evidence(summary["calibration"], joined_path.parent),
            joined_summary=j.evidence(row["summary"], joined_path.parent),
            frame_actions=j.evidence(row["frame_actions"], joined_path.parent),
        )
        refs.extend(v for v in pilot.values() if isinstance(v, dict))
        pilots.append(pilot)
    j.require([p["ordinal"] for p in pilots] == [1, 2, 3, 4, 5], "Pilot leaderboard positions differ")
    refs = list({(r["path"], r["sha256"]): r for r in refs}.values())
    gate = dict(
        schema_version=1,
        passed=True,
        verified_by=review["verified_by"],
        verified_utc=review["verified_utc"],
        map_sha256=actions["map"]["sha256"],
        **j.renderer.backend_identity(backend),
        settings=joined["settings"],
        checks=checks,
        pilot_runs=pilots,
        evidence_files=refs,
        experimental_policy=review.get("experimental_policy", {}),
    )
    candidate = output.with_name(output.name + ".candidate")
    j.checked_write(candidate, j.encoded(gate))
    j.renderer.validate_gate(candidate, actions, render_root)
    j.checked_write(output, j.encoded(gate))
    return {"status": "actual_five_run_pilot_gate_passed", "gate": j.pin(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset")
    parser.add_argument("--review")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--review-template", action="store_true", help="Write an explicitly unpassed review template"
    )
    args = parser.parse_args()
    try:
        if args.review_template:
            template = dict(
                schema_version=1,
                passed=False,
                verified_by="",
                verified_utc="",
                dataset_manifest={"path": "", "sha256": "", "bytes": 0},
                evidence_files=[],
                checks={
                    name: dict(passed=False, evidence="", evidence_files=[])
                    for name in sorted(j.renderer.required_gate_checks("native_ui"))
                },
                experimental_policy={},
            )
            j.checked_write(Path(args.output), j.encoded(template))
            print(json.dumps({"status": "unpassed_review_template_written", "output": args.output}))
        else:
            j.require(args.dataset and args.review, "--dataset and --review are required")
            print(json.dumps(assemble(args)))
    except (ValueError, KeyError, OSError, TypeError) as error:
        print("Rejected: " + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
