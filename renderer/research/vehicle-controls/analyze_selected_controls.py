#!/usr/bin/env python3
"""Exact selected-vehicle/control correlation; never approves state-clock alignment.

All offsets compare reflected controls with original held input rows. A lag can
arise from engine capture order; it is not an absolute physical-state clock.
"""

import argparse
import base64
import bisect
import collections
import csv
import hashlib
import json
from pathlib import Path


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for part in iter(lambda: f.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def pin(path, expected=None):
    path = Path(path).resolve(strict=True)
    actual = sha(path)
    require(expected is None or actual == expected, "Changed pinned source: " + str(path))
    return dict(path=str(path), sha256=actual, bytes=path.stat().st_size)


def inspect_run(record, request, radius):
    require(record["status"] == "selected_vehicle_controls_diagnostic_only", "Vehicle probe failed")
    require(record["source_sha256"] == request["sha256"], "Probe source differs")
    original = pin(request["path"], request["sha256"])
    metadata_ref = pin(
        Path(request["actions"]["directory"]) / "metadata.json", request["actions"]["metadata_sha256"]
    )
    metadata = json.loads(Path(metadata_ref["path"]).read_text())
    require(
        metadata["source_sha256"] == original["sha256"]
        and metadata["finish_time_ms"] == record["finish_time_ms"],
        "Action/source finish or identity differs",
    )
    action_ref = pin(
        Path(request["actions"]["directory"]) / "actions_all_100hz.csv",
        metadata["files"]["actions_all_100hz.csv"]["sha256"],
    )
    actions = list(csv.DictReader(Path(action_ref["path"]).open()))
    times = [int(a["timestamp_ms"]) for a in actions]
    require(
        all(int(a["valid_until_ms"]) == t + 10 for a, t in zip(actions, times))
        and all(b - a == 10 for a, b in zip(times, times[1:])),
        "Controls are not contiguous10ms intervals",
    )
    values = [
        (((int(a["steer_raw"]) + 127) * 255) // 254, int(a["throttle"]) * 255, int(a["brake"]) * 255)
        for a in actions
    ]
    require(all(-127 <= int(a["steer_raw"]) <= 127 for a in actions), "Invalid input steering")
    require(
        record["selected_entity_indices"] == [e["entity_index"] for e in record["entities"]],
        "Selected source vehicle entities differ",
    )
    samples = []
    for entity in record["entities"]:
        require(entity["class_id"] == "0x0A018000", "Non-vehicle selection")
        for sample in entity["samples"]:
            raw = base64.b64decode(sample["raw_sample_base64"], validate=True)
            require(
                len(raw) == sample["raw_sample_bytes"]
                and hashlib.sha256(raw).hexdigest() == sample["raw_sample_sha256"],
                "Raw sample hash or extent changed",
            )
            require(
                (raw[14], raw[15], raw[18])
                == (sample["steer_byte"], sample["gas_difference_byte"], sample["brake_byte"]),
                "Named control bytes differ from actual source sample",
            )
            t = sample["record_time_ms"]
            require(type(t) is int and t % 10 == 0, "Unsupported sample clock phase")
            samples.append(
                (t, (raw[14], raw[15] + raw[18], raw[18]), entity["entity_index"], sample["sample_index"])
            )
    samples.sort()
    known_start = max(0, times[0])
    finish = record["finish_time_ms"]
    require(len(samples) > 30, "Insufficient selected samples")
    interior = [s for s in samples if known_start + radius <= s[0] < finish - radius]
    require(len(interior) > 30, "Insufficient common-support samples for offset search")

    def mismatch(shift, chosen):
        bad = [0, 0, 0]
        compared = 0
        ignored = 0
        details = []
        for t, value, entity, index in chosen:
            race = t + shift
            if race < known_start or race >= finish:
                ignored += 1
                continue
            ix = bisect.bisect_right(times, race) - 1
            require(ix >= 0 and ix < len(actions) and race < times[ix] + 10, "Input extent missing")
            diff = [int(a != b) for a, b in zip(value, values[ix])]
            bad = [a + b for a, b in zip(bad, diff)]
            compared += 1
            if any(diff) and len(details) < 5:
                details.append(
                    dict(
                        record_time_ms=t,
                        compared_input_time_ms=race,
                        entity_index=entity,
                        sample_index=index,
                        sample_control_bytes=value,
                        input_quantized_bytes=values[ix],
                    )
                )
        return dict(
            offset_ms=shift,
            compared_samples=compared,
            ignored_outside_known_race=ignored,
            steer_mismatches=bad[0],
            gas_mismatches=bad[1],
            brake_mismatches=bad[2],
            first_mismatches=details,
        )

    candidates = []
    all_scores = []
    for offset in range(-radius, radius + 1, 10):
        row = mismatch(offset, interior)
        all_scores.append(row)
        if row["steer_mismatches"] == row["gas_mismatches"] == row["brake_mismatches"] == 0:
            candidates.append(offset)
    checks = [mismatch(offset, samples) for offset in candidates]
    valid = [
        row["offset_ms"]
        for row in checks
        if row["steer_mismatches"] == row["gas_mismatches"] == row["brake_mismatches"] == 0
    ]
    intervals = []
    for offset in valid:
        if intervals and intervals[-1][1] == offset:
            intervals[-1][1] = offset + 10
        else:
            intervals.append([offset, offset + 10])
    # Each state's changes use the exact same integer10ms phase. Hence every
    # real offset in [k,k+10) selects the same held row; upper bounds are open.
    nearest = sorted(
        all_scores,
        key=lambda x: (
            x["steer_mismatches"] + x["gas_mismatches"] + x["brake_mismatches"],
            abs(x["offset_ms"]),
        ),
    )[:5]
    phase_checks = []
    for offset in valid:
        for label, lo, hi in [
            ("start", known_start, finish / 3),
            ("middle", finish / 3, 2 * finish / 3),
            ("finish", 2 * finish / 3, finish),
        ]:
            chosen = [s for s in samples if lo <= s[0] + offset < hi]
            phase_checks.append(dict(phase=label, **mismatch(offset, chosen)))
    return dict(
        ordinal=record["ordinal"],
        run_id=request["run_id"],
        status="exact_control_reflection_correlated" if valid else "control_reflection_unresolved",
        original_ghost=original,
        action_metadata=metadata_ref,
        source_actions=action_ref,
        game_build=record["game_build"],
        finish_time_ms=finish,
        known_action_interval_ms=[known_start, finish],
        record_interval_ms=[record["record_start_ms"], record["record_end_ms"]],
        selected_entity_indices=record["selected_entity_indices"],
        selected_entities=[
            dict(
                entity_index=e["entity_index"],
                scene_uid=e["SceneUId"],
                descriptor=e["descriptor_properties"],
                begin_ms=e["begin_ms"],
                end_ms=e["end_ms"],
                samples=len(e["samples"]),
            )
            for e in record["entities"]
        ],
        total_selected_samples=len(samples),
        common_support_samples=len(interior),
        offset_search=dict(
            min_ms=-radius,
            max_ms=radius,
            grid_ms=10,
            common_support_only=True,
            meaning="input_race_timestamp = selected_vehicle_sample_record_timestamp + control_reflection_offset",
        ),
        exact_integer_quantizer="steer_byte = ((steer_raw + 127) * 255) // 254; gas_byte_sum = throttle * 255; brake_byte = brake * 255",
        viable_control_reflection_offset_intervals_ms=intervals,
        offset_interval_upper_bounds_exclusive=True,
        full_extent_candidate_checks=checks,
        three_phase_checks=phase_checks,
        nearest_common_support_offsets=nearest,
        physical_state_clock_alignment_verified=False,
        capture_order_or_latched_input_delay_disambiguated=False,
        nominal_record_race_relation_approved=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requests", type=Path)
    parser.add_argument("samples", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--radius-ms", type=int, default=1000)
    a = parser.parse_args()
    require(a.radius_ms > 0 and a.radius_ms % 10 == 0, "Search radius must be a positive10ms multiple")
    requests = json.loads(a.requests.read_text())
    by_ordinal = {r["ordinal"]: r for r in requests["files"]}
    require(len(by_ordinal) == len(requests["files"]), "Duplicate requested ordinal")
    results = []
    with a.samples.open() as f:
        for line in f:
            record = json.loads(line)
            results.append(inspect_run(record, by_ordinal[record["ordinal"]], a.radius_ms))
    require(
        len(results) == len(by_ordinal) and {r["ordinal"] for r in results} == set(by_ordinal),
        "Missing/duplicate sample records",
    )
    patterns = collections.Counter(
        (
            tuple(e["descriptor"]["StateArchiveVersion"] for e in r["selected_entities"]),
            tuple(tuple(x) for x in r["viable_control_reflection_offset_intervals_ms"]),
        )
        for r in results
    )
    output = dict(
        schema_version=1,
        status="selected_vehicle_control_correlation_diagnostic",
        requests=pin(a.requests),
        raw_selected_samples=pin(a.samples),
        analyzer=pin(__file__),
        records=results,
        records_checked=len(results),
        exact_matches=sum(r["status"] == "exact_control_reflection_correlated" for r in results),
        archive_offset_patterns=[
            dict(state_archive_versions=k[0], offset_intervals_ms=k[1], runs=v)
            for k, v in sorted(patterns.items())
        ],
        interpretation="Exact controls corroborate the relationship of the selected vehicle and original input timeline. Control reflection timing may differ from physical state exposure by engine capture order; no absolute state-clock phase or video alignment is established here.",
        source_clock_approved=False,
        video_alignment_verified=False,
        production_files_modified=False,
    )
    require(not a.output.exists(), "Diagnostic output already exists")
    a.output.write_text(json.dumps(output, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: output[k]
                for k in (
                    "records_checked",
                    "exact_matches",
                    "archive_offset_patterns",
                    "source_clock_approved",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
