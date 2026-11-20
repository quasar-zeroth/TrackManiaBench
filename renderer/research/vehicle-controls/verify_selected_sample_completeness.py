#!/usr/bin/env python3
"""Check every selected source sample is present, using frozen extractor CSVs."""

import argparse
import csv
import json
from pathlib import Path
import analyze_selected_controls as a


def verify(raw, request):
    directory = Path(request["actions"]["directory"])
    metadata_ref = a.pin(directory / "metadata.json", request["actions"]["metadata_sha256"])
    metadata = json.loads(Path(metadata_ref["path"]).read_text())
    visual = a.pin(directory / "visual_samples.csv", metadata["files"]["visual_samples.csv"]["sha256"])
    selected = set(raw["selected_entity_indices"])
    expected = [
        (int(r["entity_index"]), int(r["sample_index"]), int(r["raw_timestamp_ms"]))
        for r in csv.DictReader(Path(visual["path"]).open())
        if r["source"] == "record_data" and int(r["entity_index"]) in selected
    ]
    actual = [
        (e["entity_index"], s["sample_index"], s["record_time_ms"])
        for e in raw["entities"]
        for s in e["samples"]
    ]
    a.require(
        expected and actual == expected,
        "Selected source samples were omitted, duplicated, reordered or shifted",
    )
    return dict(
        ordinal=raw["ordinal"],
        run_id=request["run_id"],
        action_metadata=metadata_ref,
        visual_samples=visual,
        selected_entity_indices=raw["selected_entity_indices"],
        samples=len(actual),
        exact_all_selected_samples_present=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    index_path = args.index
    index = json.loads(index_path.read_text())
    results = []
    for row in index["runs"]:
        evidence = json.loads(
            Path(a.pin(row["evidence"]["path"], row["evidence"]["sha256"])["path"]).read_text()
        )
        raw = json.loads(
            Path(
                a.pin(evidence["raw_controls"]["path"], evidence["raw_controls"]["sha256"])["path"]
            ).read_text()
        )
        request = json.loads(
            Path(a.pin(evidence["request"]["path"], evidence["request"]["sha256"])["path"]).read_text()
        )
        results.append(verify(raw, request))
    output = args.output
    a.require(not output.exists(), "Output exists")
    report = dict(
        schema_version=1,
        status="all_selected_control_samples_present",
        evidence_index=a.pin(index_path),
        verifier=a.pin(__file__),
        runs=results,
        records_checked=len(results),
        selected_samples_checked=sum(r["samples"] for r in results),
        source_clock_approved=False,
        physical_capture_phase_verified=False,
    )
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            dict(
                records_checked=len(results),
                selected_samples_checked=report["selected_samples_checked"],
                report=a.pin(output),
            )
        )
    )


if __name__ == "__main__":
    main()
