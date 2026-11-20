"""Write signed GhostToReplay input from verified frozen-source identities."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from collector.common import inspect_gbx, safe_uid, sha256, valid_id


def write_batch(manifest_path, output, limit=None):
    manifest = json.loads(Path(manifest_path).read_text())
    uid = safe_uid(manifest["map"]["uid"])
    if sha256(manifest["map"]["path"]) != manifest["map"]["sha256"]:
        raise ValueError("Map changed after collection")
    if manifest.get("failures"):
        raise ValueError("Collection contains unresolved failures")
    runs = sorted(manifest["runs"], key=lambda row: row["ordinal"])
    count = len(runs) if limit is None else limit
    if not 1 <= count <= len(runs) or [r["ordinal"] for r in runs] != list(range(1, len(runs) + 1)):
        raise ValueError("Invalid batch limit or noncontiguous cohort")
    if len({r["run_id"] for r in runs}) != len(runs) or len({r["account_id"] for r in runs}) != len(runs):
        raise ValueError("Duplicate record or player")
    lines = []
    for run in runs[:count]:
        record_id = valid_id(run["run_id"])
        if sha256(run["ghost_path"]) != run["ghost_sha256"]:
            raise ValueError("Ghost changed after collection")
        inspect_gbx(run["ghost_path"])
        lines.append(f"{uid}; {record_id}\n")
    content = "".join(lines)
    output = Path(output)
    if output.exists() and output.read_text() != content:
        raise ValueError("Refusing to replace a different replay-conversion batch")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content)
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    count = write_batch(args.manifest, args.output, args.limit)
    print(json.dumps({"batch": str(args.output), "records": count}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
