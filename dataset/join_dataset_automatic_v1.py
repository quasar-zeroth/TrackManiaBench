#!/usr/bin/env python3
"""Hash-bound, measured-clock Trackmania video/action join. Python stdlib + ffprobe/ffmpeg.

Never estimates alignment. Calibration is an independently measured input, not output.
"""

from __future__ import annotations
import argparse
import bisect
import csv
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

VERSION = "0.4.0-automatic-visibility-v1"
import visibility_contract_automatic_v1 as visibility_contract
import coverage_policy_v1 as coverage_policy
import alignment_contract_v1 as alignment_contract

SCRIPT = Path(__file__).resolve()
RENDERER_PATH = SCRIPT.parent.parent / "renderer" / "render_queue_partial_v1.py"
spec = importlib.util.spec_from_file_location("dataset_native_renderer", RENDERER_PATH)
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)
HELD = ("steer_raw", "steer_normalized", "throttle", "brake", "horn", "free_look", "jump", "action_slot_mask")
REQUIRED_CHECKS = (
    "strict_bitstream",
    "gbx_semantics_match_strict_rows",
    "single_store",
    "normalized_steering_range",
    "expected_map_uid_checked",
    "expected_finish_time_checked",
    "expected_account_id_checked",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def pin(path, expected=None):
    path = Path(path).resolve(strict=True)
    actual = sha(path)
    if expected is not None:
        require(actual == expected, f"SHA-256 mismatch: {path}")
    return {"path": str(path), "sha256": actual, "bytes": path.stat().st_size}


def evidence(reference, base):
    require(
        isinstance(reference, dict) and reference.get("path") and reference.get("sha256"),
        "Missing hash-pinned evidence",
    )
    path = Path(reference["path"])
    if not path.is_absolute():
        path = base / path
    return pin(path, reference["sha256"])


def rational(value):
    require(not isinstance(value, bool), "Boolean timestamp is invalid")
    return Fraction(str(value))


def num(value):
    return int(value) if value.denominator == 1 else float(value)


def probe_video(path, settings):
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_frames",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,avg_frame_rate,time_base,duration:"
        "frame=media_type,best_effort_timestamp_time,pts_time,duration_time,pkt_duration_time",
        "-of",
        "json",
        str(path),
    ]
    probe = json.loads(subprocess.check_output(command))
    videos = [s for s in probe["streams"] if s["codec_type"] == "video"]
    require(
        len(videos) == 1 and len(probe["streams"]) == 1, "Expected one video stream and no audio/other stream"
    )
    stream = videos[0]
    require(
        (stream["width"], stream["height"]) == (settings["width"], settings["height"]),
        "Video dimensions differ from render settings",
    )
    require(Fraction(stream["avg_frame_rate"]) == settings["fps"], "Video rate differs from render settings")
    frames = [f for f in probe["frames"] if f["media_type"] == "video"]
    require(len(frames) > 1, "Insufficient actual video frames")
    pts = [rational(f["best_effort_timestamp_time"]) * 1000 for f in frames]
    period = Fraction(1000, settings["fps"])
    require(
        all(abs(b - a - period) <= Fraction(11, 10) for a, b in zip(pts, pts[1:])),
        "PTS gaps, reordering, or wrong frame rate",
    )
    duration = frames[-1].get("duration_time", frames[-1].get("pkt_duration_time"))
    if duration is None:
        raise ValueError("Last frame has no actual packet/frame duration; cannot infer its interval")
    end = pts[-1] + rational(duration) * 1000
    require(end > pts[-1] and abs(end - pts[-1] - period) <= Fraction(11, 10), "Invalid final frame duration")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    return (
        pts,
        end,
        {
            "stream": stream,
            "frames": len(frames),
            "first_pts_ms": num(pts[0]),
            "last_pts_ms": num(pts[-1]),
            "final_interval_end_ms": num(end),
            "pts_sha256": hashlib.sha256(encoded([str(p) for p in pts] + [str(end)])).hexdigest(),
            "decoded_without_errors": True,
        },
    )


