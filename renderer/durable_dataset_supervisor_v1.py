#!/usr/bin/env python3
"""Durable automatic QA -> calibration -> release pipeline, without LLM calls.

No GUI process is created or retried. Existing native render/prepare workers are
consumed. Stage workers have independent sessions, immutable command receipts,
and a shared decode gate. An uncertain acceptance is quarantined, never retried.
"""

import argparse
from collections import Counter
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pipeline_common_v1 as c
import dataset_pipeline_worker_v1 as worker
import serial_accept_review_v3 as scheduler

HERE = Path(__file__).resolve().parent
FINAL_ORDER = ["calibration_final", "join", "export", "local_verify", "transfer", "remote_verify"]
AUTO_MODE = "deterministic_decoded_image_policy_v1"
OWNED_PROCESSES = {}


def run_identity(run):
    return {k: run[k] for k in ("run_id", "ordinal", "account_id", "ghost_sha256", "record_time_ms")}


def accepted_pin(root, run):
    result = Path(root) / run["run_id"] / "result.json"
    data = c.read(result)
    c.require(data.get("status") == "native_video_verified", "Accepted result is incomplete")
    c.matching(data["run"], run_identity(run))
    video = data["video"]
    # Frozen native_ui_queue writes this exact basename and hashes the accepted
    # remux separately from native.webm. Never substitute the raw native video.
    path = result.parent / "video.webm"
    c.require(c.sha(path) == video["sha256"], "Accepted video bytes changed")
    return {"result": c.pin(result), "video": c.pin(path)}


