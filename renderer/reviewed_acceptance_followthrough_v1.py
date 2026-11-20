#!/usr/bin/env python3
"""Accept only independently completed image reviews, one local job at a time.

This does not create reviews, decode outside the frozen acceptance pipeline,
alter the render queue, or act on the game machine. A prior acceptance attempt without a
successful receipt stops execution and requires explicit reconciliation.
"""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def read_published(path):
    """Allow a short publication window, never silently skip malformed evidence."""
    for attempt in range(20):
        try:
            return read(path)
        except json.JSONDecodeError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def pin(path):
    path = Path(path).resolve(strict=True)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def verified(ref):
    if pin(ref["path"]) != {k: ref[k] for k in ("path", "sha256")}:
        raise ValueError("Pinned source changed: " + ref["path"])
    return Path(ref["path"])


def read_verified(ref):
    # Read once so JSON parsing uses exactly the bytes whose hash was checked.
    path = Path(ref["path"]).resolve(strict=True)
    data = path.read_bytes()
    if str(path) != ref["path"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
        raise ValueError("Pinned source changed: " + ref["path"])
    return json.loads(data)


def write_new(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def eligible_review(item, prepared):
    review = Path(item["output"]) / "explicit-review.json"
    if not review.exists():
        return None
    data = read_published(review)
    if data["run_id"] != item["run_id"] or data["ordinal"] != item["ordinal"]:
        raise ValueError("Explicit review identity differs")
    for field in (
        "completion_reviewed",
        "sampled_continuity_reviewed",
        "scene_unchanged_until_render",
        "settings_reviewed",
    ):
        if data.get(field) is not True:
            raise ValueError("Incomplete explicit review: " + field)
    if not data.get("verified_by") or not data.get("verified_utc") or not data.get("method"):
        raise ValueError("Review lacks actual reviewer identity/time/method")
    report_refs = [
        data[key] for key in ("reviewer_report", "independent_visual_review_report") if key in data
    ]
    if not report_refs or any(ref != report_refs[0] for ref in report_refs):
        raise ValueError("Missing or conflicting independent reviewer report")
    report = read_verified(report_refs[0])
    if report["run_id"] != item["run_id"] or report["ordinal"] != item["ordinal"]:
        raise ValueError("Reviewer report identity differs")
    if report.get("unresolved_issues") != []:
        raise ValueError("Unresolved or unspecified reviewer issues")
    if report.get("all_frames_manually_reviewed") is not False:
        raise ValueError("Expected sampled/flagged review scope")
    viewed = [report[key] for key in ("actual_viewed_frame_indices", "manual_frame_indices") if key in report]
    if not viewed or any(set(indices) != set(data["manual_frame_indices"]) for indices in viewed):
        raise ValueError("Actual-view report and explicit review indices differ")
    if any(report.get(key) != data[key] for key in ("verified_by", "verified_utc", "method")):
        raise ValueError("Reviewer identity/time/method differs from explicit review")
    draft_ref = prepared["review_draft"]
    if Path(draft_ref["path"]) != review.parent / "review-draft.json":
        raise ValueError("Prepared review draft path differs")
    draft = read_verified(draft_ref)
    if report.get("untouched_review_draft") != draft_ref:
        raise ValueError("Reviewer report does not pin the prepared draft")
    editable = {
        "status",
        "verified_by",
        "verified_utc",
        "method",
        "manual_frame_indices",
        "flag_resolutions",
        "marker_texts_reviewed",
        "natural_occlusions",
        "completion_reviewed",
        "sampled_continuity_reviewed",
        "scene_unchanged_until_render",
        "settings_reviewed",
        "settings_observed",
    }
    for key, value in draft.items():
        if key not in editable and data.get(key) != value:
            raise ValueError("Explicit review changed prepared evidence: " + key)
    for role in ("clean_candidate", "clock_candidate"):
        if prepared[role] != draft[role] or Path(prepared[role]["path"]) != Path(item[role]):
            raise ValueError("Prepared candidate identity differs: " + role)
    if not set(data["required_manual_frame_indices"]).issubset(data["manual_frame_indices"]):
        raise ValueError("Required images not actually reviewed")
    review_ref = pin(review)
    if read_verified(review_ref) != data:
        raise ValueError("Explicit review changed while being validated")
    return review_ref


def completed_receipt(path, item, accepted, review):
    receipt = read(path)
    if (
        receipt.get("run_id") != item["run_id"]
        or receipt.get("ordinal") != item["ordinal"]
        or receipt.get("status") != "actual_review_accepted_and_pipeline_verified"
    ):
        raise ValueError("Completion receipt identity/status differs")
    expected_paths = {
        "accepted_result": accepted,
        "explicit_review": review,
        "acceptance_receipt": review.parent / "acceptance-receipt.json",
        "scheduling_receipt": review.parent / "local-accept-scheduling.json",
    }
    for key, expected in expected_paths.items():
        if verified(receipt[key]) != expected:
            raise ValueError("Completion receipt path differs: " + key)
    verified(receipt["reviewer_report"])
    return receipt


def successful_acceptance(item, review_ref, accepted, scheduler_ref, queue_ref):
    review = verified(review_ref)
    data = read_verified(review_ref)
    report_ref = data.get("reviewer_report", data.get("independent_visual_review_report"))
    verified(report_ref)
    schedule_path = review.parent / "local-accept-scheduling.json"
    schedule = read(schedule_path)
    if (
        schedule.get("acceptance_exit_code") != 0
        or schedule.get("preparation_children_settled") is not True
        or schedule.get("review") != str(review)
        or schedule.get("watcher_config") != queue_ref["path"]
        or schedule.get("scheduler_source_sha256") != scheduler_ref["sha256"]
        or schedule.get("failure")
        or schedule.get("resume_failure")
    ):
        raise ValueError("Successful scheduling receipt missing or inconsistent: " + item["run_id"])
    proof_path = review.parent / "acceptance-receipt.json"
    proof = read(proof_path)
    if (
        proof.get("status") != "accepted_calibration_join_still_required"
        or proof.get("review") != review_ref
        or proof.get("source_clock_contract") != data["source_clock_contract"]
    ):
        raise ValueError("Acceptance receipt differs from reviewed evidence")
    # The frozen pipeline has already decoded and checked these artifacts. Check
    # its committed references once here; later loops check the receipt pins.
    for ref in proof["evidence_files"]:
        verified(ref)
    if read(accepted).get("run", {}).get("run_id") != item["run_id"]:
        raise ValueError("Accepted run identity differs")
    return {
        "finished_utc": now(),
        "ordinal": item["ordinal"],
        "run_id": item["run_id"],
        "explicit_review": review_ref,
        "reviewer_report": report_ref,
        "accepted_result": pin(accepted),
        "acceptance_receipt": pin(proof_path),
        "scheduling_receipt": pin(schedule_path),
        "status": "actual_review_accepted_and_pipeline_verified",
    }


def run(config_path, state):
    source_identity = {"config": pin(config_path), "tool": pin(__file__)}
    cfg = read_verified(source_identity["config"])
    queue_path = verified(cfg["watcher_config"])
    scheduler = verified(cfg["scheduler"])
    queue = read_verified(cfg["watcher_config"])
    if Path(cfg["accepted"]).resolve() != Path(queue["accepted_output"]).resolve():
        raise ValueError("Accepted output differs from watcher configuration")
    run_ids = {item["run_id"] for item in queue["runs"]}
    if (
        not run_ids
        or len(run_ids) != len(queue["runs"])
        or len({item["ordinal"] for item in queue["runs"]}) != len(run_ids)
    ):
        raise ValueError("Empty or duplicate configured runs")
    state.mkdir(parents=True, exist_ok=True)
    with (state / "followthrough.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not (state / "identity.json").exists():
            write_new(state / "identity.json", source_identity)
        elif read(state / "identity.json") != source_identity:
            raise ValueError("Followthrough source/config changed")
        while True:
            verified(source_identity["config"])
            verified(source_identity["tool"])
            verified(cfg["scheduler"])
            verified(cfg["watcher_config"])
            progress = read(Path(cfg["watcher_state"]) / "progress.json")
            if progress.get("config") != cfg["watcher_config"] or not set(progress["runs"]) <= run_ids:
                raise ValueError("Watcher progress config/run identities differ")
            completed = 0
            for item in queue["runs"]:
                rid = item["run_id"]
                accepted = Path(cfg["accepted"]).resolve() / rid / "result.json"
                review = Path(item["output"]).resolve() / "explicit-review.json"
                done = state / (rid + ".completed.json")
                if done.exists():
                    completed_receipt(done, item, accepted, review)
                    completed += 1
                    continue
                if accepted.exists():
                    raise ValueError("Unreconciled existing acceptance: " + rid)
                attempt = state / (rid + ".started.json")
                if attempt.exists() or (review.parent / "local-accept-scheduling.json").exists():
                    raise ValueError("Prior acceptance attempt requires reconciliation: " + rid)
                prepared = progress["runs"].get(rid, {})
                if prepared.get("status") == "failed":
                    raise ValueError("Preparation failed and requires reconciliation: " + rid)
                if prepared.get("status") != "prepared_review_required":
                    continue
                if prepared.get("ordinal") != item["ordinal"]:
                    raise ValueError("Prepared ordinal differs")
                review_ref = eligible_review(item, prepared)
                if review_ref is None:
                    continue
                review = verified(review_ref)
                command = [
                    sys.executable,
                    "-B",
                    str(scheduler),
                    "--watcher-config",
                    str(queue_path),
                    "--watcher-state",
                    cfg["watcher_state"],
                    "--review",
                    str(review),
                    "--accepted",
                    cfg["accepted"],
                ]
                write_new(
                    attempt,
                    {
                        "started_utc": now(),
                        "explicit_review": review_ref,
                        "scheduler": pin(scheduler),
                        "command": command,
                    },
                )
                print(
                    json.dumps(
                        {
                            "event": "actual_review_acceptance_started",
                            "ordinal": item["ordinal"],
                            "run_id": rid,
                            "utc": now(),
                        }
                    ),
                    flush=True,
                )
                with (state / (rid + ".accept.log")).open("x") as log:
                    process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
                if process.returncode:
                    raise RuntimeError("Acceptance failed for " + rid + "; inspect immutable attempt/log")
                verified(cfg["scheduler"])
                receipt = successful_acceptance(
                    item, review_ref, accepted, cfg["scheduler"], cfg["watcher_config"]
                )
                write_new(done, receipt)
                completed += 1
                print(json.dumps(receipt), flush=True)
            if completed == len(queue["runs"]):
                final = state / "complete.json"
                if final.exists():
                    prior = read(final)
                    if (
                        prior.get("status") != "all_configured_real_reviews_accepted"
                        or prior.get("count") != completed
                        or prior.get("config") != source_identity["config"]
                    ):
                        raise ValueError("Final completion receipt differs")
                else:
                    write_new(
                        final,
                        {
                            "status": "all_configured_real_reviews_accepted",
                            "count": completed,
                            "finished_utc": now(),
                            "config": source_identity["config"],
                        },
                    )
                return
            time.sleep(10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    run(args.config.resolve(strict=True), args.state.resolve())