def validate_native_timeline(cal, scene, base):
    """Check measured clock composition against already validated native readback.

    Serialized scene bounds explain a transform; they never establish video PTS
    or the race clock. Those two links need separate hash-pinned measurements.
    """
    chain = cal.get("native_timeline")
    require(isinstance(chain, dict), "Native calibration needs explicit measured clock chain")
    declared = chain.get("serialized_scene", {})
    for key in (
        "preroll_ms",
        "postroll_ms",
        "record_start_ms",
        "record_end_ms",
        "entity_start_ms",
        "entity_end_ms",
        "camera_start_ms",
        "camera_end_ms",
        "start_offset_ms",
        "time_scale",
    ):
        require(
            key in declared and rational(declared[key]) == rational(scene[key]),
            "Calibration serialized scene mismatch: " + key,
        )
    video = chain.get("video_to_clip", {})
    race = chain.get("record_to_race", {})
    contract = cal.get("source_clock_contract", {})
    conditional = alignment_contract.conditional(contract)
    if contract:
        import verify_source_clocks_v2 as clocks

        proof_ref = evidence(contract["source_proof"], base)
        proof = read(proof_ref["path"])
        clocks.validate_proof(proof)
        require(
            contract == alignment_contract.source_contract(proof, contract["source_proof"]),
            "Calibration source-clock scope differs from independently verified evidence",
        )
        require(
            proof["action_metadata"]["sha256"] == cal["action_metadata_sha256"],
            "Calibration source-clock proof refers to another action source",
        )
    refs = []
    for name, link in (("video_to_clip", video), ("record_to_race", race)):
        require(
            link.get("method") and link.get("evidence_files"), "Clock link lacks measured evidence: " + name
        )
        refs.extend(evidence(ref, base) for ref in link["evidence_files"])
    pts_anchor = rational(video["video_pts_anchor_ms"])
    clip_anchor = rational(video["clip_time_anchor_ms"])
    clip_scale = rational(video["clip_ms_per_video_ms"])
    record_anchor = rational(race["record_time_anchor_ms"])
    race_anchor = rational(race["race_time_anchor_ms"])
    race_scale = rational(race["race_ms_per_record_ms"])
    require(clip_scale > 0 and race_scale > 0, "Clock link scale must be positive")
    video_error = rational(video["max_absolute_error_ms"])
    record_error = alignment_contract.mapping_bound(race, conditional)
    if contract:
        require(
            record_error >= alignment_contract.source_bound(proof),
            "Record/race bound understates source evidence",
        )
        for measurement in proof["checkpoints"]:
            predicted = race_anchor + (rational(measurement["record_time_ms"]) - record_anchor) * race_scale
            require(
                abs(predicted - rational(measurement["race_time_ms"])) <= record_error,
                "Record/race mapping disagrees with preserved source checkpoints",
            )
    if conditional:
        alignment_contract.verify_nominal(proof, record_anchor, race_anchor, race_scale)
    require(video_error >= 0 and record_error >= 0, "Clock link precision bound must be nonnegative")
    record_at_anchor = rational(scene["start_offset_ms"]) + (
        clip_anchor - rational(scene["entity_start_ms"])
    ) * rational(scene["time_scale"])
    composed_race = race_anchor + (record_at_anchor - record_anchor) * race_scale
    composed_scale = clip_scale * rational(scene["time_scale"]) * race_scale
    mapping = cal["mapping"]
    declared_race = rational(mapping["race_time_anchor_ms"]) + (
        pts_anchor - rational(mapping["video_pts_anchor_ms"])
    ) * rational(mapping["race_ms_per_video_ms"])
    require(
        composed_race == declared_race and composed_scale == rational(mapping["race_ms_per_video_ms"]),
        "Measured clock chain does not compose to declared video/race mapping",
    )
    composed_error = video_error * rational(scene["time_scale"]) * race_scale + record_error
    declared_error = alignment_contract.mapping_bound(mapping, conditional)
    require(
        declared_error >= composed_error,
        "Declared alignment precision understates measured clock-link bounds",
    )
    audit = {
        "serialized_scene": declared,
        "video_to_clip": video,
        "record_to_race": race,
        "composed_race_at_video_anchor_ms": num(composed_race),
        "composed_race_ms_per_video_ms": num(composed_scale),
        "conservative_composed_max_absolute_error_ms": num(composed_error),
        "declared_max_absolute_error_ms": num(declared_error),
        "evidence_files": refs,
        "native_end_hold_assumed": False,
    }
    if contract:
        audit["source_clock_contract"] = contract
    if conditional:
        audit.update(
            conservative_composed_max_absolute_error_ms=None,
            declared_max_absolute_error_ms=None,
            conditional_composed_max_absolute_error_ms=num(composed_error),
            conditional_declared_max_absolute_error_ms=num(declared_error),
            strict_action_aligned_training_eligible=False,
        )
    return audit


