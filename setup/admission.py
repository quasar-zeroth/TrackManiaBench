"""Reusable source admission, cache binding and explicit completed-prefix resume."""

import hashlib
import json
from pathlib import Path
import os
import runtime

ROOT = Path(__file__).resolve().parents[1]


def require(ok, message):
    if not ok:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text())


def pin(path):
    path = Path(path).resolve(strict=True)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def checked(ref):
    path = Path(ref["path"]).resolve(strict=True)
    require(pin(path)["sha256"] == ref["sha256"], "Pinned evidence changed: " + str(path))
    return path


def tool_pins(adapter, native):
    paths = [
        p
        for folder in ("setup", "renderer", "dataset", "extractor")
        for p in (ROOT / folder).rglob("*")
        if p.is_file()
        and p.suffix in (".py", ".cs", ".csproj")
        and not any(x in p.parts for x in ("bin", "obj", "__pycache__"))
    ]
    paths.extend(p for p in (ROOT / "dataset/clip-inspector/bin/Release/net8.0").glob("*") if p.is_file())
    for key in ("TRACKMANIABENCH_COVERAGE_POLICY", "TRACKMANIABENCH_CONTROL_EVIDENCE_INDEX"):
        if os.environ.get(key):
            paths.append(Path(os.environ[key]))
    return {str(p.resolve()): pin(p) for p in paths}


def storage_source():
    return runtime.remote_prelude() + (ROOT / "setup/storage.py").read_text()


def cache_binding(prepared, tooling):
    plans = json.loads(json.dumps(prepared["plans"]))
    for document in plans.values():
        for state in document["states"].values():
            for region in state["regions"]:
                region["image"] = pin(region["image"])["sha256"]
    return {
        "map_sha256": prepared["manifest"]["map"]["sha256"],
        "plan_signature": hashlib.sha256(
            (json.dumps(plans, sort_keys=True, indent=2) + "\n").encode()
        ).hexdigest(),
        "tooling": {
            name: ref["sha256"] for name, ref in tooling.items() if not name.startswith("execution:")
        },
        "runtime": {key: runtime.value(key) for key in runtime.KEYS},
        "slot": "Replays/Clips/DatasetQueue/DatasetSlot.Clip.Gbx",
        "readback": "Replays/Clips/DatasetReadback.Clip.Gbx",
    }


def validate_configuration(config, prepared, native, adapter):
    runs = prepared["manifest"]["runs"]
    require(1 <= len(runs) <= 250, "Manifest must contain 1..250 unique runs")
    require(
        [r["ordinal"] for r in runs] == list(range(1, len(runs) + 1)),
        "Manifest ordinals must be contiguous from one",
    )
    if prepared["plans"]:
        typed = [
            s["action"].get("text")
            for s in prepared["plans"]["prepare"]["steps"]
            if s["action"]["kind"] == "type"
        ]
        require(
            typed == ["DatasetQueueVideo"],
            "Prepare plan must use the owned export basename DatasetQueueVideo",
        )
    if config.get("source_admission_tools"):
        require(
            config["source_admission_tools"] == native.admission_tools(), "Source admission tools changed"
        )


def execution_evidence(config, prepared, args, native):
    refs = {"execution:config": prepared["source_config"]}
    if config.get("source_native_cohort"):
        cohort_ref = config["source_native_cohort"]
        cohort = read(checked(cohort_ref))
        require(
            cohort["actions_manifest"]["sha256"] == prepared["source_manifest"]["sha256"],
            "Cohort actions differ",
        )
        rows = {r["run"]["ordinal"]: r for r in cohort["runs"]}
        for job in prepared["jobs"]:
            row = rows[job["run"]["ordinal"]]
            require(
                all(
                    row["run"][k] == job["run"][k]
                    for k in ("run_id", "ordinal", "account_id", "ghost_sha256", "record_time_ms")
                ),
                "Cohort identity differs",
            )
            output = next(x for x in row["outputs"] if x["role"] == job["role"])
            for key in ("clip", "inspection"):
                require(
                    output[key]["sha256"] == job[key]["sha256"], "Job source differs from prepared cohort"
                )
            for key, source in [
                ("native_replay", "native_replay"),
                ("map_comparison", "replay_comparison"),
                ("replay_action_metadata", "native_action_metadata"),
            ]:
                require(
                    job[key]["sha256"] == row[source]["sha256"], "Job native evidence differs from cohort"
                )
            if row.get("source_clock"):
                refs["execution:clock:" + str(job["run"]["ordinal"])] = row["source_clock"]
                checked(row["source_clock"])
        refs["execution:cohort"] = cohort_ref
    return refs


def prepare_resume(config, prepared, args):
    if args.remote_resume_snapshot:
        raise ValueError(
            "Historic snapshot adapters are not supported; use a committed prefix and live read-only checks"
        )
    if not args.completed_prefix:
        return None
    require(
        args.command == "run" and args.attempt_id and args.attempt_id != prepared["queue_id"],
        "Resume requires run and a new --attempt-id",
    )
    prefix = read(args.completed_prefix)
    candidates = prefix.get("candidate_results", [])
    require(
        candidates and len(candidates) < len(prepared["jobs"]),
        "Resume requires a nonempty, incomplete committed prefix",
    )
    evidence = {"resume:prefix": pin(args.completed_prefix)}
    for index, ref in enumerate(candidates):
        path = checked(ref)
        candidate = read(path)
        job = prepared["jobs"][index]
        require(
            candidate.get("status") == "native_video_candidate" and candidate["job"] == job,
            "Completed prefix source/order differs",
        )
        for key in (
            "video",
            "readback",
            "readback_proof",
            "closure",
            "readback_closure",
            "output_name_provenance",
            "preservation_closure",
        ):
            evidence["resume:" + str(index) + ":" + key] = candidate[key]
            checked(candidate[key])
        evidence["resume:" + str(index)] = ref
    return {"candidate_results": candidates, "completed_count": len(candidates), "evidence": evidence}


def check_remote_resume(queue, resumed):
    queue.remote("resume_state")
    # Remote commits are checked independently of local candidate presence.
    for ref in resumed["candidate_results"]:
        candidate = read(checked(ref))
        queue.remote(
            "verify_commit",
            original_queue_id=read(Path(ref["path"]).parent.parent / "prepared.json")["queue_id"],
            job_id=candidate["job"]["job_id"],
            candidate=ref,
            readback_sha256=candidate["readback"]["sha256"],
            video_sha256=candidate["video"]["sha256"],
        )
