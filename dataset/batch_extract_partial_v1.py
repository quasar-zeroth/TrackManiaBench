"""Versioned action admission for exact measured 10ms prefix gaps.

The frozen base handles unchanged parsing, identities, hashing and pilot gates.
Only observed-coverage admission and its callers are local; no monkeypatching.
"""

import csv
from pathlib import Path
import subprocess
import uuid
import importlib.util
import sys

_spec = importlib.util.spec_from_file_location(
    "trackmaniabench_action_batch", Path(__file__).resolve().parents[1] / "extractor/batch_extract.py"
)
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)
import coverage_policy_v1 as coverage

REQUIRED_FILES = base.REQUIRED_FILES
sha = base.sha
json_hash = base.json_hash
read_json = base.read_json
write_json = base.write_json
utc = base.utc
verify_file = base.verify_file
identity = base.identity
load_manifest = base.load_manifest
extractor_package = base.extractor_package
validate_gate = base.validate_gate


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
    observed = coverage.validate_metadata(metadata)
    start = observed["known_race_interval_ms"][0]
    finish = expected["record_time_ms"]
    with (folder / "actions_race_100hz.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != (finish - start + 9) // 10 or metadata.get("race_ticks") != len(rows):
        raise ValueError("Action row count does not cover the exact finish interval")
    for i, row in enumerate(rows):
        if int(row["timestamp_ms"]) != start + i * 10 or int(row["valid_until_ms"]) != min(
            start + i * 10 + 10, finish
        ):
            raise ValueError("Action interval grid is invalid")
        if not -1 <= float(row["steer_normalized"]) <= 1 or any(
            row[k] not in {"0", "1"} for k in ["throttle", "brake", "respawn", "secondary_respawn"]
        ):
            raise ValueError("Action values outside supported range")
    return metadata


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


def run_one(manifest, run, output, command, package):
    expected = identity(manifest, run, package)
    if expected.get("reviewed_coverage_policy") != coverage.policy_reference():
        raise ValueError("Reviewed coverage policy changed after package verification")
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
    result = base.row_result(run, folder, metadata, metadata_digest, status)
    if metadata.get("action_coverage"):
        result["actions"]["action_coverage"] = coverage.validate_metadata(metadata, run)
    return result


def main():
    return base.main(run_handler=run_one)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
