#!/usr/bin/env python3
"""Inspect new immutable audit edges, reusing an explicit prior closure attestation.

Baseline JSON bytes are rehashed to reconstruct its exact reference graph;
baseline binary leaves rely on the supplied prior attestation. Every new leaf
and every historical alias used for a new edge is hashed now. This is a closure
preflight, not a new verification of all baseline binary bytes or game output.
"""

import argparse
from collections import deque
import hashlib
import json
import os
from pathlib import Path


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def pin(path):
    p = Path(path).resolve(strict=True)
    return dict(path=str(p), sha256=sha(p), bytes=p.stat().st_size)


def refs(obj):
    if isinstance(obj, dict):
        if isinstance(obj.get("path"), str) and isinstance(obj.get("sha256"), str):
            yield obj
        for value in obj.values():
            yield from refs(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from refs(value)


def normalized(ref, base):
    p = Path(ref["path"])
    return (str(Path(os.path.normpath(str(p if p.is_absolute() else base / p)))), ref["sha256"])


def inspect(baseline_path, root_paths):
    baseline_path = Path(baseline_path).resolve(strict=True)
    baseline = json.loads(baseline_path.read_text())
    if baseline.get("status") != "source_evidence_closure_verified" or baseline.get("unresolved"):
        raise ValueError("Prior baseline closure did not pass")
    cache = {}
    locations = {}
    aliases = []
    baseline_keys = set()
    checked = set()
    visited_json = set()
    baseline_json_checked = set()

    def current(path):
        p = Path(path)
        if path not in cache:
            cache[path] = sha(p) if p.is_file() else None
        return cache[path]

    def walk(initial, is_baseline):
        queue = deque(initial)
        seen = set()
        unresolved = []
        while queue:
            path, digest = queue.popleft()
            key = (path, digest)
            if key in seen:
                continue
            seen.add(key)
            if is_baseline:
                baseline_keys.add(key)
            elif key in baseline_keys:
                continue
            is_json = Path(path).suffix.lower() == ".json"
            if is_baseline and not is_json:
                locations.setdefault(digest, set()).add(path)
                continue
            actual = path if current(path) == digest else None
            if actual is None:
                actual = next((p for p in locations.get(digest, ()) if current(p) == digest), None)
            if actual is None:
                unresolved.append(key)
                continue
            locations.setdefault(digest, set()).add(actual)
            if actual != path:
                aliases.append(dict(original_path=path, sha256=digest, available_path=actual))
            checked.add((actual, digest))
            if is_baseline:
                baseline_json_checked.add((actual, digest))
            if is_json and key not in visited_json:
                visited_json.add(key)
                obj = json.loads(Path(actual).read_text())
                queue.extend(normalized(r, Path(path).parent) for r in refs(obj))
        # Provenance roots can reveal historical byte aliases after their use.
        remaining = []
        for path, digest in unresolved:
            actual = next((p for p in locations.get(digest, ()) if current(p) == digest), None)
            if actual is None:
                remaining.append(dict(path=path, sha256=digest, current_sha256=current(path)))
                continue
            aliases.append(dict(original_path=path, sha256=digest, available_path=actual))
            checked.add((actual, digest))
            if Path(path).suffix.lower() == ".json":
                # Historical JSON bases retain the original path for relatives.
                obj = json.loads(Path(actual).read_text())
                nested = walk([normalized(r, Path(path).parent) for r in refs(obj)], is_baseline)
                remaining.extend(nested)
        return remaining

    baseline_unresolved = walk([normalized(r, baseline_path.parent) for r in baseline["roots"]], True)
    roots = [pin(p) for p in root_paths]
    new_unresolved = walk([(r["path"], r["sha256"]) for r in roots], False)
    return dict(
        schema_version=1,
        status="incremental_audit_closure_verified"
        if not baseline_unresolved + new_unresolved
        else "incremental_audit_closure_unresolved",
        method="Rehash baseline JSON to reconstruct its graph; reuse prior attestation for baseline binary leaves; hash every new file and any alias used by new edges.",
        baseline=pin(baseline_path),
        baseline_reported_files=baseline["files_checked"],
        baseline_reference_keys=len(baseline_keys),
        baseline_json_rehashed=len(baseline_json_checked),
        all_baseline_binary_bytes_rehashed=False,
        roots=roots,
        newly_hashed_files=len(checked - baseline_json_checked),
        unresolved_baseline_json=baseline_unresolved,
        unresolved=new_unresolved,
        aliases=sorted({(x["original_path"], x["sha256"], x["available_path"]) for x in aliases}),
        new_checked_files=[dict(path=p, sha256=s) for p, s in sorted(checked - baseline_json_checked)],
        checker=pin(__file__),
        game_output_acceptance_asserted=False,
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", required=True)
    p.add_argument("--root", action="append", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    result = inspect(a.baseline, a.root)
    out = Path(a.output)
    if out.exists():
        raise ValueError("Report exists; use a new report path")
    out.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in ("aliases", "new_checked_files", "roots")}, indent=2
        )
    )
    return 0 if result["status"] == "incremental_audit_closure_verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