def compile_config(spec_path, output):
    """Freeze the reviewed proposal only when every implementation/input exists."""
    spec = c.read(spec_path)
    c.require(not output.exists(), "Refuse overwrite of pipeline config")
    root = Path(spec["root"]).resolve(strict=True)
    actions_path = (root / spec["actions"]).resolve(strict=True)
    actions = c.read(actions_path)
    count = actions.get("requested_count", 0)
    c.require(
        actions.get("status") == "actions_verified" and 1 <= count <= 250,
        "Verified actions for the requested cohort required",
    )
    runs = sorted(actions["runs"], key=lambda r: r["ordinal"])
    c.require(
        len(runs) == count
        and [r["ordinal"] for r in runs] == list(range(1, count + 1))
        and len({r["run_id"] for r in runs}) == count,
        "Source cohort identity/cardinality differs",
    )
    audit = c.pin(root / spec["audit_queue"])
    audit_doc = c.read_verified(audit)
    c.require(audit_doc["manifest"] == c.pin(actions_path), "Watcher action cohort differs")
    native = c.read(root / spec["native_cohort"])
    classes = {r["run"]["run_id"]: r["source_clock_evidence_class"] for r in native["runs"]}
    c.require(len(classes) == count, "Full native source projection required")
    c.require(
        set(classes.values()) <= {"direct_checkpoint_notices", "conditional_control_reflection"},
        "Unknown timing evidence class",
    )
    preserved = []
    audit_items = {x["run_id"]: x for x in audit_doc["runs"]}
    for run in runs:
        if (root / spec["accepted_root"] / run["run_id"] / "result.json").exists():
            entry = dict(run=run_identity(run), **accepted_pin(root / spec["accepted_root"], run))
            item = audit_items.get(run["run_id"])
            if item and (Path(item["output"]) / "automatic-policy-v1" / "acceptance-receipt.json").exists():
                automatic = automatic_receipt(
                    dict(item, source=run), {"accepted_root": str(root / spec["accepted_root"])}
                )
                receipt = c.read_verified(automatic["receipt"])
                entry["automatic_evidence"] = {
                    "receipt": automatic["receipt"],
                    "review": receipt["automatic_review"],
                    "files": receipt["evidence_files"],
                }
            preserved.append(entry)
    c.require(len(preserved) >= spec["minimum_preserved"], "Previously accepted outputs are missing")
    expected_preserved = spec.get("expected_preserved_ordinals")
    if expected_preserved is not None:
        c.require(
            sorted(p["run"]["ordinal"] for p in preserved) == expected_preserved,
            "Preserved accepted identity set changed before config freeze",
        )
    source = {r["run_id"]: r for r in runs}
    items = []
    for item in audit_doc["runs"]:
        c.require(
            item["run_id"] in source and item["ordinal"] == source[item["run_id"]]["ordinal"],
            "Watcher changed frozen run identity",
        )
        items.append(
            dict(item, source=run_identity(source[item["run_id"]]), timing_class=classes[item["run_id"]])
        )
    c.require(
        len(items) <= count and len({r["run_id"] for r in items}) == len(items),
        "Watcher pairs must be a unique subset of the cohort",
    )
    tools = [
        Path(__file__),
        HERE / "pipeline_common_v1.py",
        HERE / "dataset_pipeline_worker_v1.py",
        HERE / "serial_accept_review_v3.py",
    ] + [root / p for p in spec["tool_paths"]]
    inputs = [c.pin(actions_path), audit, c.pin(root / spec["native_cohort"])]
    inputs += [c.pin(root / p) for p in spec["input_paths"]]
    config = dict(spec)
    config.update(
        schema_version=1,
        status="frozen_automatic_dataset_pipeline",
        root=str(root),
        actions=c.pin(actions_path),
        audit_queue=audit,
        automatic_reference=c.pin(root / spec["automatic_reference"]),
        tool_pins=list({str(p.resolve()): c.pin(p) for p in tools}.values()),
        input_pins=list({p["path"]: p for p in inputs}.values()),
        preserved=preserved,
        runs=[dict(run_identity(r), timing_class=classes[r["run_id"]]) for r in runs],
        pending_pairs=items,
        created_utc=c.now(),
        proposal=c.pin(spec_path),
    )
    for key in ("accepted_root", "watcher_state", "state_root", "automatic_reference"):
        if key != "automatic_reference":
            config[key] = str((root / spec[key]).resolve())
    c.require(
        [s["name"] for s in spec["final_stages"]] in (FINAL_ORDER, FINAL_ORDER[:4]),
        "Final stage order differs",
    )
    c.require(
        all(s.get("guard") for s in spec["final_stages"] if s["name"] in ("join", "export")),
        "Join/export must use the shared decode gate",
    )
    c.require(spec["qa"].get("guard") is True, "Automatic QA must use the shared decode gate")
    c.require(spec["qa"].get("retry") == "never", "Uncertain automatic acceptance cannot retry")
    for stage in spec["final_stages"]:
        if stage.get("retry") == "safe_resume":
            c.require(
                stage["name"] in ("transfer", "local_verify", "remote_verify"),
                "Only verification and nondestructive transfer may retry automatically",
            )
        c.require(stage.get("checks"), "Every final stage requires positive output checks")
        if stage["name"] == "transfer":
            c.require(
                Path(stage["argv"][0]).name == "rsync"
                and "--checksum" in stage["argv"]
                and "--partial" in stage["argv"]
                and not any(x.startswith("--delete") for x in stage["argv"]),
                "Transfer must be checksum/resumable rsync without deletion",
            )
    c.write_new(output, config)
    print(
        json.dumps(
            {
                "status": config["status"],
                "config": c.pin(output),
                "preserved": len(preserved),
                "target_runs": len(runs),
                "launched_workers": 0,
            }
        )
    )


def automatic_receipt(item, config):
    path = Path(item["output"]) / "automatic-policy-v1" / "acceptance-receipt.json"
    data = c.read(path)
    c.matching(
        data,
        {
            "schema_version": 2,
            "review_mode": AUTO_MODE,
            "status": "accepted_calibration_join_still_required",
            "run_id": item["run_id"],
            "ordinal": item["ordinal"],
        },
    )
    review = c.read_verified(data["automatic_review"])
    c.require(review.get("run_id") == item["run_id"], "Automatic review belongs to another run")
    c.require(data.get("evidence_files"), "Automatic acceptance lacks pinned evidence")
    for ref in data["evidence_files"]:
        c.verified(ref)
    result = Path(config["accepted_root"]) / item["run_id"] / "result.json"
    c.require(c.verified(data["accepted_result"]) == result, "Automatic accepted result path differs")
    accepted = accepted_pin(config["accepted_root"], item["source"])
    return {"receipt": c.pin(path), **accepted}


