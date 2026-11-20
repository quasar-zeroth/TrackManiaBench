#!/usr/bin/env python3
"""Guarded fixed-slot readback/cache proof and native export candidate queue.

`validate` is local only. `seed` writes the isolated slot but sends no GUI input.
`cache-proof` and `run` require --execute and reviewed fixed-slot plans. Neither
    mode accepts/aligns a dataset; final acceptance stays in the existing renderer.
An uncertain transaction is never resumed or rendered again automatically.
"""

from __future__ import annotations
import argparse
import base64
import contextlib
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup"
import runtime

OPTS = runtime.SSH_OPTIONS
DOCS = runtime.value("GAME_DOCUMENTS")


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


ui = module("fixed_slot_ui", SETUP / "native_ui_worker.py")
native = module("fixed_slot_native_coverage", ROOT / "renderer/native_ui_queue_partial_v1.py")
admission_bridge = module("fixed_slot_coverage_bridge", SETUP / "admission.py")
common = native.common
recovery = module("fixed_slot_recovery", SETUP / "admission.py")


def require(ok, message):
    if not ok:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text())


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def sha(path):
    return common.sha256(path)


def pin(path):
    path = Path(path).resolve(strict=True)
    return {"path": str(path), "sha256": sha(path)}


def write_new(path, value):
    with Path(path).open("xb") as f:
        f.write(encoded(value))
        f.flush()
        os.fsync(f.fileno())


def safe(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,90}", value),
        "Unsafe job/queue ID",
    )
    return value


def checked_reference(value):
    path = Path(value["path"]).resolve(strict=True)
    require(sha(path) == value["sha256"], "Pinned evidence changed: " + str(path))
    return path


def local_path(value, base):
    path = Path(value)
    return (base / path if not path.is_absolute() else path).resolve(strict=True)


def fingerprint(inspection):
    entities = [
        b for t in inspection["tracks"] for b in t["blocks"] if b["class"] == "CGameCtnMediaBlockEntity"
    ]
    require(len(entities) == 1, "Cache proof needs exactly one source entity")
    return hashlib.sha256(encoded(entities[0]["record_data"])).hexdigest()


def validate_cache_sequence(jobs):
    require(len(jobs) == 5, "Cache proof must be A-clean/B-clean/A-clean/A-clock/A-clean")
    require(
        [j["role"] for j in jobs] == ["clean", "clean", "clean", "clock", "clean"],
        "Wrong cache-proof variants",
    )
    a, b, c, d, e = jobs
    require(
        a["run"]["run_id"] == c["run"]["run_id"] == d["run"]["run_id"] == e["run"]["run_id"]
        and a["run"]["run_id"] != b["run"]["run_id"],
        "Cache proof must return to the same A run",
    )
    require(
        a["clip"]["sha256"] == c["clip"]["sha256"] == e["clip"]["sha256"],
        "A-clean source changed within cache proof",
    )
    require(
        a["trajectory_sha256"] == c["trajectory_sha256"] == d["trajectory_sha256"] == e["trajectory_sha256"]
        and a["trajectory_sha256"] != b["trajectory_sha256"],
        "A/B need genuinely distinct source trajectories",
    )
    require(a["clip"]["sha256"] != d["clip"]["sha256"], "Clean/clock source bytes must differ")


