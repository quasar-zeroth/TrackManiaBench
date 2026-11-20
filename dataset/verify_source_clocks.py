#!/usr/bin/env python3
"""Corroborate preserved record/race clocks using exact checkpoint notice values.

This verifies the observed v3/v5 arena-player notice layouts only. It does not
infer an exact sub-tick offset or approve video alignment.
"""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import struct
import sys
import join_dataset as shared


def verify(raw, metadata):
    shared.require(raw["source_sha256"] == metadata["source_sha256"], "Clock evidence/source ghost mismatch")
    for key in ("map_uid", "ghost_login", "finish_time_ms"):
        shared.require(raw[key] == metadata[key], "Clock evidence identity mismatch: " + key)
    checkpoints = [c["time_ms"] for c in metadata["checkpoints"]]
    shared.require(
        checkpoints and checkpoints[-1] == metadata["finish_time_ms"], "Checkpoint series lacks exact finish"
    )
    shared.require(
        [c["race_time_ms"] for c in raw["checkpoints"]] == checkpoints, "Checkpoint records differ"
    )
    events = [e for e in raw["events"] if e["notice_class_id"] == "0x032CB000" and e["notice_type"] == 21]
    shared.require(len(events) == len(checkpoints), "Missing or extra arena-player checkpoint notices")
    measurements = []
    for index, (event, race_time) in enumerate(zip(events, checkpoints)):
        version = event["notice_archive_version"]
        shared.require(version in (3, 5), "Unsupported arena-player notice archive")
        payload = base64.b64decode(event["raw_payload_base64"], validate=True)
        shared.require(
            hashlib.sha256(payload).hexdigest() == event["raw_payload_sha256"], "Notice payload hash mismatch"
        )
        shared.require(
            len(payload) == event["raw_payload_bytes"] == (30 if version == 3 else 34),
            "Unsupported checkpoint notice size",
        )
        words = list(struct.unpack("<" + "i" * (len(payload) // 4), payload[: len(payload) // 4 * 4]))
        shared.require(
            words == event["little_endian_int32_words_for_investigation"], "Decoded notice words differ"
        )
        shared.require(
            words[0] == 0 and words[1] == words[2] == race_time and words[6] == 0,
            "Notice does not contain exact source checkpoint time",
        )
        shared.require(
            words[5] == (65536 if index == len(checkpoints) - 1 else index + 1),
            "Checkpoint ordinal/finish marker differs",
        )
        shared.require(
            int.from_bytes(payload[-2:], "little") == (0 if version == 3 else index + 1),
            "Unrecognized notice trailer",
        )
        record_time = event["record_timestamp_ms"]
        difference = record_time - race_time
        shared.require(
            record_time % 10 == 0 and abs(difference) < 10,
            "Record/race notice clocks differ by at least one input tick",
        )
        if measurements:
            shared.require(record_time > measurements[-1]["record_time_ms"], "Notices are not ordered")
        measurements.append(
            dict(
                checkpoint_ordinal=index + 1,
                finish=index == len(checkpoints) - 1,
                race_time_ms=race_time,
                record_time_ms=record_time,
                record_minus_race_ms=difference,
                notice_archive_version=version,
                raw_payload_sha256=event["raw_payload_sha256"],
            )
        )
    diffs = [m["record_minus_race_ms"] for m in measurements]
    phase = (
        "ceil_to_10ms"
        if all(d >= 0 for d in diffs)
        else "floor_to_10ms"
        if all(d <= 0 for d in diffs)
        else "mixed_within_10ms"
    )
    return dict(
        status="record_race_checkpoint_clocks_corroborated_within_10ms",
        checkpoints=measurements,
        observed_notice_quantization=phase,
        maximum_observed_absolute_difference_ms=max(map(abs, diffs)),
        precision_bound_ms=10,
        nominal_identity_clock_consistent=True,
        exact_subtick_offset_verified=False,
        video_alignment_verified=False,
        interpretation="Typed arena-player notice descriptors plus exact duplicate checkpoint-time words corroborate the same race timeline. Notice scheduling varies between game builds. This is a bounded checkpoint-clock relation, not evidence that notice timestamps or source vehicle samples have an exact zero sub-tick offset.",
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("notices")
    p.add_argument("action_metadata")
    p.add_argument("output")
    a = p.parse_args()
    try:
        notice_ref = shared.pin(a.notices)
        metadata_ref = shared.pin(a.action_metadata)
        result = verify(shared.read(a.notices), shared.read(a.action_metadata))
        result.update(
            schema_version=1,
            source_notices=notice_ref,
            action_metadata=metadata_ref,
            verifier_sha256=shared.sha(Path(__file__)),
        )
        shared.checked_write(Path(a.output), shared.encoded(result))
        print(json.dumps({"status": result["status"], "output": a.output}))
    except (ValueError, OSError, KeyError, TypeError) as e:
        print("Rejected: " + str(e), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