def admit_worker(active, root):
    folder = Path(active["attempt"])
    path = folder / "worker.json"
    if not path.exists():
        return False
    registered = c.read(path)
    identity = registered["identity"]
    c.require(registered["spec"] == active["spec"], "Registered worker spec differs")
    c.require(scheduler.identity(identity["pid"]) == identity, "Registered worker identity differs")
    expected = [
        sys.executable,
        "-B",
        str(HERE / "dataset_pipeline_worker_v1.py"),
        str(folder / "spec.json"),
        "--attempt",
        str(folder),
    ]
    c.require(
        identity["argv"] == expected and Path(identity["cwd"]) == Path(root),
        "Registered worker command/directory differs",
    )
    proof = {"schema_version": 1, "spec": active["spec"], "worker_identity": identity}
    target = folder / "launch-admitted.json"
    if target.exists():
        c.require(c.read(target) == proof, "Committed launch admission differs")
    else:
        c.write_new(target, proof)
    return True


class Supervisor:
    def __init__(self, config_path, config, state_root):
        self.config_path = Path(config_path).resolve(strict=True)
        self.config = config
        self.root = Path(config["root"])
        self.state_root = Path(state_root)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_root / "state.json"
        self.state = (
            c.read(self.state_path)
            if self.state_path.exists()
            else {
                "schema_version": 1,
                "config": c.pin(self.config_path),
                "runs": {},
                "stages": {},
                "calibration_batches": [],
                "active": None,
                "status": "waiting_for_native_candidates",
            }
        )
        c.require(self.state["config"] == c.pin(self.config_path), "Resume configuration differs")
        self.source = {r["run_id"]: r for r in config["runs"]}

    def save(self):
        self.state["updated_utc"] = c.now()
        c.atomic(self.state_path, self.state)

    def verify_inputs(self):
        for ref in self.config["tool_pins"] + self.config["input_pins"]:
            c.verified(ref)
        c.verified(self.config["automatic_reference"])

    def preserve(self):
        for entry in self.config["preserved"]:
            for key in ("result", "video"):
                c.verified(entry[key])
            rid = entry["run"]["run_id"]
            existing = self.state["runs"].get(rid)
            row = dict(
                status="preserved_accepted", source=entry["run"], result=entry["result"], video=entry["video"]
            )
            if entry.get("automatic_evidence"):
                evidence = entry["automatic_evidence"]
                for ref in [evidence["receipt"], evidence["review"]] + evidence["files"]:
                    c.verified(ref)
                row["automatic_evidence"] = evidence
            c.require(existing is None or existing == row, "Preserved accepted checkpoint changed")
            self.state["runs"][rid] = row

    def variables(self, item=None):
        v = {
            "root": str(self.root),
            "accepted_dir": self.config["accepted_root"],
            "reference": self.config["automatic_reference"]["path"],
            "completed_set": str(self.state_root / "completed-accepted-set.json"),
        }
        if item:
            v.update(
                run_id=item["run_id"],
                ordinal=str(item["ordinal"]),
                audit_dir=item["output"],
                candidate_clean=item["clean_candidate"],
                candidate_clock=item["clock_candidate"],
            )
        return v

    def launch(self, key, template, item=None, number=1):
        c.require(self.state["active"] is None, "Another pipeline stage is active")
        self.verify_inputs()
        folder = self.state_root / "attempts" / key / ("attempt-%04d" % number)
        folder.mkdir(parents=True, exist_ok=False)
        spec = c.expand(template, self.variables(item))
        inputs = list(self.config["tool_pins"]) + list(self.config["input_pins"])
        inputs += [self.config["automatic_reference"]]
        if item:
            inputs.extend(
                [
                    c.pin(Path(item["output"]) / "review-draft.json"),
                    c.pin(item["clean_candidate"]),
                    c.pin(item["clock_candidate"]),
                ]
            )
        if (self.state_root / "completed-accepted-set.json").exists():
            inputs.append(c.pin(self.state_root / "completed-accepted-set.json"))
        spec.update(inputs=inputs, cwd=str(self.root), env=self.config.get("env", {}))
        if spec.get("guard"):
            spec["guard"] = {"config": self.config["audit_queue"], "state": self.config["watcher_state"]}
        else:
            spec["guard"] = None
        c.write_new(folder / "spec.json", spec)
        active = {
            "key": key,
            "attempt": str(folder),
            "number": number,
            "spec": c.pin(folder / "spec.json"),
            "kind": "qa" if item else template["name"],
            "run_id": item["run_id"] if item else None,
            "created_utc": c.now(),
        }
        self.state["active"] = active
        self.save()  # Before Popen: a crash in the launch gap is uncertain, not rerunnable.
        with (folder / "worker.log").open("xb") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(HERE / "dataset_pipeline_worker_v1.py"),
                    str(folder / "spec.json"),
                    "--attempt",
                    str(folder),
                ],
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        active["launch_pid"] = process.pid
        active["launch_identity"] = scheduler.identity(process.pid)
        OWNED_PROCESSES[process.pid] = process
        self.save()
        deadline = time.monotonic() + 5
        while (
            not (folder / "worker.json").exists() and process.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        if (folder / "worker.json").exists() and process.poll() is None:
            admit_worker(active, self.root)

    def finish_active(self):
        for pid, child in list(OWNED_PROCESSES.items()):
            if child.poll() is not None:
                del OWNED_PROCESSES[pid]
        active = self.state["active"]
        if active is None:
            return
        folder = Path(active["attempt"])
        receipt = folder / "receipt.json"
        wi = folder / "worker.json"
        identity = c.read(wi)["identity"] if wi.exists() else active.get("launch_identity")
        if identity and worker.alive(identity):
            if not (folder / "launch-admitted.json").exists() and wi.exists():
                admit_worker(active, self.root)
            return
        if identity and worker.process_group_members(identity["pid"]):
            return
        # Even a failed command receipt can accompany an interrupted resume.
        # This resource reconciliation precedes per-attempt quarantine handling.
        worker.reconcile_dead_guard(folder)
        try:
            if not receipt.exists():
                report = {
                    "status": "interrupted_uncertain",
                    "error": "Worker ended without immutable receipt",
                }
            else:
                report = c.read(receipt)
                c.require(report["spec"] == active["spec"], "Worker receipt belongs to another stage")
                if report["status"] == "stage_verified":
                    for ref in report["outputs"]:
                        c.verified(ref)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report = {"status": "receipt_rejected", "error": str(exc)}
        status = "verified" if report["status"] == "stage_verified" else "quarantined"
        result = {
            "status": status,
            "attempt": str(folder),
            "number": active["number"],
            "receipt": c.pin(receipt) if receipt.exists() else None,
            "error": report.get("error"),
            "finished_utc": c.now(),
        }
        if active["kind"] == "qa":
            item = next(x for x in self.config["pending_pairs"] if x["run_id"] == active["run_id"])
            if status == "verified":
                try:
                    result.update(automatic_receipt(item, self.config))
                    result["status"] = "automatic_accepted"
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    result.update(status="quarantined", error=str(exc))
            self.state["runs"][item["run_id"]] = result
        elif active["kind"] == "calibration_batch":
            result["accepted_ids"] = active.get("accepted_ids", [])
            self.state["calibration_batches"].append(result)
        else:
            self.state["stages"][active["kind"]] = result
        self.state["active"] = None
        self.save()

    def accepted_ids(self):
        return sorted(
            rid
            for rid, row in self.state["runs"].items()
            if row["status"] in ("automatic_accepted", "preserved_accepted")
        )

    def validate_completed(self):
        accepted = self.accepted_ids()
        c.require(
            set(accepted) == set(self.source) and len(accepted) == len(self.source),
            "Completion requires every original identity; no quiet drops",
        )
        rows = []
        for rid in accepted:
            row = self.state["runs"][rid]
            for key in ("result", "video"):
                c.verified(row[key])
            if row["status"] == "automatic_accepted":
                item = next(x for x in self.config["pending_pairs"] if x["run_id"] == rid)
                actual = automatic_receipt(item, self.config)
                c.require(
                    all(row[key] == actual[key] for key in ("result", "video", "receipt")),
                    "Automatic accepted evidence changed after checkpoint",
                )
            rows.append(
                dict(
                    run=self.source[rid],
                    accepted_result=row["result"],
                    video=row["video"],
                    acceptance_receipt=row.get("receipt"),
                    preserved_automatic_evidence=row.get("automatic_evidence"),
                )
            )
        doc = {
            "schema_version": 1,
            "status": "all_native_acceptances_verified",
            "requested_count": len(self.source),
            "actions": self.config["actions"],
            "source_timing_classes": dict(Counter(row["timing_class"] for row in self.source.values())),
            "conditional_physical_precision_bound_ms": None,
            "unknown_control_prefix_ordinals": self.config.get("unknown_control_prefix_ordinals", []),
            "runs": rows,
            "supervisor_config": c.pin(self.config_path),
        }
        path = self.state_root / "completed-accepted-set.json"
        if path.exists():
            c.require(c.read(path) == doc, "Completed accepted set changed")
        else:
            c.write_new(path, doc)

    def tick(self):
        self.verify_inputs()
        self.finish_active()
        if self.state["active"]:
            self.state["status"] = "stage_running"
            self.save()
            return "running"
        for item in self.config["pending_pairs"]:
            if item["run_id"] in self.state["runs"]:
                continue
            draft = Path(item["output"]) / "review-draft.json"
            if not draft.exists():
                continue
            try:
                data = c.read(draft)
                c.require(
                    data["run_id"] == item["run_id"] and data["ordinal"] == item["ordinal"],
                    "Prepared audit identity differs",
                )
                # Existing partial machine output/result has no receipt accepted
                # by this pipeline. Do not invoke a mutating command to guess.
                c.require(
                    not (Path(item["output"]) / "automatic-policy-v1").exists(),
                    "Unowned/partial automatic attempt requires reconciliation",
                )
                c.require(
                    not (Path(self.config["accepted_root"]) / item["run_id"]).exists(),
                    "Unowned/partial accepted output requires explicit reconciliation",
                )
                self.launch("qa-" + item["run_id"], self.config["qa"], item)
                return "running"
            except (OSError, ValueError, KeyError, TypeError) as exc:
                if self.state["active"] is not None:
                    self.state["active"]["launch_error"] = str(exc)
                    self.save()
                    return "running"
                self.state["runs"][item["run_id"]] = {
                    "status": "quarantined",
                    "error": str(exc),
                    "finished_utc": c.now(),
                }
                self.save()
        accepted = self.accepted_ids()
        batches = self.state["calibration_batches"]
        last_ids = batches[-1].get("accepted_ids", []) if batches else []
        initial = len(self.config["preserved"])
        new = len(set(accepted) - set(last_ids)) if batches else len(accepted) - initial
        threshold = self.config.get("calibration_batch_size", 10)
        if len(accepted) < len(self.source) and new >= threshold:
            # A failed batch is not retried on the same completed set. Later
            # independent accepted runs can trigger a fresh explicit batch.
            self.launch("calibration-batch-%04d" % (len(batches) + 1), self.config["calibration_batch"])
            self.state["active"]["accepted_ids"] = accepted
            self.save()
            return "running"
        if len(accepted) == len(self.source):
            self.validate_completed()
            for stage in self.config["final_stages"]:
                prior = self.state["stages"].get(stage["name"])
                if prior and prior["status"] == "verified":
                    for ref in c.read(c.verified(prior["receipt"]))["outputs"]:
                        c.verified(ref)
                    continue
                if prior:
                    if stage.get("retry") != "safe_resume" or prior["number"] >= stage.get("max_attempts", 3):
                        self.state["status"] = "final_stage_quarantined"
                        self.save()
                        return "blocked"
                    import datetime

                    elapsed = (
                        datetime.datetime.now(datetime.timezone.utc)
                        - datetime.datetime.fromisoformat(prior["finished_utc"])
                    ).total_seconds()
                    if elapsed < stage.get("retry_delay_seconds", 60):
                        self.state["status"] = "waiting_for_safe_stage_retry"
                        self.save()
                        return "waiting"
                    number = prior["number"] + 1
                else:
                    number = 1
                self.launch(stage["name"], stage, number=number)
                return "running"
            self.state["status"] = "complete_dataset_verified"
            self.save()
            return "complete"
        watcher_state = scheduler.read_progress(Path(self.config["watcher_state"]) / "progress.json")
        pending = [r for r in self.config["pending_pairs"] if r["run_id"] not in self.state["runs"]]
        failures = watcher_state.get("runs", {})
        for item in pending:
            prior = failures.get(item["run_id"], {})
            if prior.get("status") == "failed":
                self.state["runs"][item["run_id"]] = {
                    "status": "quarantined",
                    "error": "Preparation failed",
                    "preparation": prior,
                }
        remaining = [r for r in self.config["pending_pairs"] if r["run_id"] not in self.state["runs"]]
        self.state["accepted_count"] = len(accepted)
        self.state["quarantined_count"] = sum(
            r["status"] == "quarantined" for r in self.state["runs"].values()
        )
        self.state["status"] = (
            "waiting_for_native_candidates" if remaining else "incomplete_dataset_quarantined"
        )
        for upstream in self.config.get("upstream_workers", []):
            folder = Path(upstream)
            terminal = folder / "exit.json"
            if remaining and terminal.exists() and c.read(terminal).get("returncode") != 0:
                self.state["status"] = "upstream_worker_stopped_incomplete"
                self.state["upstream_failure"] = c.pin(terminal)
                self.save()
                return "blocked"
        self.save()
        return "waiting" if remaining else "blocked"


def run(config_path, once=False):
    config_path = Path(config_path).resolve(strict=True)
    config = c.read(config_path)
    c.require(config["status"] == "frozen_automatic_dataset_pipeline", "Not a frozen configuration")
    state_root = Path(config["state_root"])
    state_root.mkdir(parents=True, exist_ok=True)
    with (state_root / "supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        c.atomic(
            state_root / "owner.json",
            {
                "identity": scheduler.identity(os.getpid()),
                "config": c.pin(config_path),
                "started_utc": c.now(),
            },
        )
        engine = Supervisor(config_path, config, state_root)
        engine.verify_inputs()
        engine.preserve()
        engine.save()
        while True:
            status = engine.tick()
            print(
                json.dumps(
                    {
                        "utc": c.now(),
                        "status": status,
                        "accepted": len(engine.accepted_ids()),
                        "active": engine.state["active"]["key"] if engine.state["active"] else None,
                    }
                ),
                flush=True,
            )
            if status in ("complete", "blocked") or once:
                return 0 if status == "complete" else (2 if status == "blocked" else 3)
            time.sleep(config.get("poll_seconds", 15))


def single_stage(spec_path, attempt):
    """Explicit one-stage smoke, using the same launch barrier and guard worker."""
    spec_path = Path(spec_path).resolve(strict=True)
    spec = c.read(spec_path)
    attempt = Path(attempt).resolve()
    attempt.mkdir(parents=True, exist_ok=False)
    c.write_new(attempt / "spec.json", spec)
    active = {"attempt": str(attempt), "spec": c.pin(attempt / "spec.json")}
    c.write_new(attempt / "one-stage-request.json", {"source_spec": c.pin(spec_path), "created_utc": c.now()})
    with (attempt / "worker.log").open("xb") as log:
        child = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(HERE / "dataset_pipeline_worker_v1.py"),
                str(attempt / "spec.json"),
                "--attempt",
                str(attempt),
            ],
            cwd=spec["cwd"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    c.write_new(
        attempt / "one-stage-launch.json", {"identity": scheduler.identity(child.pid), "created_utc": c.now()}
    )
    deadline = time.monotonic() + 30
    while not (attempt / "worker.json").exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if (attempt / "worker.json").exists() and child.poll() is None:
        admit_worker(active, spec["cwd"])
    code = child.wait()
    worker.reconcile_dead_guard(attempt)
    c.require(
        (attempt / "receipt.json").exists(), "One-stage worker lacks receipt; preserve uncertain attempt"
    )
    receipt = c.read(attempt / "receipt.json")
    c.require(receipt["spec"] == active["spec"], "One-stage receipt identity differs")
    print(
        json.dumps(
            {"returncode": code, "receipt": c.pin(attempt / "receipt.json"), "status": receipt["status"]}
        )
    )
    return 0 if code == 0 and receipt["status"] == "stage_verified" else 2


def qa_spec(proposal_path, ordinal, output):
    """Build one exact pending QA task for the parent-owned live smoke."""
    proposal = c.read(proposal_path)
    root = Path(proposal["root"]).resolve(strict=True)
    queue = c.pin(root / proposal["audit_queue"])
    document = c.read_verified(queue)
    item = next(x for x in document["runs"] if x["ordinal"] == ordinal)
    actions = c.read(root / proposal["actions"])
    run = next(r for r in actions["runs"] if r["ordinal"] == ordinal)
    c.require(item["run_id"] == run["run_id"], "QA smoke source identity differs")
    c.require(
        not (root / proposal["accepted_root"] / item["run_id"]).exists(),
        "QA smoke accepted destination already exists; preserve it",
    )
    c.require(
        not (Path(item["output"]) / "automatic-policy-v1").exists(),
        "Prior automatic attempt requires explicit reconciliation",
    )
    reference = c.pin(root / proposal["automatic_reference"])
    variables = {
        "root": str(root),
        "accepted_dir": str(root / proposal["accepted_root"]),
        "reference": reference["path"],
        "audit_dir": item["output"],
        "run_id": item["run_id"],
        "ordinal": str(ordinal),
        "candidate_clean": item["clean_candidate"],
        "candidate_clock": item["clock_candidate"],
    }
    spec = c.expand(proposal["qa"], variables)
    c.require(spec["guard"] is True and spec["retry"] == "never", "Smoke requires guarded one-shot QA")
    inputs = [
        queue,
        reference,
        c.pin(root / proposal["actions"]),
        c.pin(Path(item["output"]) / "review-draft.json"),
        c.pin(item["clean_candidate"]),
        c.pin(item["clock_candidate"]),
    ]
    for p in [
        Path(__file__),
        HERE / "pipeline_common_v1.py",
        HERE / "dataset_pipeline_worker_v1.py",
        HERE / "serial_accept_review_v3.py",
        HERE / "production_video_audit_automatic_v1.py",
        HERE / "automatic_video_policy_v1.py",
    ]:
        inputs.append(c.pin(p))
    spec.update(
        inputs=inputs,
        cwd=str(root),
        env=proposal.get("env", {}),
        guard={"config": queue, "state": str(root / proposal["watcher_state"])},
    )
    c.write_new(output, spec)
    print(
        json.dumps(
            {"spec": c.pin(output), "ordinal": ordinal, "run_id": item["run_id"], "launched_workers": 0}
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("configure")
    a.add_argument("proposal", type=Path)
    a.add_argument("--output", type=Path, required=True)
    a = sub.add_parser("run")
    a.add_argument("config", type=Path)
    a.add_argument("--once", action="store_true")
    a = sub.add_parser("stage")
    a.add_argument("spec", type=Path)
    a.add_argument("--attempt", type=Path, required=True)
    a = sub.add_parser("qa-spec")
    a.add_argument("proposal", type=Path)
    a.add_argument("--ordinal", type=int, required=True)
    a.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.command == "configure":
        compile_config(a.proposal.resolve(strict=True), a.output.resolve())
    elif a.command == "qa-spec":
        qa_spec(a.proposal.resolve(strict=True), a.ordinal, a.output.resolve())
    elif a.command == "stage":
        raise SystemExit(single_stage(a.spec, a.attempt))
    else:
        raise SystemExit(run(a.config, a.once))