def prepare(config_path, need_plans=True):
    config_path = config_path.resolve(strict=True)
    c = read(config_path)
    base = config_path.parent
    require(c.get("schema_version") == 1, "Unsupported fixed-slot schema")
    safe(c["queue_id"])
    manifest_path = local_path(c["manifest"], base)
    manifest = common.normalise_manifest(manifest_path, ROOT, "")
    by_rank = {r["ordinal"]: r for r in manifest["runs"]}
    jobs = []
    for item in c["jobs"]:
        safe(item["job_id"])
        require(item["role"] in ("clean", "clock"), "Wrong job role")
        run = by_rank[int(item["ordinal"])]
        clip = local_path(item["clip"], base)
        inspection = local_path(item["inspection"], base)
        meta = local_path(item["action_metadata"], base)
        provenance = local_path(item["provenance"], base)
        native.metadata(meta, run["ghost_sha256"], run, manifest["map"]["uid"])
        purpose = "production" if item["role"] == "clean" else "diagnostic"
        timeline = native.clip_inspection(clip, inspection, run, meta.parent / "visual_samples.csv", purpose)
        doc = read(inspection)
        blocks = [b for t in doc["tracks"] for b in t["blocks"]]
        require(
            sum(b["class"] == "CGameCtnMediaBlockText" for b in blocks)
            == (0 if item["role"] == "clean" else 3),
            "Role differs from actual clean/clock scene",
        )
        construction = read(provenance)
        require(
            construction.get("output_clip_sha256") == sha(clip)
            and construction.get("record_data_payload_byte_equal") is True
            and construction.get("original_record_data_payload_sha256")
            == construction.get("output_record_data_payload_sha256"),
            "Missing unchanged RecordData construction sidecar",
        )
        job = {
            "job_id": item["job_id"],
            "role": item["role"],
            "run": run,
            "clip": pin(clip),
            "inspection": pin(inspection),
            "action_metadata": pin(meta),
            "provenance": pin(provenance),
            "trajectory_sha256": fingerprint(doc),
            "timeline": timeline,
        }
        for name in ("native_replay", "map_comparison", "replay_action_metadata"):
            if name in item:
                source = (
                    checked_reference(item[name])
                    if isinstance(item[name], dict)
                    else local_path(item[name], base)
                )
                job[name] = pin(source)
        jobs.append(job)
    require(
        0 < len(jobs) <= 500 and len({j["job_id"] for j in jobs}) == len(jobs),
        "Empty, oversized or duplicate jobs",
    )
    plans = {}
    if need_plans:
        require(
            set(c["plans"])
            in (
                {"clear", "import", "export", "prepare", "render"},
                {"refresh", "clear", "import", "export", "prepare", "render"},
            ),
            "Incomplete plan bundle",
        )
        require(
            c.get("plans_reviewed_by") and c.get("plans_reviewed_utc"),
            "Fixed-slot plans require recorded review",
        )
        for name, value in c["plans"].items():
            path = local_path(value, base)
            document = read(path)
            for st in document["states"].values():
                for region in st["regions"]:
                    region["image"] = str(local_path(region["image"], path.parent))
            ui.validate_plan(document)
            plans[name] = document
        typed = [s["action"].get("text") for s in plans["export"]["steps"] if s["action"]["kind"] == "type"]
        require(typed == ["DatasetReadback"], "Export plan must use the exact owned fixed basename")
    result = {
        "schema_version": 1,
        "queue_id": c["queue_id"],
        "source_config": pin(config_path),
        "manifest": manifest,
        "source_manifest": pin(manifest_path),
        "jobs": jobs,
        "plans": plans,
        "settings": common.settings_for_backend("native_ui"),
    }
    result["source_admission_tools"] = native.admission_tools()
    return c, result


def plan_signature(plans):
    # Paths may move when freezing. Canonicalize references to actual PNG hashes.
    canonical = json.loads(json.dumps(plans))
    for document in canonical.values():
        for state in document["states"].values():
            for region in state["regions"]:
                region["image"] = sha(region["image"])
    return hashlib.sha256(encoded(canonical)).hexdigest()


def cache_binding(prepared, tooling):
    return admission_bridge.cache_binding(prepared, tooling)


def validate_cache_proof(path, binding):
    proof = read(path)
    require(proof.get("status") == "fixed_slot_cache_readbacks_verified", "Cache proof has not passed")
    require(proof.get("binding") == binding, "Cache proof differs in map, plans, slot, or tooling")
    require(len(proof.get("results", [])) == 5, "Cache proof lacks five preserved readbacks")
    jobs = []
    for item in proof["results"]:
        result = read(checked_reference(item))
        require(result["status"] == "native_readback_candidate", "Cache result is not a completed readback")
        require(result["identity"] == proof["identity"], "Cache proof spans different game process lifetimes")
        jobs.append(result["job"])
        proof_path = checked_reference(result["readback_proof"])
        comparison = read(proof_path)
        require(
            comparison["all_exposed_track_fields_equal"] and comparison["all_exposed_block_fields_equal"],
            "Cache comparison did not establish scene equality",
        )
        for ref in comparison["inputs"].values():
            checked_reference(ref)
        checked_reference(comparison["native_readback_inspection"])
        require(
            comparison["source_sha256"] == result["job"]["clip"]["sha256"]
            and comparison["native_readback_sha256"] == result["readback"]["sha256"],
            "Cache comparison is bound to different source/readback bytes",
        )
        checked_reference(result["readback"])
        checked_reference(result["readback_closure"])
    validate_cache_sequence(jobs)
    return proof