def validate_calibration(cal, expected, pts, end, base, native_scene=None):
    require(
        cal.get("schema_version") == 1 and cal.get("verified") is True,
        "Calibration has not been measured and verified",
    )
    require(
        cal.get("verified_by") and cal.get("verified_utc") and cal.get("method"),
        "Calibration lacks verifier/date/method",
    )
    for key, value in expected.items():
        require(cal.get(key) == value, f"Calibration identity mismatch: {key}")
    mapping = cal.get("mapping", {})
    anchor_pts = rational(mapping["video_pts_anchor_ms"])
    anchor_race = rational(mapping["race_time_anchor_ms"])
    scale = rational(mapping["race_ms_per_video_ms"])
    require(scale > 0, "Calibration rate must be positive")
    conditional = alignment_contract.conditional(cal.get("source_clock_contract", {}))
    native_precision = (
        alignment_contract.mapping_bound(mapping, conditional) if native_scene is not None else Fraction(0)
    )
    require(0 <= native_precision <= 30, "Native experimental alignment bound must be within 0..30 ms")
    mapped = lambda p: anchor_race + (p - anchor_pts) * scale
    finish = Fraction(expected["finish_time_ms"])
    require(mapped(pts[0]) <= 0 and mapped(end) >= finish, "Mapped video does not cover start through finish")
    measurements = cal.get("measurements", [])
    require(
        len(measurements) >= 3 and {m.get("phase") for m in measurements} >= {"start", "middle", "finish"},
        "Calibration needs independent start, middle, finish measurements",
    )
    residuals = []
    references = []
    selected = set()
    for m in measurements:
        i = m["frame_index"]
        require(type(i) is int and 0 <= i < len(pts), "Calibration frame index outside actual video")
        selected.add(i)
        observed = rational(m["observed_race_ms"])
        interval = (pts[i + 1] if i + 1 < len(pts) else end) - pts[i]
        tolerance = max(interval * scale, native_precision)
        residual = abs(mapped(pts[i]) - observed)
        require(residual <= tolerance, "Measured race clock residual exceeds one rendered frame")
        if m["phase"] == "start":
            require(abs(observed) <= max(tolerance, finish / 10), "Start measurement is too far from start")
        if m["phase"] == "middle":
            require(finish / 3 <= observed <= 2 * finish / 3, "Middle measurement is not near middle")
        if m["phase"] == "finish":
            require(
                abs(observed - finish) <= max(tolerance, finish / 10),
                "Finish measurement is too far from finish",
            )
        references.append(evidence(m.get("evidence"), base))
        residuals.append(
            {
                "phase": m["phase"],
                "frame_index": i,
                "observed_race_ms": num(observed),
                "mapped_race_ms": num(mapped(pts[i])),
                "absolute_residual_ms": num(residual),
                "allowed_residual_ms": num(tolerance),
            }
        )
    require(len(selected) >= 3, "Measurements must use at least three distinct frames")
    references.extend(evidence(r, base) for r in cal.get("evidence_files", []))
    audit = {
        "residuals": residuals,
        "evidence_files": references,
        "race_start_video_pts_ms": num(anchor_pts - anchor_race / scale),
        "race_finish_video_pts_ms": num(anchor_pts + (finish - anchor_race) / scale),
        "exported_leading_race_ms": num(-mapped(pts[0])),
        "exported_trailing_race_ms": num(mapped(end) - finish),
    }
    if native_scene is not None:
        audit["native_timeline"] = validate_native_timeline(cal, native_scene, base)
        require(
            isinstance(cal.get("observation_window"), dict),
            "Native calibration needs measured usable observation window",
        )
    if cal.get("observation_window") is not None:
        window = cal["observation_window"]
        first, last = window.get("first_usable_frame_index"), window.get("last_usable_frame_index")
        require(
            type(first) is int and type(last) is int and 0 <= first < last < len(pts),
            "Invalid usable observation frame bounds",
        )
        visibility_mode = visibility_contract.verify(window, expected, base, evidence, calibrated=True)
        first_race, last_race = mapped(pts[first]), mapped(pts[last])
        require(
            0 <= first_race < last_race < finish, "Usable observations must lie within original race clock"
        )
        require(
            rational(window["first_usable_race_ms"]) == first_race
            and rational(window["last_usable_race_ms"]) == last_race,
            "Measured usable observation bounds differ from calibrated PTS",
        )
        if "measured_visibility_intersection" in window:
            intersection = window["measured_visibility_intersection"]
            require(
                intersection.get("policy") == "measured_visible_frames_intersect_original_race_v1",
                "Unverified measured visibility intersection policy",
            )
            report_ref = evidence(intersection["source_report"], base)
            report = read(report_ref["path"])
            raw_first, raw_last = report["first_usable_frame_index"], report["last_usable_frame_index"]
            require(
                type(raw_first) is int and type(raw_last) is int and 0 <= raw_first < raw_last < len(pts),
                "Actual visible bounds outside original video",
            )
            require(
                report["accepted_video"]["sha256"] == expected["video_sha256"]
                and report["accepted_result"]["sha256"] == expected["render_result_sha256"],
                "Visibility intersection references another video/result",
            )
            require(
                visibility_contract.verify(report, expected, Path(report_ref["path"]).parent, evidence)
                == visibility_mode,
                "Visibility intersection attestation mode differs from calibration",
            )
            if visibility_mode == visibility_contract.MODE:
                require(
                    window["automatic_review"] == report["automatic_review"],
                    "Calibration automatic evidence differs from visibility source",
                )
            require(
                pts[raw_first] == rational(report["first_usable_video_pts_ms"])
                and pts[raw_last] == rational(report["last_usable_video_pts_ms"]),
                "Raw visible bounds differ from actual PTS",
            )
            indices = [i for i in range(raw_first, raw_last + 1) if 0 <= mapped(pts[i]) < finish]
            require(
                indices == list(range(first, last + 1)),
                "Usable window is not the measured visible/race intersection",
            )
            for key, value in dict(
                raw_first_visible_frame_index=raw_first,
                raw_last_visible_frame_index=raw_last,
                raw_first_visible_race_ms=num(mapped(pts[raw_first])),
                raw_last_visible_race_ms=num(mapped(pts[raw_last])),
                removed_before_race_frames=first - raw_first,
                removed_after_race_frames=raw_last - last,
                race_interval_ms=[0, expected["finish_time_ms"]],
                action_coverage_mask_applied=False,
            ).items():
                require(intersection.get(key) == value, "Visibility intersection evidence differs: " + key)

        observed_end = min(finish, mapped(pts[last + 1] if last + 1 < len(pts) else end))
        audit["observation_window"] = {
            "first_usable_frame_index": first,
            "last_usable_frame_index": last,
            "first_usable_race_ms": num(first_race),
            "last_usable_race_ms": num(last_race),
            "observed_action_interval_end_ms": num(observed_end),
            "unobserved_prefix_race_ms": num(first_race),
            "unobserved_suffix_race_ms": num(finish - observed_end),
            "training_action_coverage_ms": num(observed_end - first_race),
            "raw_action_race_coverage_ms": num(finish),
            "missing_observations_fabricated": False,
            "method": window["method"],
            "visibility_verification_mode": visibility_mode,
            "evidence_files": [evidence(ref, base) for ref in window["evidence_files"]],
        }
    return mapped, audit


