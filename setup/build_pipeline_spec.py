#!/usr/bin/env python3
"""Generate an editable local-only durable pipeline proposal from real paths.

This performs no rendering, QA, acceptance or training. The supervisor's
configure command subsequently validates inputs and freezes the proposal.
"""

import argparse
import json
from pathlib import Path
import sys
import admission as c

ROOT = Path(__file__).resolve().parents[1]


def build(a):
    actions = c.read(a.actions)
    count = actions["requested_count"]
    c.require(
        actions.get("status") == "actions_verified" and len(actions["runs"]) == count,
        "Verified source actions required",
    )
    c.require(a.batch_size > 0, "Calibration batch size must be positive")
    c.require(5 < count <= 250, "Production supervisor requires a completed five-run pilot and larger cohort")

    def command(path, *args):
        return [sys.executable, str(ROOT / path), *map(str, args)]

    calibrate = command(
        "dataset/calibrate_production_automatic_v2.py",
        "--actions",
        a.actions,
        "--prepared",
        a.cohort,
        "--gate",
        a.gate,
        "--render-root",
        a.accepted,
        "--audit-root",
        a.audit_root,
        "--calibrations",
        a.calibrations,
        "--state",
        a.calibration_state,
        "--clock-parameters-from-source-proofs",
    )
    stages = [
        {
            "name": "calibration_final",
            "argv": calibrate + ["--require-complete"],
            "guard": False,
            "retry": "never",
            "checks": [
                {
                    "path": str(a.calibration_state),
                    "json": {"status": "all_calibrations_verified", "total_verified_calibrations": count},
                    "snapshot": True,
                }
            ],
        },
        {
            "name": "join",
            "argv": command(
                "dataset/join_dataset_automatic_v2.py",
                "--actions",
                a.actions,
                "--render-root",
                a.accepted,
                "--calibrations",
                a.calibrations,
                "--output",
                a.joined,
                "--count",
                count,
                "--gate",
                a.gate,
                "--split-seed",
                a.split_seed,
                "--split-fractions",
                a.split_fractions,
            ),
            "guard": True,
            "retry": "never",
            "checks": [
                {
                    "path": str(a.joined / "dataset_manifest.json"),
                    "json": {"status": "dataset_verified", "requested_count": count},
                }
            ],
        },
        {
            "name": "export",
            "argv": command(
                "dataset/export_portable_automatic_v2.py",
                "--dataset",
                a.joined / "dataset_manifest.json",
                "--gate",
                a.gate,
                "--output",
                a.release,
                "--archive-reference",
                "{completed_set}",
            ),
            "guard": True,
            "retry": "never",
            "checks": [{"path": str(a.release / "release_manifest.json"), "json": {}}],
        },
        {
            "name": "local_verify",
            "argv": command("dataset/export_portable_automatic_v2.py", "--verify", a.release),
            "guard": False,
            "retry": "safe_resume",
            "checks": [
                {
                    "stdout_json": {
                        "status": "portable_release_hashes_and_audit_closure_verified",
                        "runs": count,
                    }
                }
            ],
        },
    ]
    source_tools = [
        str(p)
        for directory in ("renderer", "dataset", "setup")
        for p in (ROOT / directory).rglob("*.py")
        if "__pycache__" not in p.parts
    ]
    proposal = {
        "root": str(ROOT),
        "actions": str(a.actions),
        "native_cohort": str(a.cohort),
        "audit_queue": str(a.audit_queue),
        "accepted_root": str(a.accepted),
        "watcher_state": str(a.watcher_state),
        "state_root": str(a.state),
        "automatic_reference": str(a.reference),
        "minimum_preserved": 5,
        "tool_paths": source_tools,
        "input_paths": [str(a.gate), str(a.reference)],
        "calibration_batch_size": a.batch_size,
        "env": {},
        "qa": {
            "argv": command(
                "renderer/production_video_audit_automatic_v2.py",
                "run",
                "{audit_dir}/review-draft.json",
                "--reference",
                "{reference}",
                "--accepted",
                "{accepted_dir}",
                "--output",
                "{audit_dir}/automatic-policy-v1",
            ),
            "guard": True,
            "retry": "never",
            "checks": [
                {
                    "path": "{audit_dir}/automatic-policy-v1/acceptance-receipt.json",
                    "json": {"status": "accepted_calibration_join_still_required", "run_id": "{run_id}"},
                }
            ],
        },
        "calibration_batch": {
            "name": "calibration_batch",
            "argv": calibrate,
            "guard": False,
            "retry": "never",
            "checks": [{"path": str(a.calibration_state), "json": {}, "snapshot": True}],
        },
        "final_stages": stages,
    }
    c.require(not a.output.exists(), "Proposal already exists")
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open("x") as f:
        json.dump(proposal, f, indent=2)
        f.write("\n")
    print(
        json.dumps(
            {
                "proposal": str(a.output),
                "requested_count": count,
                "final_stages": [s["name"] for s in stages],
                "launched_workers": 0,
            }
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for key in (
        "actions",
        "cohort",
        "gate",
        "accepted",
        "audit-root",
        "audit-queue",
        "watcher-state",
        "reference",
        "calibrations",
        "calibration-state",
        "joined",
        "release",
        "state",
        "output",
    ):
        p.add_argument("--" + key, type=lambda x: Path(x).expanduser().resolve(), required=True)
    p.add_argument("--batch-size", type=int, default=10)
    p.add_argument("--split-seed", default="trackmaniabench-v1")
    p.add_argument("--split-fractions", default="0.8,0.1,0.1")
    build(p.parse_args())
