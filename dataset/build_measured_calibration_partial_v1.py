#!/usr/bin/env python3
"""Create a calibration only from accepted native media and measured reports.

Requires explicit record/race anchors; never substitutes a default alignment.
The source-clock, readable pulse and full-window visibility evidence must exist.
"""

import argparse
from datetime import datetime, timezone
from fractions import Fraction
import json
from pathlib import Path
import sys
import join_dataset_partial_v1 as j
import verify_source_clocks_v2 as clocks
import alignment_contract_v1 as contract


def validate_record_link(clock, record_anchor, race_anchor, scale, error):
    j.require(
        scale > 0 and error >= contract.source_bound(clock), "Explicit record-clock rate/bound is invalid"
    )
    if contract.conditional(clock):
        contract.verify_nominal(clock, record_anchor, race_anchor, scale)
    for measurement in clock["checkpoints"]:
        predicted = race_anchor + (j.rational(measurement["record_time_ms"]) - record_anchor) * scale
        j.require(
            abs(predicted - j.rational(measurement["race_time_ms"])) <= error,
            "Explicit record/race anchors disagree with original checkpoint evidence",
        )


def build(args):
    paths = {
        name: Path(getattr(args, name)).resolve(strict=True)
        for name in ("render_result", "action_metadata", "source_clock", "pulses", "visibility")
    }
    documents = {name: j.read(path) for name, path in paths.items()}
    result, metadata, clock, pulse, window = (documents[name] for name in paths)
    run = result["run"]
    manifest = {"map": result["map"], "runs": [run]}
    j.renderer.validate_render_result(result, manifest, run, paths["render_result"].parent)
    j.require(result["renderer_backend"] == "native_ui", "Measured native reports require native UI backend")
    for key, value in {
        "source_sha256": run["ghost_sha256"],
        "account_id": run["account_id"],
        "map_uid": result["map"]["uid"],
        "finish_time_ms": run["record_time_ms"],
        "status": "extracted",
    }.items():
        j.require(metadata.get(key) == value, "Action metadata identity differs: " + key)
    j.load_actions(paths["action_metadata"].parent, metadata, run["record_time_ms"])
    for report, result_key, video_key in (
        (pulse, "accepted_clean_result", "accepted_clean_video"),
        (window, "accepted_result", "accepted_video"),
    ):
        j.require(
            report.get("verified_by") and report.get("verified_utc"),
            "Report lacks actual measurement attestation",
        )
        j.require(
            report[result_key]["sha256"] == j.sha(paths["render_result"])
            and report[video_key]["sha256"] == result["video"]["sha256"],
            "Measured report belongs to another accepted render/video",
        )
    j.require(
        pulse["source_record_to_race_evidence"]["sha256"] == j.sha(paths["source_clock"]),
        "Pulse report clock source differs",
    )
    j.require(
        clock["action_metadata"]["sha256"] == j.sha(paths["action_metadata"]),
        "Clock evidence action source differs",
    )
    recomputed = clocks.validate_proof(clock)
    raw_ref = j.evidence(clock["source_notices"], paths["source_clock"].parent)
    j.require(
        all(clock.get(k) == v for k, v in recomputed.items()),
        "Source clock proof differs from actual raw notices",
    )
    conditional = contract.conditional(clock)
    source_contract = contract.source_contract(clock, j.pin(paths["source_clock"]))
    scene = dict(result["timeline"]["serialized_scene_timing"])
    scene.update({k: result["timeline"]["entity"][k] for k in ("start_offset_ms", "time_scale")})
    scene_keys = (
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
    )
    scene = {k: scene[k] for k in scene_keys}
    video = pulse["video_to_clip_nominal_mapping"]
    record_anchor = j.rational(args.record_anchor_ms)
    race_anchor = j.rational(args.race_anchor_ms)
    race_scale = j.rational(args.race_ms_per_record_ms)
    validate_record_link(clock, record_anchor, race_anchor, race_scale, j.rational(args.record_error_ms))
    video_pts_anchor = j.rational(video["video_pts_anchor_ms"])
    clip_anchor = j.rational(video["clip_time_anchor_ms"])
    record_at_anchor = j.rational(scene["start_offset_ms"]) + (
        clip_anchor - j.rational(scene["entity_start_ms"])
    ) * j.rational(scene["time_scale"])
    composed_race = race_anchor + (record_at_anchor - record_anchor) * race_scale
    composed_scale = j.rational(video["clip_ms_per_video_ms"]) * j.rational(scene["time_scale"]) * race_scale
    error_bound = j.rational(video["max_absolute_error_ms"]) * j.rational(
        scene["time_scale"]
    ) * race_scale + j.rational(args.record_error_ms)
    j.require(
        j.rational(args.record_error_ms) >= contract.source_bound(clock),
        "Explicit record-clock bound understates source evidence",
    )
    mapping = lambda pts: composed_race + (pts - video_pts_anchor) * composed_scale
    video_path = paths["render_result"].parent / "video.webm"
    pts, end, _ = j.probe_video(video_path, result["settings"])
    first, last = window["first_usable_frame_index"], window["last_usable_frame_index"]
    j.require(
        type(first) is int and type(last) is int and 0 <= first < last < len(pts),
        "Measured usable window outside actual video",
    )
    j.require(
        pts[first] == j.rational(window["first_usable_video_pts_ms"])
        and pts[last] == j.rational(window["last_usable_video_pts_ms"]),
        "Usable-window report PTS differs from actual video",
    )
    raw_first, raw_last = first, last
    visible_race = [
        index for index in range(raw_first, raw_last + 1) if 0 <= mapping(pts[index]) < run["record_time_ms"]
    ]
    j.require(len(visible_race) >= 2, "Measured visible frames do not intersect the original race")
    first, last = visible_race[0], visible_race[-1]
    j.require(visible_race == list(range(first, last + 1)), "Measured race visibility is not contiguous")
    visibility_intersection = dict(
        policy="measured_visible_frames_intersect_original_race_v1",
        raw_first_visible_frame_index=raw_first,
        raw_last_visible_frame_index=raw_last,
        raw_first_visible_race_ms=j.num(mapping(pts[raw_first])),
        raw_last_visible_race_ms=j.num(mapping(pts[raw_last])),
        removed_before_race_frames=first - raw_first,
        removed_after_race_frames=raw_last - last,
        race_interval_ms=[0, run["record_time_ms"]],
        action_coverage_mask_applied=False,
        source_report=j.pin(paths["visibility"]),
    )
    references = []

    def collect(value, base):
        if isinstance(value, dict):
            if isinstance(value.get("path"), str) and isinstance(value.get("sha256"), str):
                references.append(j.evidence(value, base))
            for v in value.values():
                collect(v, base)
        elif isinstance(value, list):
            for v in value:
                collect(v, base)

    for name in ("source_clock", "pulses", "visibility"):
        collect(documents[name], paths[name].parent)
        references.append(j.pin(paths[name]))
    expected = dict(
        map_uid=result["map"]["uid"],
        map_sha256=result["map"]["sha256"],
        run_id=run["run_id"],
        account_id=run["account_id"],
        finish_time_ms=run["record_time_ms"],
        ghost_sha256=run["ghost_sha256"],
        action_metadata_sha256=j.sha(paths["action_metadata"]),
        render_result_sha256=j.sha(paths["render_result"]),
        video_sha256=result["video"]["sha256"],
    )
    identity = {
        "sources": {name: j.pin(path) for name, path in paths.items()},
        "record_anchor_ms": str(record_anchor),
        "race_anchor_ms": str(race_anchor),
        "race_ms_per_record_ms": str(race_scale),
        "record_error_ms": str(j.rational(args.record_error_ms)),
        "builder_sha256": j.sha(Path(__file__)),
        "joiner": j.pin(Path(j.__file__)),
        "clock_verifier": j.pin(Path(clocks.__file__)),
        "coverage_policy": j.pin(Path(j.coverage_policy.__file__)),
        "alignment_contract": j.pin(Path(contract.__file__)),
    }
    identity["reviewed_coverage_policy"] = j.coverage_policy.policy_reference()
    cal = dict(
        schema_version=1,
        verified=True,
        verified_by="build_measured_calibration_partial_v1 checked actual reports by "
        + pulse["verified_by"]
        + " / "
        + window["verified_by"],
        verified_utc=datetime.now(timezone.utc).isoformat(),
        method="Native event-boundary measurements and exact readback compose with explicitly supplied, source-notice-corroborated record/race anchors. Preserve the conservative error bound and actual contiguous observation window. Native marker labels denote events, not exact frame exposure clocks.",
        **expected,
        builder_inputs=identity,
        mapping=dict(
            video_pts_anchor_ms=j.num(video_pts_anchor),
            race_time_anchor_ms=j.num(composed_race),
            race_ms_per_video_ms=j.num(composed_scale),
            max_absolute_error_ms=j.num(error_bound),
        ),
        native_timeline=dict(
            serialized_scene=scene,
            video_to_clip={
                **video,
                "method": "Actual readable marker boundary report, native readback and clean/diagnostic correspondence.",
                "evidence_files": [j.pin(paths["pulses"])],
            },
            record_to_race=dict(
                record_time_anchor_ms=j.num(record_anchor),
                race_time_anchor_ms=j.num(race_anchor),
                race_ms_per_record_ms=j.num(race_scale),
                max_absolute_error_ms=j.num(j.rational(args.record_error_ms)),
                method="Explicit record/race anchors checked against this run’s versioned source-clock proof and preserved typed notices. No exact sub-tick phase asserted.",
                evidence_files=[j.pin(paths["source_clock"]), raw_ref],
            ),
        ),
        measurements=[
            dict(
                phase=p["phase"],
                frame_index=p["first_visible_frame"],
                observed_race_ms=p["known_event_nominal_race_ms"],
                measurement_kind="interval_censored_native_marker_event_boundary",
                evidence=j.pin(paths["pulses"]),
            )
            for p in pulse["pulses"]
        ],
        observation_window=dict(
            first_usable_frame_index=first,
            last_usable_frame_index=last,
            first_usable_race_ms=j.num(mapping(pts[first])),
            last_usable_race_ms=j.num(mapping(pts[last])),
            reviewed_contiguous_race_visibility=window["reviewed_contiguous_race_visibility"],
            method=window["method"],
            measured_visibility_intersection=visibility_intersection,
            evidence_files=[j.pin(paths["visibility"])] + window["evidence_files"],
        ),
        evidence_files=list({r["path"]: r for r in references}.values()),
        experimental_precision_contract=dict(
            max_absolute_error_ms=j.num(error_bound),
            initial_one_frame_20ms_target_met=error_bound <= 20,
            full_top5_pilot_gate_passed=False,
            missing_observations_fabricated=False,
        ),
    )
    cal["source_clock_contract"] = source_contract
    if conditional:
        cal["method"] = (
            "Native video/clip timing is measured. Record/race alignment is a conditional control-reflection inference; its nominal mapping and assumed error are retained separately. It is excluded from strict action-aligned training."
        )
        for value, bound in (
            (cal["mapping"], error_bound),
            (cal["native_timeline"]["record_to_race"], j.rational(args.record_error_ms)),
            (cal["experimental_precision_contract"], error_bound),
        ):
            value["max_absolute_error_ms"] = None
            value["conditional_max_absolute_error_ms"] = j.num(bound)
        cal["native_timeline"]["record_to_race"]["method"] = (
            "Conditional nominal mapping from selected vehicle control reflection; capture-order assumptions and phase ambiguities remain in the source proof. No uniform physical-state clock guarantee."
        )
        for measurement in cal["measurements"]:
            measurement["measurement_kind"] = (
                "interval_censored_native_marker_event_boundary_conditional_race_projection"
            )
            measurement["race_clock_basis"] = "conditional_nominal_projection_not_direct_physical_observation"
        cal["experimental_precision_contract"].update(
            full_race_uniform_error_guarantee=False,
            strict_action_aligned_training_eligible=False,
            initial_one_frame_20ms_target_met=False,
        )
    j.validate_calibration(cal, expected, pts, end, Path(args.output).resolve().parent, scene)
    output = Path(args.output).resolve()
    if output.exists():
        existing = j.read(output)
        j.require(
            existing.get("builder_inputs") == identity,
            "Completed calibration sources/anchors/tooling changed",
        )
        j.validate_calibration(existing, expected, pts, end, output.parent, scene)
    elif not args.check_only:
        j.checked_write(output, j.encoded(cal))
    return dict(
        status="measured_calibration_checked" if args.check_only else "measured_calibration_verified",
        run_id=run["run_id"],
        output=str(output),
        max_absolute_error_ms=None if conditional else j.num(error_bound),
        conditional_max_absolute_error_ms=j.num(error_bound) if conditional else None,
        evidence_class=clock["evidence_class"],
        first_usable_frame=first,
        last_usable_frame=last,
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("render-result", "action-metadata", "source-clock", "pulses", "visibility", "output"):
        p.add_argument("--" + name, required=True)
    for name in ("record-anchor-ms", "race-anchor-ms", "race-ms-per-record-ms", "record-error-ms"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--check-only", action="store_true")
    args = p.parse_args()
    try:
        print(json.dumps(build(args)))
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        print("Rejected: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
