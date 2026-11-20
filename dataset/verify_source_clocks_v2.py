#!/usr/bin/env python3
"""Validate direct checkpoint evidence or explicitly conditional controls.

Conditional reflected controls never establish a measured physical-clock bound.
Native render markers prove a separate video/clip relation.
"""

import argparse
import base64
from collections import defaultdict
import hashlib
from pathlib import Path
import struct
import sys
import json
import join_dataset as j

STATUS = "record_race_exact_checkpoint_series_corroborated_v2"
METHOD = "unique_arena21_exact_header_checkpoint_series_v2"


def payload_words(event):
    payload = base64.b64decode(event["raw_payload_base64"], validate=True)
    j.require(
        hashlib.sha256(payload).hexdigest() == event["raw_payload_sha256"]
        and len(payload) == event["raw_payload_bytes"],
        "Clock notice payload hash/size differs",
    )
    words = list(struct.unpack("<" + "i" * (len(payload) // 4), payload[: len(payload) // 4 * 4]))
    j.require(
        words == event["little_endian_int32_words_for_investigation"], "Decoded clock notice words differ"
    )
    return payload, words


def verify(raw, metadata):
    j.require(raw["source_sha256"] == metadata["source_sha256"], "Clock/source SHA differs")
    for key in ("map_uid", "ghost_login", "finish_time_ms"):
        j.require(raw[key] == metadata[key], "Clock identity differs: " + key)
    cps = [c["time_ms"] for c in metadata["checkpoints"]]
    j.require(
        len(cps) >= 3 and cps[-1] == metadata["finish_time_ms"] and cps == sorted(set(cps)),
        "Expected at least three ordered checkpoint times including exact finish",
    )
    j.require([c["race_time_ms"] for c in raw["checkpoints"]] == cps, "Original checkpoint tables differ")
    entities = {e["entity_index"]: e for e in raw.get("entities", [])}
    grouped = defaultdict(list)
    for event in raw["events"]:
        if event["notice_class_id"] != "0x032CB000" or event["notice_type"] != 21:
            continue
        payload, words = payload_words(event)
        # Retain unrelated entities/notices in the raw evidence. Do not use a
        # nearest-time or U02/vehicle-index heuristic to select arena ownership.
        if len(words) > 2 and words[1] == words[2] and words[1] in cps:
            grouped[event["entity_index"]].append((event, payload, words))
    matches = []
    for entity, events in grouped.items():
        if len(events) != len(cps) or [w[1] for _, _, w in events] != cps:
            continue
        measurements = []
        valid = True
        for index, (event, payload, words) in enumerate(events):
            version = event["notice_archive_version"]
            race = cps[index]
            if not (
                version in (3, 5)
                and len(payload) == (30 if version == 3 else 34)
                and words[6] == 0
                and (
                    words[0] == 0
                    or (
                        entity in entities
                        and entities[entity].get("entity_class_id") == "0x032CB000"
                        and words[0] == entities[entity].get("scene_uid")
                    )
                )
                and words[5] == (65536 if index == len(cps) - 1 else index + 1)
                and int.from_bytes(payload[-2:], "little") == (0 if version == 3 else index + 1)
            ):
                valid = False
                break
            record = event["record_timestamp_ms"]
            difference = record - race
            if type(record) is not int or record % 10 or abs(difference) > 10:
                valid = False
                break
            measurements.append(
                dict(
                    checkpoint_ordinal=index + 1,
                    finish=index == len(cps) - 1,
                    race_time_ms=race,
                    record_time_ms=record,
                    record_minus_race_ms=difference,
                    entity_index=entity,
                    arena_scene_uid=words[0],
                    notice_archive_version=version,
                    raw_payload_sha256=event["raw_payload_sha256"],
                )
            )
        if valid and all(
            a["record_time_ms"] < b["record_time_ms"] for a, b in zip(measurements, measurements[1:])
        ):
            matches.append(measurements)
    j.require(
        len(matches) == 1,
        "Source clock remains unverified: expected one complete exact checkpoint-time payload series within inclusive10ms; legacy/missing/ambiguous series require separate measured corroboration",
    )
    measurements = matches[0]
    diffs = [x["record_minus_race_ms"] for x in measurements]
    return dict(
        status=STATUS,
        method=METHOD,
        evidence_class="direct_checkpoint_notices",
        checkpoints=measurements,
        precision_bound_ms=10,
        conditional_precision_bound_ms=None,
        precision_bound_inclusive=True,
        maximum_observed_absolute_difference_ms=max(map(abs, diffs)),
        nominal_mapping=dict(record_anchor_ms=0, race_anchor_ms=0, scale=1),
        nominal_identity_clock_consistent=True,
        finish_corroborated=True,
        corroborated_checkpoint_ordinals=list(range(1, len(cps) + 1)),
        full_race_uniform_error_guarantee=False,
        exact_subtick_offset_verified=False,
        video_alignment_verified=False,
        interpretation="A unique arena entity contains the exact ordered header checkpoint values twice, typed ordinals and finish marker. Its record timestamps corroborate nominal record_ms=race_ms within inclusive10ms at every checkpoint event. This establishes a bounded source-clock relation; it does not assert exact frame exposure time, inferred arena ownership from vehicle selection, or a measured sub-tick offset.",
    )


def validate_proof(proof):
    j.require(
        proof.get("schema_version") == 2 and proof.get("verifier_sha256") == j.sha(__file__),
        "Source clock verifier/schema changed",
    )
    raw = j.evidence(proof["source_notices"], Path("."))
    meta = j.evidence(proof["action_metadata"], Path("."))
    if proof.get("evidence_class") == "conditional_control_reflection":
        import control_clock_corroboration_v1 as controls

        expected = controls.verify(j.read(raw["path"]), j.read(meta["path"]), proof["control_evidence"])
    else:
        expected = verify(j.read(raw["path"]), j.read(meta["path"]))
    j.require(
        all(proof.get(k) == v for k, v in expected.items()),
        "Source-clock proof differs from raw preserved evidence",
    )
    return expected


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("notices")
    p.add_argument("action_metadata")
    p.add_argument("output")
    p.add_argument("--control-evidence")
    a = p.parse_args()
    try:
        try:
            result = verify(j.read(a.notices), j.read(a.action_metadata))
        except ValueError:
            if not a.control_evidence:
                raise
            import control_clock_corroboration_v1 as controls

            result = controls.verify(j.read(a.notices), j.read(a.action_metadata), j.pin(a.control_evidence))
        result.update(
            schema_version=2,
            source_notices=j.pin(a.notices),
            action_metadata=j.pin(a.action_metadata),
            verifier_sha256=j.sha(__file__),
        )
        validate_proof(result)
        j.checked_write(Path(a.output), j.encoded(result))
        print(json.dumps({"status": result["status"], "output": a.output}))
    except (ValueError, OSError, KeyError, TypeError) as e:
        print("Rejected: " + str(e), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
