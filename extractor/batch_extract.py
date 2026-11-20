#!/usr/bin/env python3
"""Sequential, resumable action extraction from the collector's normalized manifest.

Pilot defaults to leaderboard positions 1-5. More than five needs an evidence-backed
pilot gate. This program never infers or marks frame/action alignment as verified.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

REQUIRED_FILES = {
    "actions_all_100hz.csv",
    "actions_race_100hz.csv",
    "input_events.json",
    "input_changes.json",
    "input_store_000.bin",
    "visual_samples.csv",
}
REQUIRED_GATE_CHECKS = {
    "correct_single_ghost",
    "fixed_camera_whole_run",
    "no_hud",
    "motion_blur_disabled",
    "frame_race_clock_alignment",
    "finish_covered",
    "repeat_render_consistency",
    "resume_preserves_outputs",
    "action_decode_complete",
}


def required_gate_checks(backend="openplanet"):
    if backend not in {"openplanet", "native_ui"}:
        raise ValueError("Unknown rendering backend: " + str(backend))
    checks = set(REQUIRED_GATE_CHECKS)
    if backend == "native_ui":
        checks.remove("motion_blur_disabled")
        checks.add("render_effect_controls_documented")
    return checks


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text())


def write_json(path: Path, value):
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def uuid_text(value, name):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value.lower():
        raise ValueError(f"Invalid {name}: {value!r}")
    return value.lower()


def int_value(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"Invalid positive integer {name}: {value!r}")
    return value


def source_path(text, base):
    if not isinstance(text, str) or not text or re.match(r"^[A-Za-z]:", text):
        raise ValueError("Expected a Linux file path from the normalized collector manifest")
    path = Path(text)
    return (path if path.is_absolute() else base / path).resolve(strict=True)


def verify_file(path, digest, size=None):
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"Missing/invalid source SHA256 for {path}")
    if not path.is_file() or (size is not None and path.stat().st_size != size) or sha(path) != digest:
        raise ValueError(f"File/hash/size mismatch: {path}")


def load_manifest(path, limit):
    original = read_json(path)
    if original.get("schema_version") != 1 or not isinstance(original.get("failures"), list):
        raise ValueError("Expected collector normalized schema version 1, including failures array")
    count = int_value(original.get("requested_count"), "requested_count")
    if limit < 1 or limit > count:
        raise ValueError("Manifest does not cover the requested run count")
    m = dict(original["map"])
    if not isinstance(m.get("uid"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", m["uid"]):
        raise ValueError("Invalid map UID")
    m["id"] = uuid_text(m.get("id"), "map ID")
    m["path"] = str(source_path(m["path"], path.parent))
    verify_file(Path(m["path"]), m["sha256"], m.get("bytes"))
    rows = []
    for original_row in original["runs"]:
        row = dict(original_row)
        row["run_id"] = uuid_text(row.get("run_id"), "record ID")
        row["account_id"] = uuid_text(row.get("account_id"), "account ID")
        for field in ["ordinal", "rank", "record_time_ms"]:
            row[field] = int_value(row.get(field), field)
        if row["ordinal"] > count or row.get("status") != "downloaded":
            raise ValueError("Unexpected ordinal or undownloaded row in normalized runs")
        row["ghost_path"] = str(source_path(row["ghost_path"], path.parent))
        verify_file(Path(row["ghost_path"]), row["ghost_sha256"], row.get("ghost_bytes"))
        rows.append(row)
    for key in ["run_id", "account_id", "ordinal", "ghost_sha256"]:
        if len({r[key] for r in rows}) != len(rows):
            raise ValueError(f"Duplicate {key} in manifest")
    if len(rows) + len(original["failures"]) != count:
        raise ValueError("Manifest's successes/failures do not cover requested_count")
    rows.sort(key=lambda r: r["ordinal"])
    chosen = [r for r in rows if r["ordinal"] <= limit]
    if [r["ordinal"] for r in chosen] != list(range(1, limit + 1)):
        raise ValueError(
            "Missing exact requested leaderboard positions; no lower-rank substitutions are allowed"
        )
    if [r["record_time_ms"] for r in chosen] != sorted(r["record_time_ms"] for r in chosen):
        raise ValueError("Leaderboard finish scores are out of order")
    return {**original, "map": m, "runs": chosen, "source_path": str(path), "source_sha256": sha(path)}


def extractor_package(executable, dotnet):
    executable = executable.resolve(strict=True)
    if not executable.is_file():
        raise ValueError("Extractor executable/DLL is not a file")
    package = executable.parent
    # Pin the shipped parser/runtime/dependency tree, not merely its small native launcher.
    files = {str(p.relative_to(package)): sha(p) for p in sorted(package.rglob("*")) if p.is_file()}
    if not files or "TrackmaniaExtractor.dll" not in files:
        raise ValueError("Expected a complete published extractor directory")
    command = [str(executable)]
    if executable.suffix.lower() == ".dll":
        if dotnet is None:
            raise ValueError("--dotnet is required when --extractor names the portable DLL")
        command.insert(0, str(dotnet.resolve(strict=True)))
    elif dotnet is not None:
        raise ValueError("Use --dotnet only with a DLL extractor")
    result = {"directory": str(package), "sha256": json_hash(files), "files": files}
    policy = os.environ.get("TRACKMANIABENCH_COVERAGE_POLICY")
    if policy:
        policy_path = Path(policy).resolve(strict=True)
        result["reviewed_coverage_policy"] = {
            "path": str(policy_path),
            "sha256": sha(policy_path),
            "bytes": policy_path.stat().st_size,
        }
    return command, result


def identity(manifest, run, package):
    return {
        "map_uid": manifest["map"]["uid"],
        "map_id": manifest["map"]["id"],
        "map_sha256": manifest["map"]["sha256"],
        "run_id": run["run_id"],
        "ghost_sha256": run["ghost_sha256"],
        "account_id": run["account_id"],
        "record_time_ms": run["record_time_ms"],
        "extractor_package_sha256": package["sha256"],
        "reviewed_coverage_policy": package.get("reviewed_coverage_policy"),
    }


def verify_actions(folder, expected, metadata_digest=None):
    path = folder / "metadata.json"
    if metadata_digest is not None:
        verify_file(path, metadata_digest)
    metadata = read_json(path)
    for key, value in {
        "status": "extracted",
        "source_sha256": expected["ghost_sha256"],
        "map_uid": expected["map_uid"],
        "finish_time_ms": expected["record_time_ms"],
        "account_id": expected["account_id"],
    }.items():
        if metadata.get(key) != value:
            raise ValueError(f"Extracted metadata mismatch for {key}: {folder}")
    for key in [
        "strict_bitstream",
        "gbx_semantics_match_strict_rows",
        "single_store",
        "race_coverage",
        "normalized_steering_range",
        "expected_map_uid_checked",
        "expected_finish_time_checked",
        "expected_account_id_checked",
    ]:
        if metadata.get("checks", {}).get(key) is not True:
            raise ValueError(f"Missing extractor check {key}: {folder}")
    files = metadata.get("files", {})
    if not REQUIRED_FILES <= set(files):
        raise ValueError("Missing required extraction files")
    for name, info in files.items():
        if Path(name).name != name or name in {".", "..", "metadata.json"}:
            raise ValueError("Unsafe extraction output filename")
        verify_file(folder / name, info["sha256"], info["bytes"])
    finish = expected["record_time_ms"]
    with (folder / "actions_race_100hz.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != (finish + 9) // 10 or metadata.get("race_ticks") != len(rows):
        raise ValueError("Action row count does not cover the exact finish interval")
    for i, row in enumerate(rows):
        if int(row["timestamp_ms"]) != i * 10 or int(row["valid_until_ms"]) != min(i * 10 + 10, finish):
            raise ValueError("Action interval grid is invalid")
        if not -1 <= float(row["steer_normalized"]) <= 1 or any(
            row[k] not in {"0", "1"} for k in ["throttle", "brake", "respawn", "secondary_respawn"]
        ):
            raise ValueError("Action values outside supported range")
    return metadata


def gate_file(reference, base):
    if not isinstance(reference, dict):
        raise ValueError("Pilot evidence needs a {path,sha256} reference")
    path = source_path(reference["path"], base)
    verify_file(path, reference["sha256"])
    return path


def verify_batch(folder, expected):
    batch = read_json(folder / "batch-result.json")
    if batch.get("status") != "actions_verified" or batch.get("source_key") != json_hash(expected):
        raise ValueError("Completed run source/parser changed or verification is missing")
    file_hashes = batch.get("output_files", {})
    actual_files = {p.name for p in folder.iterdir() if p.is_file() and p.name != "batch-result.json"}
    if set(file_hashes) != actual_files:
        raise ValueError("Completed output file inventory changed")
    for name, info in file_hashes.items():
        if Path(name).name != name:
            raise ValueError("Unsafe batch output filename")
        verify_file(folder / name, info["sha256"], info["bytes"])
    metadata = verify_actions(folder, expected, batch["metadata_sha256"])
    return batch, metadata


def validate_gate(path, manifest, output, package, render_output):
    """Validate the shared render gate plus hashed evidence and current pilot actions.

    The attestation is supplied by the person/agent who actually measured the
    pilot. This function checks its evidence integrity, never invents a pass.
    """
    gate = read_json(path)
    if gate.get("passed") is not True or gate.get("map_sha256") != manifest["map"]["sha256"]:
        raise ValueError("Pilot gate did not pass or refers to a different map")
    if not gate.get("verified_by") or not gate.get("verified_utc"):
        raise ValueError("Pilot gate must identify who measured/verified it and when")
    for name in required_gate_checks(gate.get("renderer_backend", "openplanet")):
        check = gate.get("checks", {}).get(name, {})
        if check.get("passed") is not True or not check.get("evidence"):
            raise ValueError("Pilot gate is missing check evidence: " + name)
    pilots = gate.get("pilot_runs", [])
    if len(pilots) != 5 or {p.get("ordinal") for p in pilots} != set(range(1, 6)):
        raise ValueError("Pilot gate requires all five leaderboard positions")
    if not gate.get("evidence_files"):
        raise ValueError("Pilot gate needs hash-pinned evidence_files")
    for reference in gate["evidence_files"]:
        gate_file(reference, path.parent)
    for pilot in pilots:
        run = manifest["runs"][pilot["ordinal"] - 1]
        if pilot.get("ghost_sha256") != run["ghost_sha256"]:
            raise ValueError("Top-five ghost snapshot changed; repeat the pilot")
        folder = output / "runs" / run["run_id"]
        expected = identity(manifest, run, package)
        batch, _ = verify_batch(folder, expected)
        action_metadata = gate_file(pilot.get("action_metadata"), path.parent)
        if (
            action_metadata != (folder / "metadata.json").resolve()
            or sha(action_metadata) != batch["metadata_sha256"]
        ):
            raise ValueError("Pilot gate refers to different action metadata")
        # Required actual rendered artifacts keep a boolean-only gate from scaling a batch.
        result_path = gate_file(pilot.get("render_result"), path.parent)
        video = gate_file(pilot.get("video"), path.parent)
        expected_render = render_output / run["run_id"]
        if (
            result_path != (expected_render / "result.json").resolve()
            or video != (expected_render / "video.webm").resolve()
        ):
            raise ValueError("Pilot evidence is outside the specified render output/run directory")
        result = read_json(result_path)
        if result.get("status") != "native_video_verified" or result.get("video", {}).get("sha256") != sha(
            video
        ):
            raise ValueError("Pilot video/result is missing verification or its hash changed")
        rendered_run = result.get("run", {})
        if any(
            rendered_run.get(k) != run[k] for k in ["run_id", "ghost_sha256", "account_id", "record_time_ms"]
        ):
            raise ValueError("Pilot render belongs to a different ghost")
        if result.get("map", {}).get("sha256") != manifest["map"]["sha256"]:
            raise ValueError("Pilot render used a different map")
        if gate.get("settings") != result.get("settings") or gate.get("plugin_build") != result.get(
            "plugin_build"
        ):
            raise ValueError("Pilot gate/render settings or build mismatch")
    # Share backend/settings/native scene validation with rendering. This policy
    # change does not enter action source identities or alter the shipped parser.
    renderer_path = Path(__file__).resolve().parent.parent / "renderer" / "render_queue.py"
    spec = importlib.util.spec_from_file_location("action_gate_renderer", renderer_path)
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    renderer.validate_gate(path, manifest, render_output)
    return {
        "path": str(path),
        "sha256": sha(path),
        "verified_by": gate["verified_by"],
        "verified_utc": gate["verified_utc"],
    }


def run_one(manifest, run, output, command, package):
    expected = identity(manifest, run, package)
    key = json_hash(expected)
    folder = output / "runs" / run["run_id"]
    verify_file(Path(manifest["map"]["path"]), expected["map_sha256"])
    verify_file(Path(run["ghost_path"]), expected["ghost_sha256"], run.get("ghost_bytes"))
    result_path = folder / "batch-result.json"
    if result_path.exists():
        previous = read_json(result_path)
        if previous.get("status") == "actions_verified":
            previous, metadata = verify_batch(folder, expected)
            return row_result(run, folder, metadata, previous["metadata_sha256"], "reused_verified")
    if folder.exists():
        # Preserve interrupted/rejected attempts, then let the immutable CLI create its new directory.
        attempts = output / "attempts"
        attempts.mkdir(exist_ok=True)
        archived = attempts / (run["run_id"] + "-" + uuid.uuid4().hex)
        folder.rename(archived)
    folder.parent.mkdir(exist_ok=True)
    cmd = command + [
        run["ghost_path"],
        "--output",
        str(folder),
        "--expected-map-uid",
        expected["map_uid"],
        "--expected-time-ms",
        str(run["record_time_ms"]),
        "--expected-account-id",
        run["account_id"],
    ]
    result = subprocess.run(cmd, text=True, capture_output=True, stdin=subprocess.DEVNULL)
    folder.mkdir(exist_ok=True)
    (folder / "extractor.stdout.log").write_text(result.stdout)
    (folder / "extractor.stderr.log").write_text(result.stderr)
    batch = {
        "source_key": key,
        "identity": expected,
        "collector_manifest_sha256": manifest["source_sha256"],
        "finished_utc": utc(),
    }
    try:
        if result.returncode != 0:
            raise ValueError(
                f"Extractor rejected {run['run_id']} (exit {result.returncode}): {result.stderr.strip()}"
            )
        verify_file(Path(run["ghost_path"]), expected["ghost_sha256"])
        verify_file(Path(manifest["map"]["path"]), expected["map_sha256"])
        metadata = verify_actions(folder, expected)
        metadata_digest = sha(folder / "metadata.json")
        batch.update(
            status="actions_verified",
            metadata_sha256=metadata_digest,
            output_files={
                p.name: {"sha256": sha(p), "bytes": p.stat().st_size}
                for p in sorted(folder.iterdir())
                if p.is_file() and p.name != "batch-result.json"
            },
        )
        write_json(result_path, batch)
        return row_result(run, folder, metadata, metadata_digest, "actions_verified")
    except Exception as exc:
        batch.update(status="failed", error=str(exc))
        write_json(result_path, batch)
        raise


def row_result(run, folder, metadata, metadata_digest, status):
    return {
        **run,
        "status": status,
        "actions": {
            "directory": str(folder),
            "metadata_path": str(folder / "metadata.json"),
            "metadata_sha256": metadata_digest,
            "race_csv_path": str(folder / "actions_race_100hz.csv"),
            "race_csv_sha256": metadata["files"]["actions_race_100hz.csv"]["sha256"],
            "raw_events_path": str(folder / "input_events.json"),
            "visual_samples_path": str(folder / "visual_samples.csv"),
            "race_rows": metadata["race_ticks"],
            "finish_time_ms": metadata["finish_time_ms"],
            "verified": True,
        },
        "video_alignment_verified": False,
    }


def main(run_handler=None):
    run_handler = run_handler or run_one
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--extractor", type=Path, required=True, help="Published x64 executable or portable DLL"
    )
    parser.add_argument("--dotnet", type=Path, help="Runtime executable, only for a portable DLL")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--gate", type=Path, help="Required for more than five; evidence-backed shared pilot gate"
    )
    parser.add_argument(
        "--render-output", type=Path, help="Required with --gate; root of the verified native render jobs"
    )
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    manifest = load_manifest(args.manifest.resolve(strict=True), args.limit)
    command, package = extractor_package(args.extractor, args.dotnet)
    output = args.output.resolve()
    if output == Path(package["directory"]) or Path(package["directory"]) in output.parents:
        raise ValueError("Dataset output must be outside the immutable extractor package directory")
    if args.preflight:
        print(
            json.dumps(
                {
                    "status": "preflight_passed",
                    "runs": args.limit,
                    "map": manifest["map"],
                    "extractor_package_sha256": package["sha256"],
                    "execution_requires_gate": args.limit > 5,
                }
            )
        )
        return 0
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".batch-extraction.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        gate = None
        if args.limit > 5:
            if not args.gate or not args.render_output:
                raise ValueError(
                    "More than five runs requires --gate and --render-output after actual pilot verification"
                )
            gate = validate_gate(
                args.gate.resolve(strict=True),
                manifest,
                output,
                package,
                args.render_output.resolve(strict=True),
            )
        record = {
            "schema_version": 1,
            "status": "extracting",
            "requested_count": args.limit,
            "cohort": manifest.get("cohort"),
            "source_manifest": manifest["source_path"],
            "source_manifest_sha256": manifest["source_sha256"],
            "map": manifest["map"],
            "extractor_package": package,
            "pilot_gate": gate,
            "started_utc": utc(),
            "runs": [],
            "failures": [],
            "source_collection_failures": manifest["failures"],
            "map_file_uid_verified": False,
            "video_alignment_verified": False,
        }
        dataset_path = output / "action_dataset_manifest.json"
        write_json(dataset_path, record)
        for run in manifest["runs"]:
            try:
                row = run_handler(manifest, run, output, command, package)
                record["runs"].append(row)
                write_json(dataset_path, record)
                print(
                    json.dumps({"run_id": run["run_id"], "ordinal": run["ordinal"], "status": row["status"]}),
                    flush=True,
                )
            except Exception as exc:
                record["status"] = "incomplete"
                record["failures"].append(
                    {"run_id": run["run_id"], "ordinal": run["ordinal"], "error": str(exc)}
                )
                record["stopped_utc"] = utc()
                write_json(dataset_path, record)
                raise
        record["status"] = "actions_verified"
        record["finished_utc"] = utc()
        write_json(dataset_path, record)
        print(
            json.dumps(
                {
                    "status": "actions_verified",
                    "runs": len(record["runs"]),
                    "manifest": str(dataset_path),
                    "video_alignment_verified": False,
                }
            )
        )
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}), file=sys.stderr)
        raise SystemExit(2)