def load_actions(directory, metadata, finish):
    coverage = coverage_policy.validate_metadata(metadata)
    require(coverage["race_interval_ms"] == [0, finish], "Action coverage finish differs")
    for name, details in metadata["files"].items():
        require(Path(name).name == name, "Action metadata path traversal")
        ref = pin(directory / name, details["sha256"])
        require(ref["bytes"] == details["bytes"], "Action output size mismatch")
    with (directory / "actions_all_100hz.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    require(rows, "Empty held actions")
    for i, row in enumerate(rows):
        for key in row:
            row[key] = float(row[key]) if key == "steer_normalized" else int(row[key])
        require(row["store_index"] == 0 and row["tick"] == i, "Multiple/discontinuous input stores")
        require(row["valid_until_ms"] - row["timestamp_ms"] == 10, "Original action interval is not 100 Hz")
        if i:
            require(rows[i - 1]["valid_until_ms"] == row["timestamp_ms"], "Gap or overlap in action timeline")
        require(-1 <= row["steer_normalized"] <= 1, "Steering outside normalized range")
        require(
            all(
                row[k] in (0, 1)
                for k in ("throttle", "brake", "horn", "free_look", "jump", "respawn", "secondary_respawn")
            ),
            "Invalid action flags",
        )
    known_start, known_end = coverage["known_race_interval_ms"]
    require(
        rows[0]["timestamp_ms"] <= known_start and rows[-1]["valid_until_ms"] >= known_end,
        "Actions do not cover the declared known race interval",
    )
    if not coverage["fully_covered"]:
        require(
            rows[0]["timestamp_ms"] == 10
            and [rows[0]["timestamp_ms"], rows[-1]["valid_until_ms"]]
            == coverage["original_input_interval_ms"],
            "Partial inputs were fabricated, shifted or shortened",
        )
    require(
        all(
            r["input_kind"] in (2, 3, 4)
            for r in rows
            if r["timestamp_ms"] < finish and r["valid_until_ms"] > known_start
        ),
        "Opaque input kind overlaps known driving interval",
    )
    events = read(directory / "input_events.json")
    for i, event in enumerate(events):
        require(
            event["sequence"] == i and event["store_index"] == 0,
            "Raw events have duplicate/missing sequence or input stores",
        )
        require(
            rows[0]["timestamp_ms"] <= event["timestamp_ms"] < rows[-1]["valid_until_ms"],
            "Event outside action intervals",
        )
        if i:
            require(events[i - 1]["timestamp_ms"] <= event["timestamp_ms"], "Raw events not sorted")
    return rows, events


def join_frames(
    pts,
    video_end,
    mapping,
    rows,
    events,
    finish,
    observation_window=None,
    action_coverage=None,
    alignment_error_ms=0,
    conditional_alignment=False,
):
    """Every interval is half-open. Pulse actions occur only at their exact event timestamp."""
    starts = [Fraction(row["timestamp_ms"]) for row in rows]
    event_times = [Fraction(e["timestamp_ms"]) for e in events]
    out = []
    coverage = Fraction(0)
    last_covered = Fraction(0)
    finish = Fraction(finish)
    partial = action_coverage is not None and action_coverage["fully_covered"] is False
    known_start = Fraction(action_coverage["known_race_interval_ms"][0]) if partial else Fraction(0)
    known_end = Fraction(action_coverage["known_race_interval_ms"][1]) if partial else finish
    if partial:
        require(
            action_coverage["policy"] == coverage_policy.POLICY
            and known_start == 10
            and known_end == finish
            and action_coverage["missing_race_intervals_ms"] == [[0, 10]]
            and action_coverage["extrapolated"] is False,
            "Unsupported join coverage policy",
        )
        training_start, training_end = coverage_policy.training_time_bounds(
            action_coverage, alignment_error_ms
        )
    known_coverage = Fraction(0)
    require(mapping(pts[0]) <= 0 and mapping(video_end) >= finish, "Video interval coverage incomplete")
    for i, start_pts in enumerate(pts):
        end_pts = pts[i + 1] if i + 1 < len(pts) else video_end
        r0, r1 = mapping(start_pts), mapping(end_pts)
        require(r1 > r0, "Nonpositive frame interval")
        a, b = max(Fraction(0), r0), min(finish, r1)
        pieces = []
        missing = []
        if a < b:
            require(a == last_covered, "Video intervals contain a race gap or overlap")
            if a < known_start:
                missing.append([num(a), num(min(b, known_start))])
            cursor = max(a, known_start)
            action_end = min(b, known_end)
            j = bisect.bisect_right(starts, cursor) - 1
            while cursor < action_end:
                require(0 <= j < len(rows), "Action coverage missing at video interval")
                row = rows[j]
                end = min(action_end, Fraction(row["valid_until_ms"]))
                require(starts[j] <= cursor < end, "Action interval gap or overlap")
                e0, e1 = bisect.bisect_left(event_times, cursor), bisect.bisect_left(event_times, end)
                selected = events[e0:e1]
                pieces.append(
                    {
                        "source_row_index": j,
                        "store_index": row["store_index"],
                        "tick": row["tick"],
                        "source_interval_ms": [row["timestamp_ms"], row["valid_until_ms"]],
                        "race_interval_ms": [num(cursor), num(end)],
                        "duration_ms": num(end - cursor),
                        "held": {k: row[k] for k in HELD},
                        "raw_event_sequences": [e["sequence"] for e in selected],
                        "respawn_events": sum(e["type"] == "RespawnTM2020" for e in selected),
                        "secondary_respawn_events": sum(e["type"] == "SecondaryRespawn" for e in selected),
                    }
                )
                cursor = end
                j += 1
            known_coverage += max(Fraction(0), action_end - max(a, known_start))
            coverage += b - a
            last_covered = b
        phase = "preroll" if r0 < 0 else "race" if r0 < finish else "postfinish"
        usable = (
            observation_window is None
            or observation_window["first_usable_frame_index"]
            <= i
            <= observation_window["last_usable_frame_index"]
        )
        next_usable = (
            observation_window is None
            or observation_window["first_usable_frame_index"]
            <= i + 1
            <= observation_window["last_usable_frame_index"]
        )
        training_eligible = phase == "race" and usable
        transition_eligible = phase == "race" and usable and next_usable and i + 1 < len(pts) and r1 < finish
        if partial:
            training_eligible = training_eligible and not missing and training_start <= r0 <= training_end
            transition_eligible = (
                transition_eligible and training_eligible and training_start <= r1 <= training_end
            )
        frame = {
            "frame_index": i,
            "video_pts_ms": num(start_pts),
            "video_interval_end_ms": num(end_pts),
            "race_time_ms": num(r0),
            "race_interval_end_ms": num(r1),
            "phase": phase,
            "observation_usable": usable,
            "training_eligible": training_eligible,
            "transition_eligible": transition_eligible,
            "race_action_overlap_ms": num(sum(Fraction(str(p["duration_ms"])) for p in pieces)),
            "action_substeps": pieces,
        }
        if partial:
            frame.update(
                missing_action_intervals_ms=missing,
                race_interval_overlap_ms=num(max(Fraction(0), b - a)),
                control_interval_fully_observed=not missing,
                known_controls_clock_guard_ms=num(Fraction(str(alignment_error_ms))),
                training_nominal_race_bounds_ms=[num(training_start), num(training_end)],
            )
        if conditional_alignment:
            frame.update(
                experimental_training_eligible=training_eligible,
                experimental_transition_eligible=transition_eligible,
                video_training_eligible=phase == "race" and usable,
                training_eligible=False,
                transition_eligible=False,
                time_alignment_evidence_class=alignment_contract.CONDITIONAL,
                time_alignment_max_absolute_error_ms=None,
                time_alignment_conditional_max_absolute_error_ms=num(Fraction(str(alignment_error_ms))),
            )
        out.append(frame)
    require(
        coverage == finish and last_covered == finish, "Joined action duration does not exactly cover race"
    )
    require(known_coverage == known_end - known_start, "Known action intervals are incomplete or duplicated")
    if partial:
        require(
            sum(Fraction(str(b)) - Fraction(str(a)) for f in out for a, b in f["missing_action_intervals_ms"])
            == known_start,
            "Unknown control duration was lost or misrepresented",
        )
    expected_sequences = [e["sequence"] for e in events if 0 <= e["timestamp_ms"] < finish]
    actual_sequences = [
        seq for f in out for part in f["action_substeps"] for seq in part["raw_event_sequences"]
    ]
    require(actual_sequences == expected_sequences, "Raw events duplicated or lost across frame boundaries")
    return out


from splits import split_runs


def prepare_run(manifest, run, render_root, calibration_root):
    rid = run["run_id"]
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", rid), "Unsafe run ID")
    directory = Path(run["actions"]["directory"]).resolve(strict=True)
    metadata_ref = pin(directory / "metadata.json", run["actions"]["metadata_sha256"])
    metadata = read(metadata_ref["path"])
    expected = {
        "map_uid": manifest["map"]["uid"],
        "map_sha256": manifest["map"]["sha256"],
        "run_id": rid,
        "account_id": run["account_id"],
        "finish_time_ms": run["record_time_ms"],
        "ghost_sha256": run["ghost_sha256"],
        "action_metadata_sha256": metadata_ref["sha256"],
    }
    require(metadata.get("status") == "extracted", "Action extraction is incomplete")
    for key in ("map_uid", "account_id", "finish_time_ms"):
        require(metadata.get(key) == expected[key], "Action identity mismatch: " + key)
    require(metadata["source_sha256"] == expected["ghost_sha256"], "Action source ghost mismatch")
    require(
        all(metadata.get("checks", {}).get(k) is True for k in REQUIRED_CHECKS),
        "Action parser verification incomplete",
    )
    action_coverage = coverage_policy.validate_metadata(metadata, run)
    ghost_ref = pin(run["ghost_path"], run["ghost_sha256"])
    result_ref = pin(render_root / rid / "result.json")
    result = read(result_ref["path"])
    backend = renderer.backend_identity(result.get("renderer_backend", "openplanet"))
    renderer.validate_render_result(result, manifest, run, render_root / rid)
    video_ref = pin(render_root / rid / "video.webm", result["video"]["sha256"])
    pts, end, video_probe = probe_video(
        video_ref["path"], renderer.settings_for_backend(backend["renderer_backend"])
    )
    require(len(pts) == result["video"]["frames"], "Render result frame count mismatch")
    expected.update(render_result_sha256=result_ref["sha256"], video_sha256=video_ref["sha256"])
    calibration_ref = pin(calibration_root / (rid + ".json"))
    cal = read(calibration_ref["path"])
    native_scene = None
    if backend["renderer_backend"] == "native_ui":
        native_scene = dict(result["timeline"]["serialized_scene_timing"])
        native_scene.update({k: result["timeline"]["entity"][k] for k in ("start_offset_ms", "time_scale")})
    mapping, cal_audit = validate_calibration(
        cal, expected, pts, end, Path(calibration_ref["path"]).parent, native_scene
    )
    rows, events = load_actions(directory, metadata, run["record_time_ms"])
    conditional = alignment_contract.conditional(cal.get("source_clock_contract", {}))
    alignment_error = (
        num(alignment_contract.mapping_bound(cal["mapping"], conditional)) if native_scene else 0
    )
    frames = join_frames(
        pts,
        end,
        mapping,
        rows,
        events,
        run["record_time_ms"],
        cal_audit.get("observation_window"),
        action_coverage,
        alignment_error,
        conditional,
    )
    if not action_coverage["fully_covered"]:
        known_ms = action_coverage["known_race_interval_ms"][1] - action_coverage["known_race_interval_ms"][0]
        if cal_audit.get("observation_window"):
            cal_audit["observation_window"]["raw_action_race_coverage_ms"] = known_ms
            cal_audit["observation_window"]["missing_raw_action_intervals_ms"] = action_coverage[
                "missing_race_intervals_ms"
            ]
            cal_audit["observation_window"]["training_action_coverage_ms"] = sum(
                f["race_action_overlap_ms"] for f in frames if f["training_eligible"]
            )
    if native_scene is not None:
        for frame in frames:
            frame["time_alignment_max_absolute_error_ms"] = cal_audit["native_timeline"][
                "declared_max_absolute_error_ms"
            ]
    summary = {
        "schema_version": 1,
        "status": "joined_verified",
        "identity": expected,
        "ordinal": run["ordinal"],
        "rank": run["rank"],
        "frames": len(frames),
        "training_frames": sum(f["training_eligible"] for f in frames),
        "transition_frames": sum(f["transition_eligible"] for f in frames),
        "race_coverage_ms": run["record_time_ms"],
        "training_action_coverage_ms": sum(
            f["race_action_overlap_ms"] for f in frames if f["training_eligible"]
        ),
        "observation_window": cal_audit.get("observation_window"),
        "raw_events": pin(directory / "input_events.json", metadata["files"]["input_events.json"]["sha256"]),
        "source_actions": pin(
            directory / "actions_all_100hz.csv", metadata["files"]["actions_all_100hz.csv"]["sha256"]
        ),
        "source_ghost": ghost_ref,
        "action_metadata": metadata_ref,
        "render_result": result_ref,
        "video": video_ref,
        "calibration": calibration_ref,
        "calibration_audit": cal_audit,
        "video_probe": video_probe,
        "mapping": cal["mapping"],
        "settings": result["settings"],
        **backend,
        "renderer_validator_files": [pin(RENDERER_PATH)]
        + (
            [pin(RENDERER_PATH.with_name("native_ui_queue_partial_v1.py"))]
            if backend["renderer_backend"] == "native_ui"
            else []
        ),
        "joiner_sha256": sha(SCRIPT),
        "renderer_validator_sha256": sha(RENDERER_PATH),
        "semantics": "Frame observation at actual PTS; controls apply to the half-open interval before next observation. Race clipped to [0, finish). Every original 100 Hz held substep retained, including controls during an explicitly unobserved prefix/suffix for audit. Training requires the measured usable observation window. Pulse actions use exact raw-event timestamps. Preroll/postfinish excluded; partial finish intervals retained without invalid transition targets.",
    }
    summary["visibility_contract_source"] = pin(Path(visibility_contract.__file__))
    summary["coverage_policy_source"] = pin(Path(coverage_policy.__file__))
    summary["reviewed_coverage_policy"] = coverage_policy.policy_reference()
    summary["action_coverage"] = action_coverage
    if cal.get("source_clock_contract"):
        summary["source_clock_contract"] = cal["source_clock_contract"]
        summary["alignment_contract_source"] = pin(Path(alignment_contract.__file__))
    if conditional:
        summary.update(
            experimental_training_frames=sum(f["experimental_training_eligible"] for f in frames),
            experimental_transition_frames=sum(f["experimental_transition_eligible"] for f in frames),
            experimental_training_action_coverage_ms=sum(
                f["race_action_overlap_ms"] for f in frames if f["experimental_training_eligible"]
            ),
            video_training_frames=sum(f["video_training_eligible"] for f in frames),
            strict_action_aligned_training_eligible=False,
        )
        summary["semantics"] += (
            " Physical-state/action timing is conditional. Strict training and transition masks are false. Experimental masks use the assumed error margin; video-only masks use visible in-race frames. No full-race physical-state timing guarantee is asserted."
        )
    if not action_coverage["fully_covered"]:
        eligible = [f for f in frames if f.get("experimental_training_eligible", f["training_eligible"])]
        require(eligible, "No measured frames have known controls after uncertainty masking")
        summary.update(
            race_coverage_ms=known_ms,
            full_race_duration_ms=run["record_time_ms"],
            missing_action_race_ms=run["record_time_ms"] - known_ms,
            training_first_frame_index=eligible[0]["frame_index"],
            training_last_frame_index=eligible[-1]["frame_index"],
            training_time_guard_ms=alignment_error,
        )
        if conditional:
            summary["experimental_training_first_frame_index"] = summary.pop("training_first_frame_index")
            summary["experimental_training_last_frame_index"] = summary.pop("training_last_frame_index")
        summary["semantics"] += (
            " This source lacks controls for [0,10)ms. Unknown intervals are explicit and contain no held-control values. Training observations and targets stay inside the known interval after applying the declared measured or conditional clock margin. The measured visual window remains distinct from training eligibility."
        )
    return frames, summary


def checked_write(path, payload, audit=False):
    if path.exists():
        require(
            path.read_bytes() == payload,
            "Completed dataset output differs from current verified inputs: " + str(path),
        )
    else:
        require(not audit, "Missing dataset output: " + str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".partial")
        temp.write_bytes(payload)
        temp.replace(path)
    return pin(path)


def execute(args):
    manifest_path = Path(args.actions).resolve(strict=True)
    manifest = read(manifest_path)
    require(
        manifest.get("status") == "actions_verified"
        and not manifest.get("failures")
        and not manifest.get("source_collection_failures"),
        "Action cohort incomplete",
    )
    require(type(args.count) is int and args.count > 0, "Count must be positive")
    require(
        manifest.get("requested_count") == args.count and len(manifest["runs"]) == args.count,
        "Expected exactly requested number of action runs",
    )
    runs = sorted(manifest["runs"], key=lambda r: r["ordinal"])
    require(
        [r["ordinal"] for r in runs] == list(range(1, args.count + 1)),
        "Leaderboard positions must be complete and contiguous",
    )
    for key in ("run_id", "account_id", "ghost_sha256"):
        require(len({r[key] for r in runs}) == args.count, "Duplicate " + key)
    map_ref = pin(manifest["map"]["path"], manifest["map"]["sha256"])
    render_root, calibration_root, output = (
        Path(p).resolve() for p in (args.render_root, args.calibrations, args.output)
    )
    gate_ref = None
    gate = None
    if args.count > 5:
        require(args.gate, "Production beyond five runs requires the measured five-run pilot gate")
        gate_path = Path(args.gate).resolve(strict=True)
        gate = renderer.validate_gate(gate_path, manifest, render_root)
        gate_ref = pin(gate_path)
        # Calibration hashes are mandatory in addition to the native renderer gate.
        for pilot in gate["pilot_runs"]:
            run = runs[pilot["ordinal"] - 1]
            ref = evidence(pilot.get("calibration"), gate_path.parent)
            require(
                Path(ref["path"]) == (calibration_root / (run["run_id"] + ".json")).resolve(),
                "Gate references another calibration",
            )
    split_fractions = getattr(args, "split_fractions", "0.8,0.1,0.1")
    splits = split_runs(runs, args.split_seed, split_fractions)
    produced = []
    cohort_backend = None
    for run in runs:
        frames, summary = prepare_run(manifest, run, render_root, calibration_root)
        backend = {k: summary[k] for k in ("renderer_backend", "renderer_build", "plugin_build")}
        if cohort_backend is None:
            cohort_backend = backend
        require(cohort_backend == backend, "Mixed rendering backends/builds in one cohort")
        if gate is not None:
            require(
                renderer.backend_identity(gate.get("renderer_backend", "openplanet")) == backend,
                "Pilot gate backend differs from joined cohort",
            )
        summary["split"] = splits[run["run_id"]]
        folder = output / "runs" / run["run_id"]
        joined_ref = checked_write(
            folder / "frame_actions.jsonl", b"".join(encoded(f) for f in frames), args.audit
        )
        summary["frame_actions"] = joined_ref
        summary_ref = checked_write(folder / "summary.json", encoded(summary), args.audit)
        produced.append(
            {
                "run_id": run["run_id"],
                "account_id": run["account_id"],
                "ordinal": run["ordinal"],
                "split": summary["split"],
                "summary": summary_ref,
                "frame_actions": joined_ref,
                "frames": summary["frames"],
                "training_frames": summary["training_frames"],
                "timing_evidence_class": summary.get("source_clock_contract", {}).get(
                    "evidence_class", alignment_contract.DIRECT
                ),
                "experimental_training_frames": summary.get(
                    "experimental_training_frames", summary["training_frames"]
                ),
                "experimental_transition_frames": summary.get(
                    "experimental_transition_frames", summary["transition_frames"]
                ),
                "video_training_frames": summary.get(
                    "video_training_frames",
                    sum(f["phase"] == "race" and f["observation_usable"] for f in frames),
                ),
            }
        )
        print(
            json.dumps(
                {
                    "run_id": run["run_id"],
                    "status": "audited" if args.audit else "joined_verified",
                    "frames": len(frames),
                }
            ),
            flush=True,
        )
    artifact = {
        "schema_version": 1,
        "status": "dataset_verified",
        "joiner_version": VERSION,
        "requested_count": args.count,
        "map": map_ref,
        "map_uid": manifest["map"]["uid"],
        "actions_manifest": pin(manifest_path),
        "pilot_gate": gate_ref,
        "split_seed": args.split_seed,
        "split_fractions": split_fractions,
        "split_counts": {s: sum(r["split"] == s for r in produced) for s in sorted(set(splits.values()))},
        "settings": renderer.settings_for_backend(cohort_backend["renderer_backend"]),
        **cohort_backend,
        "runs": produced,
        "video_alignment_verified": all(
            r["timing_evidence_class"] == alignment_contract.DIRECT for r in produced
        ),
        "video_clip_alignment_verified": True,
        "timing_evidence_counts": {
            key: sum(r["timing_evidence_class"] == key for r in produced)
            for key in (alignment_contract.DIRECT, alignment_contract.CONDITIONAL)
        },
        "joiner_sha256": sha(SCRIPT),
        "renderer_validator_sha256": sha(RENDERER_PATH),
        "inputs": {
            "actions": str(manifest_path),
            "render_root": str(render_root),
            "calibrations": str(calibration_root),
            "gate": args.gate,
        },
    }
    checked_write(output / "dataset_manifest.json", encoded(artifact), args.audit)
    print(
        json.dumps(
            {
                "status": "audit_passed" if args.audit else "dataset_verified",
                "runs": len(produced),
                "output": str(output),
            }
        )
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--actions", required=True)
    p.add_argument("--render-root", required=True)
    p.add_argument("--calibrations", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--count", type=int, required=True)
    p.add_argument("--gate")
    p.add_argument(
        "--split-fractions",
        default="0.8,0.1,0.1",
        help="Train,validation,test fractions; largest-remainder allocation",
    )
    p.add_argument("--split-seed", default="trackmaniabench-v1")
    p.add_argument(
        "--audit", action="store_true", help="Recompute every join and verify existing bytes, without writing"
    )
    args = p.parse_args()
    try:
        execute(args)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print("Rejected: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