def tool_pins():
    return recovery.tool_pins(Path(__file__), native)


class Queue:
    def __init__(self, c, prepared, out, tooling):
        self.config = c
        self.prepared = prepared
        self.out = out
        self.tooling = tooling
        out.mkdir(parents=True, exist_ok=False)
        self.journal = (out / "events.jsonl").open("x", buffering=1)
        self.identity = None
        self.lock_process = None
        self.plans = {}
        write_new(out / "prepared.json", prepared)
        write_new(out / "tooling.json", tooling)
        folder = out / "plans"
        folder.mkdir()
        refs = folder / "references"
        refs.mkdir()
        for name, document in prepared["plans"].items():
            document = json.loads(json.dumps(document))
            for state in document["states"].values():
                for region in state["regions"]:
                    source = Path(region["image"])
                    dest = refs / (sha(source) + ".png")
                    if not dest.exists():
                        shutil.copyfile(source, dest)
                    region["image"] = str(dest.resolve())
            path = folder / (name + ".json")
            write_new(path, document)
            self.plans[name] = pin(path)

    def emit(self, event, **fields):
        row = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "event": event, **fields}
        self.journal.write(json.dumps(row) + "\n")
        self.journal.flush()
        os.fsync(self.journal.fileno())
        print(json.dumps(row), flush=True)

    def check_tools(self):
        for ref in self.tooling.values():
            checked_reference(ref)
        if self.lock_process is not None:
            require(self.lock_process.poll() is None, "Exclusive remote queue lock was lost")

    def remote(self, operation, **fields):
        self.check_tools()
        payload = {"operation": operation, "queue_id": self.prepared["queue_id"], **fields}
        source = recovery.storage_source()
        payload["storage_verifier_sha256"] = hashlib.sha256(source.encode()).hexdigest()
        raw = subprocess.check_output(
            ["ssh", "-T", *OPTS, runtime.value("SSH_HOST"), "python3 -c " + shlex.quote(source)],
            input=encoded(payload),
            timeout=180,
        )
        return json.loads(raw)

    @contextlib.contextmanager
    def lock(self):
        # Shared with the existing native/plugin supervisors. It is cooperative;
        # the operator must still give this process sole desktop ownership.
        local = SETUP / ".native-ui-controller.lock"
        with local.open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            script = (
                "import fcntl,sys;f=open(" + repr(DOCS + "/.dataset-renderer.lock") + ',"a");'
                'fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);print("locked",flush=True);sys.stdin.buffer.read()'
            )
            process = subprocess.Popen(
                ["ssh", "-T", *OPTS, runtime.value("SSH_HOST"), "python3 -c " + shlex.quote(script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                ready, _, _ = select.select([process.stdout], [], [], 15)
                require(
                    ready and process.stdout.readline().strip() == "locked",
                    "Could not acquire remote renderer lock",
                )
                self.lock_process = process
                self.emit("exclusive_lock_acquired")
                yield
            finally:
                self.lock_process = None
                if process.stdin:
                    process.stdin.close()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=5)

    def initialize(self):
        tools = {}
        for name in ("native_ui_queue.py", "render_queue.py"):
            path = ROOT / "renderer" / name
            tools[name] = {"data": base64.b64encode(path.read_bytes()).decode(), "sha256": sha(path)}
        initial = None
        if self.config.get("initial_seed"):
            seed = read(checked_reference(self.config["initial_seed"]))
            require(
                seed["remote"] == DOCS + "/Replays/Clips/DatasetQueue/DatasetSlot.Clip.Gbx",
                "Initial seed used a different slot",
            )
            require(sha(seed["source"]) == seed["sha256"], "Initial seed source changed")
            initial = seed["sha256"]
        self.emit("storage_initialized", **self.remote("init", tools=tools, initial_seed_sha256=initial))

    def copy_remote(self, source, destination, digest=None):
        require(not destination.exists(), "Local archive destination exists")
        subprocess.run(
            ["scp", *OPTS, runtime.value("SSH_HOST") + ":" + source, str(destination)],
            check=True,
            timeout=180,
            stdout=subprocess.DEVNULL,
        )
        if digest:
            require(sha(destination) == digest, "Remote/local preserved bytes differ")

    def phase(self, name, jobout, job_id, observe_only=False):
        self.check_tools()
        if self.identity:
            self.remote("identity", identity=self.identity)
        path = checked_reference(self.plans[name])
        destination = jobout / name
        args = [
            sys.executable,
            str(SETUP / "native_ui_worker.py"),
            "run",
            str(path),
            "--output",
            str(destination),
        ]
        if not observe_only:
            args += ["--execute"]
        if self.identity:
            args += ["--window", str(self.identity["window"])]
        self.emit("phase_started", job_id=job_id, phase=name, observe_only=observe_only)
        # Bound the host even if SSH/tooling stalls. No action is retried after timeout.
        budget = sum(s.get("timeout_seconds", 30) * 2 + 15 for s in read(path)["steps"]) + 45
        with (jobout / (name + ".log")).open("x") as log:
            subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=budget, cwd=ROOT)
        events = [json.loads(line) for line in (destination / "events.jsonl").read_text().splitlines()]
        require(
            events[-1]["event"] == ("dry_run_complete" if observe_only else "sequence_complete"),
            "UI phase did not finish",
        )
        identity = next(e for e in events if e["event"] == "identity_verified")
        current = {k: identity[k] for k in ("window", "pid", "process_start", "size", "origin")}
        if self.identity is None:
            self.identity = current
        else:
            require(self.identity == current, "Game/window identity differs between phases")
        self.remote("identity", identity=self.identity)
        self.emit("phase_completed", job_id=job_id, phase=name, identity=self.identity)
        last = next(e for e in reversed(events) if e["event"] == "screenshot")
        return pin(destination / last["screenshot"])

    def stage(self, job, seed=False):
        data = checked_reference(job["clip"]).read_bytes()
        return self.remote(
            "stage",
            job_id=job["job_id"],
            data=base64.b64encode(data).decode(),
            sha256=job["clip"]["sha256"],
            seed_only=seed,
        )

    def job(self, job, number=None):
        job_id = job["job_id"]
        folder = self.out / job_id
        folder.mkdir()
        for field, name in (("clip", "intended.Clip.Gbx"), ("inspection", "intended.inspected.json")):
            source = checked_reference(job[field])
            shutil.copyfile(source, folder / name)
        write_new(folder / "job.json", job)
        # Observe editor state before touching the shared slot. No GUI input here.
        self.phase("clear", folder, job_id, observe_only=True)
        # Give the actual clear execution its own untouched evidence directory.
        (folder / "clear").rename(folder / "initial-editor-state")
        (folder / "clear.log").rename(folder / "initial-editor-state.log")
        self.emit("source_staged", **self.stage(job))
        if "refresh" in self.plans:
            self.phase("refresh", folder, job_id)
        self.phase("clear", folder, job_id)
        self.phase("import", folder, job_id)
        export = self.phase("export", folder, job_id)
        archived = self.remote("archive_readback", job_id=job_id, identity=self.identity)
        readback = folder / "native-readback.Clip.Gbx"
        self.copy_remote(archived["path"], readback, archived["sha256"])
        self.copy_remote(archived["closure"], folder / "readback-closure.json")
        prefix = folder / "readback"
        self.check_tools()
        with (folder / "readback-validation.log").open("x") as log:
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "dataset/verify_native_readback_v041.py"),
                    "--intended-clip",
                    str(folder / "intended.Clip.Gbx"),
                    "--intended-inspection",
                    str(folder / "intended.inspected.json"),
                    "--readback",
                    str(readback),
                    "--output-prefix",
                    str(prefix),
                    "--inspector",
                    self.config["inspector"],
                    "--dotnet",
                    self.config["dotnet"],
                ],
                check=True,
                cwd=ROOT,
                timeout=180,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        proof = folder / "readback-comparison.json"
        result = {
            "schema_version": 1,
            "status": "native_readback_candidate",
            "job": job,
            "identity": self.identity,
            "readback": pin(readback),
            "readback_proof": pin(proof),
            "export_observation": export,
            "readback_closure": pin(folder / "readback-closure.json"),
            "accepted": False,
            "aligned": False,
            "source_admission_tools": self.prepared["source_admission_tools"],
        }
        self.emit("native_readback_verified", job_id=job_id, proof=pin(proof))
        if number is not None:
            before = self.remote("empty_inventory")
            name = f"Video{number:02d}.webm"
            require(
                name not in before and "DatasetQueueVideo.webm" not in before,
                "Expected or fixed requested video already exists; do not render",
            )
            result["settings_observation"] = self.phase("prepare", folder, job_id)
            require(
                self.remote("empty_inventory") == before,
                "Staging changed during export preparation; do not render",
            )
            write_new(folder / "pre-render-inventory.json", before)
            self.emit("render_once_committed", job_id=job_id, expected_video=name)
            result["completion_observation"] = self.phase("render", folder, job_id)
            video = self.remote(
                "harvest", job_id=job_id, identity=self.identity, inventory=before, video_name=name
            )
            local_video = folder / "native-video.webm"
            self.copy_remote(video["path"], local_video, video["sha256"])
            self.copy_remote(video["closure"], folder / "video-closure.json")
            self.copy_remote(
                video["name_receipt"], folder / "video-name-receipt.json", video["name_receipt_sha256"]
            )
            self.copy_remote(video["preservation_closure"], folder / "video-preservation-closure.json")
            self.check_tools()
            check = common.video_check(local_video, job["timeline"]["expected_native_duration_seconds"])
            require(
                check["codec"] == "vp8" and check["audio_streams"] == 0, "Unexpected native video codec/audio"
            )
            result.update(
                status="native_video_candidate",
                video=pin(local_video),
                video_check=check,
                closure=pin(folder / "video-closure.json"),
                source_video=video["source"],
                native_original_source=video["native_original_source"],
                output_name_provenance=pin(folder / "video-name-receipt.json"),
                preservation_closure=pin(folder / "video-preservation-closure.json"),
            )
        result["guarded_sequences"] = {
            name: {"plan": ref, "events": pin(folder / name / "events.jsonl")}
            for name, ref in self.plans.items()
            if (folder / name / "events.jsonl").exists()
        }
        result["review_flags"] = {
            "visual_review_passed": False,
            "settings_review_passed": False,
            "alignment_verified": False,
        }
        write_new(folder / "candidate.json", result)
        commit = {"result": pin(folder / "candidate.json"), "readback_sha256": sha(readback)}
        if number is not None:
            commit["video_sha256"] = result["video"]["sha256"]
        self.remote("commit", job_id=job_id, identity=self.identity, result=commit)
        self.emit("candidate_preserved", job_id=job_id, status=result["status"])
        return pin(folder / "candidate.json")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("validate", "seed", "cache-proof", "run"))
    p.add_argument("config", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--expected-video-start", type=int)
    p.add_argument("--cache-proof", type=Path)
    p.add_argument("--pilot-gate", type=Path)
    p.add_argument("--accepted-output", type=Path)
    p.add_argument("--completed-prefix", type=Path)
    p.add_argument("--remote-resume-snapshot", type=Path)
    p.add_argument("--attempt-id")
    p.add_argument(
        "--inspector",
        type=Path,
        default=Path(
            runtime.value("INSPECTOR", str(ROOT / "build/clip-inspector/TrackmaniaClipInspector.dll"))
        ),
    )
    p.add_argument("--dotnet", default=runtime.value("DOTNET", shutil.which("dotnet") or ""))
    args = p.parse_args()
    if args.attempt_id:
        safe(args.attempt_id)
    c, prepared = prepare(args.config, need_plans=args.command != "seed")
    recovery.validate_configuration(c, prepared, native, Path(__file__))
    tooling = tool_pins()
    if args.command in ("cache-proof", "run"):
        inspector = args.inspector.resolve(strict=True)
        require(args.dotnet, "A .NET runtime is required: use --dotnet or TRACKMANIABENCH_DOTNET")
        dotnet = Path(args.dotnet).resolve(strict=True)
        c.update(inspector=str(inspector), dotnet=str(dotnet))
        for path in [dotnet, *[x for x in inspector.parent.iterdir() if x.is_file()]]:
            tooling["inspector:" + str(path)] = pin(path)
    tooling.update(admission_bridge.execution_evidence(c, prepared, args, native))
    binding = cache_binding(prepared, tooling)
    resumed = recovery.prepare_resume(c, prepared, args)
    if resumed:
        tooling.update(resumed["evidence"])
    if args.command == "validate":
        print(
            json.dumps(
                {"jobs": len(prepared["jobs"]), "plan_signature": binding["plan_signature"], "gui_inputs": 0}
            )
        )
        return 0
    require(args.output is not None, "A new evidence output directory is required")
    if args.command == "cache-proof":
        validate_cache_sequence(prepared["jobs"])
        require(
            max(j["run"]["ordinal"] for j in prepared["jobs"]) <= 5,
            "Cache test must use downloaded pilot runs",
        )
    if args.command == "run":
        require(args.cache_proof is not None, "Same-path cache proof required before rendering")
        cache_proof = validate_cache_proof(args.cache_proof.resolve(strict=True), binding)
        require(
            args.expected_video_start is not None and args.expected_video_start >= 0,
            "Declare the expected first native VideoNN number",
        )
        require(
            len({(j["run"]["run_id"], j["role"]) for j in prepared["jobs"]}) == len(prepared["jobs"]),
            "Production queue duplicates a run/role",
        )
        for job in prepared["jobs"]:
            require(
                all(key in job for key in ("native_replay", "map_comparison", "replay_action_metadata")),
                "Production job lacks native replay, semantic map, or converted action evidence",
            )
        if any(j["run"]["ordinal"] > 5 for j in prepared["jobs"]):
            require(
                args.pilot_gate is not None and args.accepted_output is not None,
                "Verified top-five renderer gate required",
            )
            gate = common.validate_gate(
                args.pilot_gate.resolve(strict=True),
                prepared["manifest"],
                args.accepted_output.resolve(strict=True),
            )
            require(gate["renderer_backend"] == "native_ui", "Pilot used a different renderer")
    require(args.execute, "Review configuration, then use --execute; nothing was sent to the render machine")
    if args.command == "seed":
        prepared["queue_id"] += "-seed-" + str(int(time.time()))
    if resumed:
        prepared = dict(prepared, queue_id=args.attempt_id, resume_prefix=pin(args.completed_prefix))
    runtime.require_remote()
    queue = Queue(c, prepared, args.output.resolve(), tooling)
    if args.command == "run":
        queue.identity = cache_proof["identity"]
    try:
        with queue.lock():
            queue.initialize()
            if resumed:
                recovery.check_remote_resume(queue, resumed)
            if args.command == "seed":
                queue.emit("slot_seeded_for_manual_review", **queue.stage(prepared["jobs"][0], seed=True))
                return 0
            results = [] if resumed is None else list(resumed["candidate_results"])
            skipped = 0 if resumed is None else resumed["completed_count"]
            if resumed:
                queue.emit(
                    "verified_prefix_preserved", completed_count=skipped, prefix=pin(args.completed_prefix)
                )
            for index, job in enumerate(prepared["jobs"][skipped:]):
                results.append(
                    queue.job(
                        job, None if args.command == "cache-proof" else args.expected_video_start + index
                    )
                )
            if args.command == "cache-proof":
                proof = {
                    "schema_version": 1,
                    "status": "fixed_slot_cache_readbacks_verified",
                    "binding": binding,
                    "sequence": "A-clean/B-clean/A-clean/A-clock/A-clean",
                    "identity": queue.identity,
                    "results": results,
                    "verified_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }
                write_new(queue.out / "cache-proof.json", proof)
            write_new(
                queue.out / "completed.json",
                {"candidate_results": results, "accepted": False, "aligned": False},
            )
            queue.emit("queue_completed", candidates=len(results), accepted=False, aligned=False)
    except BaseException as exc:
        queue.emit("queue_stopped", reason=str(exc), automatic_retry=False)
        raise
    finally:
        queue.journal.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print("Stopped: " + str(exc), file=sys.stderr)
        raise SystemExit(2)
