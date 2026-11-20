#!/usr/bin/env python3
"""Compare exact control-reflection fits with independently typed checkpoint times.

No automatic production clock approval. The transfer from control capture phase
to physical state exposure remains an explicitly reported empirical inference.
"""

import argparse
import collections
import csv
import json
from pathlib import Path
import sys
import analyze_selected_controls as a

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "dataset"))
import importlib.util
import verify_source_clocks_v2 as clocks


def intervalize(offsets):
    result = []
    for offset in offsets:
        if result and result[-1][1] == offset:
            result[-1][1] = offset + 10
        else:
            result.append([offset, offset + 10])
    return result


def phase_offsets(record, request, radius=1000):
    path = Path(request["actions"]["directory"]) / "actions_all_100hz.csv"
    rows = list(csv.DictReader(path.open()))
    first = int(rows[0]["timestamp_ms"])
    values = [
        (((int(r["steer_raw"]) + 127) * 255) // 254, int(r["throttle"]) * 255, int(r["brake"]) * 255)
        for r in rows
    ]
    samples = [
        (s["record_time_ms"], (s["steer_byte"], s["gas_difference_byte"] + s["brake_byte"], s["brake_byte"]))
        for e in record["entities"]
        for s in e["samples"]
    ]
    finish = record["finish_time_ms"]
    result = []
    for phase, lo, hi in [
        ("start", 0, finish / 3),
        ("middle", finish / 3, 2 * finish / 3),
        ("finish", 2 * finish / 3, finish),
    ]:
        chosen = [
            (t, value)
            for t, value in samples
            if max(max(0, first) + radius, lo) <= t < min(finish - radius, hi)
        ]
        offsets = []
        for off in range(-radius, radius + 1, 10):
            if chosen and all(value == values[(t + off - first) // 10] for t, value in chosen):
                offsets.append(off)
        result.append(
            dict(
                phase=phase,
                common_support_samples=len(chosen),
                viable_offset_intervals_ms=intervalize(offsets),
                upper_bounds_exclusive=True,
            )
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("correlation", "requests", "bindings", "samples", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--primary-source", type=Path, action="append", required=True)
    args = parser.parse_args()
    correlation_path = args.correlation
    correlation = json.loads(correlation_path.read_text())
    request_path = args.requests
    requests = json.loads(request_path.read_text())
    byordinal = {r["ordinal"]: r for r in requests["files"]}
    binding_path = args.bindings
    bindings = {
        r["source_sha256"]: r for r in (json.loads(l) for l in binding_path.open()) if r["role"] == "original"
    }
    sample_path = args.samples
    sample_rows = {r["ordinal"]: r for r in (json.loads(l) for l in sample_path.open())}
    result = []
    family = collections.defaultdict(list)
    for fit in correlation["records"]:
        ordinal = fit["ordinal"]
        request = byordinal[ordinal]
        binding = bindings[request["sha256"]]
        meta = json.loads(Path(fit["action_metadata"]["path"]).read_text())
        raw = {k: binding[k] for k in ("source_sha256", "map_uid", "ghost_login", "finish_time_ms")}
        raw["checkpoints"] = [dict(race_time_ms=cp["time_ms"]) for cp in binding["checkpoints"]]
        raw["entities"] = [
            dict(
                entity_index=e["entity_index"], entity_class_id=e["entity_class_id"], scene_uid=e["SceneUId"]
            )
            for e in binding["entities"]
        ]
        raw["events"] = [
            {**event, "little_endian_int32_words_for_investigation": event["raw_int32_words"]}
            for event in binding["notices"]
        ]
        try:
            checkpoint = clocks.verify(raw, meta)
            verified = True
        except ValueError as error:
            checkpoint = dict(status="unverified", reason=str(error))
            verified = False
        full = fit["status"] == "exact_control_reflection_correlated"
        phases = phase_offsets(sample_rows[ordinal], request) if full else []
        archives = tuple(e["descriptor"]["StateArchiveVersion"] for e in fit["selected_entities"])
        item = dict(
            ordinal=ordinal,
            run_id=fit["run_id"],
            game_build=fit["game_build"],
            state_archive_versions=archives,
            original_ghost=fit["original_ghost"],
            action_metadata=fit["action_metadata"],
            source_actions=fit["source_actions"],
            exact_all_controls=full,
            viable_control_reflection_offset_intervals_ms=fit[
                "viable_control_reflection_offset_intervals_ms"
            ],
            phase_offset_search=phases,
            exact_checkpoint_series_verified=verified,
            checkpoint_series=checkpoint,
            full_physical_state_clock_guarantee=False,
        )
        result.append(item)
        if verified and full:
            family[
                (archives, tuple(tuple(x) for x in fit["viable_control_reflection_offset_intervals_ms"]))
            ].append(item)
    families = []
    for (archives, offsets), rows in sorted(family.items()):
        differences = [
            cp["record_minus_race_ms"] for row in rows for cp in row["checkpoint_series"]["checkpoints"]
        ]
        families.append(
            dict(
                state_archive_versions=archives,
                control_reflection_intervals_ms=offsets,
                source_count=len(rows),
                ordinals=[r["ordinal"] for r in rows],
                typed_checkpoint_count=len(differences),
                record_minus_exact_race_ms_min=min(differences),
                record_minus_exact_race_ms_max=max(differences),
                game_builds=sorted(set(r["game_build"] for r in rows)),
                inference="Within this sample format, exact input reflection and independently typed checkpoint clocks coexist at nominal identity within10ms; this does not distinguish capture order from exact physical-state phase.",
            )
        )
    pending = [r for r in result if not r["exact_checkpoint_series_verified"]]
    source_files = args.primary_source
    report = dict(
        schema_version=1,
        status="selected_control_checkpoint_crosscheck_diagnostic",
        correlation=a.pin(correlation_path),
        raw_notices=a.pin(binding_path),
        requests=a.pin(request_path),
        raw_samples=a.pin(sample_path),
        verifier=a.pin(clocks.__file__),
        crosschecker=a.pin(__file__),
        primary_sources=[a.pin(p) for p in source_files],
        records_checked=len(result),
        exact_checkpoint_series=sum(r["exact_checkpoint_series_verified"] for r in result),
        exact_controls=sum(r["exact_all_controls"] for r in result),
        independently_crosschecked_sources=sum(
            r["exact_all_controls"] and r["exact_checkpoint_series_verified"] for r in result
        ),
        pending_checkpoint_sources=len(pending),
        pending_sources_with_exact_controls=sum(r["exact_all_controls"] for r in pending),
        pending_ordinals=[r["ordinal"] for r in pending],
        families=families,
        records=result,
        source_clock_approved=False,
        video_alignment_verified=False,
        limitations=[
            "Offset intervals concern applied/reflected controls, not an exact physical-state exposure timestamp.",
            "Primary GBX.NET code reads the timestamp and raw state buffer but does not document engine capture order or latency.",
            "Family evidence may share a state archive format across different game builds; this is not a same-build capture-order proof.",
            "Offset search is bounded to±1000ms and uses common-support samples; all viable candidates then require every available known-race sample to match.",
            "Sources failing three-channel identity remain explicit failures; no channels are waived.",
        ],
    )
    output = args.output
    a.require(not output.exists(), "Output already exists")
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "records_checked",
                    "exact_checkpoint_series",
                    "exact_controls",
                    "independently_crosschecked_sources",
                    "pending_checkpoint_sources",
                    "pending_sources_with_exact_controls",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
