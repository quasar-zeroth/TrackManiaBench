#!/usr/bin/env python3
"""Prepare completed immutable clean/clock pairs without approving any review.

A single local worker polls only completed candidate.json files. It never runs
GUI actions, downloads ghosts, changes native candidates, or accepts videos.
Preparation failure is retained; other pairs may proceed. Failed attempts need
an explicit new output/config, so partial evidence is never silently overwritten.
"""

import argparse
import datetime
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import time
import native_ui_queue_partial_v1 as ui
import production_video_audit_conditional_v2 as audit


def configure(a):
    ui.require(not a.output.exists(), "Audit queue config already exists")
    queue = ui.read(a.render_config)
    cohort = ui.read(a.native_cohort)
    base = a.render_config.resolve().parent
    manifest = Path(queue["manifest"])
    manifest = manifest if manifest.is_absolute() else base / manifest
    manifest = manifest.resolve(strict=True)
    sources = {r["run"]["ordinal"]: r for r in cohort["runs"]}
    jobs = {}
    for job in queue["jobs"]:
        ordinal = job["ordinal"]
        role = job["role"]
        ui.require(role in ("clean", "clock"), "Unknown role")
        ui.require(role not in jobs.setdefault(ordinal, {}), "Duplicate run/role")
        jobs[ordinal][role] = (a.candidates / job["job_id"] / "candidate.json").resolve()
    runs = []
    for n, pair in sorted(jobs.items()):
        ui.require(set(pair) == {"clean", "clock"}, "Audit requires a clean/clock pair for every run")
        source = sources[n]
        clock = ui.ref(source["source_clock"], a.native_cohort.resolve().parent)
        runs.append(
            {
                "ordinal": n,
                "run_id": source["run"]["run_id"],
                "clean_candidate": str(pair["clean"]),
                "clock_candidate": str(pair["clock"]),
                "source_clock_proof": ui.pin(clock),
                "output": str((a.audit_root / source["run"]["run_id"]).resolve()),
            }
        )
    doc = {
        "schema_version": 1,
        "manifest": ui.pin(manifest),
        "render_config": ui.pin(a.render_config),
        "native_cohort": ui.pin(a.native_cohort),
        "accepted_output": str(a.accepted.resolve()),
        "audit_pipeline": ui.pin(Path(audit.__file__)),
        "supervisor": ui.pin(Path(__file__)),
        "runs": runs,
        "status": "prepare_only_actual_review_and_acceptance_separate",
    }
    if a.gate:
        doc["pilot_gate"] = ui.pin(a.gate)
    ui.require(not any(r["ordinal"] > 5 for r in runs) or a.gate, "Full-cohort audit requires pilot gate")
    ui.common.write_json(a.output, doc)
    print(json.dumps({"configured_pairs": len(runs), "gui_inputs": 0, "accepted": 0}))


def watch(a):
    config = ui.read(a.config)
    base = a.config.resolve().parent
    for key in ("manifest", "render_config", "native_cohort", "audit_pipeline", "supervisor"):
        ui.ref(config[key], base)
    gate = ui.ref(config["pilot_gate"], base) if config.get("pilot_gate") else None
    manifest = ui.ref(config["manifest"], base)
    accepted = Path(config["accepted_output"])
    ui.require(
        config["audit_pipeline"]["sha256"] == ui.common.sha256(Path(audit.__file__)),
        "Configured audit code changed",
    )
    root = a.state.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        statepath = root / "progress.json"
        state = (
            ui.read(statepath)
            if statepath.exists()
            else {"schema_version": 1, "config": ui.pin(a.config), "runs": {}}
        )
        ui.require(state["config"] == ui.pin(a.config), "Resume config differs")
        while True:
            changed = False
            for item in config["runs"]:
                key = item["run_id"]
                previous = state["runs"].get(key, {})
                if previous.get("status") in ("prepared_review_required", "failed"):
                    continue
                clean, clock = Path(item["clean_candidate"]), Path(item["clock_candidate"])
                if not (clean.exists() and clock.exists()):
                    continue
                out = Path(item["output"])
                try:
                    ui.ref(config["audit_pipeline"], base)
                    ui.ref(item["source_clock_proof"], base)
                    ui.require(
                        not out.exists(), "Audit output already exists without a committed completion record"
                    )
                    sources = {"clean_candidate": ui.pin(clean), "clock_candidate": ui.pin(clock)}
                    cmd = [
                        sys.executable,
                        str(Path(audit.__file__)),
                        "prepare",
                        "--manifest",
                        str(manifest),
                        "--ordinal",
                        str(item["ordinal"]),
                        "--clean-candidate",
                        str(clean),
                        "--clock-candidate",
                        str(clock),
                        "--source-clock-proof",
                        str(ui.ref(item["source_clock_proof"], base)),
                        "--output",
                        str(out),
                        "--accepted",
                        str(accepted),
                    ]
                    if gate:
                        cmd += ["--gate", str(gate)]
                    started = time.monotonic()
                    with (root / (key + ".prepare.log")).open("x") as log:
                        subprocess.run(cmd, check=True, stdout=log, stderr=subprocess.STDOUT, timeout=900)
                    ui.require(
                        sources == {"clean_candidate": ui.pin(clean), "clock_candidate": ui.pin(clock)},
                        "Candidate changed during audit",
                    )
                    result = {
                        "status": "prepared_review_required",
                        "ordinal": item["ordinal"],
                        "review_draft": ui.pin(out / "review-draft.json"),
                        "elapsed_seconds": time.monotonic() - started,
                        **sources,
                    }
                except Exception as exc:
                    result = {
                        "status": "failed",
                        "ordinal": item["ordinal"],
                        "error": str(exc),
                        "output": str(out),
                    }
                result["updated_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                state["runs"][key] = result
                ui.common.write_json(statepath, state)
                changed = True
                print(json.dumps({"run_id": key, **result}), flush=True)
            complete = len(state["runs"]) == len(config["runs"])
            if complete or not a.watch:
                print(
                    json.dumps(
                        {
                            "status": "preparation_settled" if complete else "waiting_for_candidates",
                            "prepared": sum(
                                r["status"] == "prepared_review_required" for r in state["runs"].values()
                            ),
                            "failed": sum(r["status"] == "failed" for r in state["runs"].values()),
                            "pending": len(config["runs"]) - len(state["runs"]),
                            "accepted": 0,
                        }
                    ),
                    flush=True,
                )
                return
            time.sleep(a.poll_seconds)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("configure")
    for name in ("render-config", "native-cohort", "candidates", "audit-root", "accepted", "output"):
        q.add_argument("--" + name, type=Path, required=True)
    q.add_argument("--gate", type=Path)
    q = sub.add_parser("run")
    q.add_argument("config", type=Path)
    q.add_argument("--state", type=Path, required=True)
    q.add_argument("--watch", action="store_true")
    q.add_argument("--poll-seconds", type=int, choices=range(1, 31), default=10)
    a = p.parse_args()
    configure(a) if a.command == "configure" else watch(a)
