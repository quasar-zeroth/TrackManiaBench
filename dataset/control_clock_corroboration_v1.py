"""Conditional control-reflection evidence; never proves physical clock phase."""

import csv
import importlib.util
import json
from pathlib import Path
import sys
import join_dataset as j
import coverage_policy_v1 as coverage

import os

ANALYZER_SHA = os.environ.get("TRACKMANIABENCH_CONTROL_ANALYZER_SHA256")
EXACT_VERIFIER_SHA = os.environ.get("TRACKMANIABENCH_EXACT_VERIFIER_SHA256")
EVIDENCE_INDEX_SHA = os.environ.get("TRACKMANIABENCH_CONTROL_EVIDENCE_SHA256")
EVIDENCE_INDEX = os.environ.get("TRACKMANIABENCH_CONTROL_EVIDENCE_INDEX")


def evidence_index_reference():
    j.require(
        EVIDENCE_INDEX and EVIDENCE_INDEX_SHA and ANALYZER_SHA and EXACT_VERIFIER_SHA,
        "Conditional evidence requires explicit reviewed index/analyzer/verifier paths and SHA-256 pins",
    )
    return j.pin(EVIDENCE_INDEX, EVIDENCE_INDEX_SHA)


def module(ref):
    path = j.evidence(ref, Path("."))["path"]
    name = "clock_evidence_" + ref["sha256"]
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    previous = list(sys.path)
    try:
        sys.path[:0] = [str(Path(path).parent), str(Path(__file__).parent)]
        spec.loader.exec_module(value)
    finally:
        sys.path[:] = previous
    sys.modules[name] = value
    return value


def checked_ref(ref):
    actual = j.evidence(ref, Path("."))
    return actual, j.read(actual["path"])


def validate_sample_extent(raw, metadata, folder):
    ref = j.pin(folder / "visual_samples.csv", metadata["files"]["visual_samples.csv"]["sha256"])
    selected = raw["selected_entity_indices"]
    with Path(ref["path"]).open(newline="") as stream:
        expected = [
            (int(r["entity_index"]), int(r["sample_index"]), int(float(r["raw_timestamp_ms"])))
            for r in csv.DictReader(stream)
            if r["source"] == "record_data" and int(r["entity_index"]) in selected
        ]
    actual = [
        (e["entity_index"], s["sample_index"], s["record_time_ms"])
        for e in raw["entities"]
        for s in e["samples"]
    ]
    j.require(
        len(actual) == len(set(actual)) and sorted(actual) == sorted(expected),
        "Control evidence omits/repeats/changes selected source samples",
    )
    return ref


def verify(notices, metadata, evidence_ref):
    ref, package = checked_ref(evidence_ref)
    index_ref = evidence_index_reference()
    index = j.read(index_ref["path"])
    indexed = [r for r in index["runs"] if r["run_id"] == package.get("run_id")]
    j.require(
        len(indexed) == 1
        and indexed[0]["ordinal"] == package.get("ordinal")
        and indexed[0]["evidence"]["sha256"] == ref["sha256"],
        "Control package is absent from the reviewed immutable evidence index",
    )
    j.require(
        package.get("status") == "diagnostic_control_reflection_evidence"
        and package.get("schema_version") == 1,
        "Expected independently packaged control evidence",
    )
    raw_ref, raw = checked_ref(package["raw_controls"])
    request_ref, request = checked_ref(package["request"])
    fit_ref, fit = checked_ref(package["correlation"])
    cross_ref, cross = checked_ref(package["checkpoint_crosscheck"])
    family_ref, family = checked_ref(package["family_calibration"])
    tools = package["extraction_and_analysis"]
    for item in tools.values():
        j.evidence(item, Path("."))
    j.require(tools["analyzer"]["sha256"] == ANALYZER_SHA, "Unreviewed control analyzer version")
    j.require(
        family["verifier"]["sha256"] == EXACT_VERIFIER_SHA,
        "Family evidence must pin frozen exact-notice verifier",
    )
    j.evidence(family["verifier"], Path("."))
    for key in ("raw_notices", "correlation", "requests", "raw_samples", "crosschecker"):
        j.evidence(family[key], Path("."))
    for item in family["primary_sources"]:
        j.evidence(item, Path("."))
    for key in ("source_sha256", "ghost_login", "finish_time_ms"):
        j.require(
            raw[key] == notices[key] == metadata[key], "Control/raw-notice source identity differs: " + key
        )
    for key in ("record_start_ms", "record_end_ms"):
        j.require(raw[key] == notices[key], "Control record bounds differ")
    j.require(
        request["run_id"] == package["run_id"] == fit["run_id"] == cross["run_id"]
        and request["ordinal"] == raw["ordinal"] == package["ordinal"] == fit["ordinal"] == cross["ordinal"],
        "Control run identity differs",
    )
    meta_ref = j.pin(
        Path(request["actions"]["directory"]) / "metadata.json", request["actions"]["metadata_sha256"]
    )
    j.require(
        j.read(meta_ref["path"]) == metadata and fit["action_metadata"] == meta_ref,
        "Control evidence concerns different action metadata",
    )
    selection = notices["appearance"]["source_ghost_record_selection"]
    j.require(
        raw["selected_entity_indices"] == selection and len(selection) == 1,
        "Control evidence must use exact single original vehicle binding",
    )
    entities = {e["entity_index"]: e for e in notices["entities"]}
    for entity in raw["entities"]:
        j.require(
            entity["entity_index"] in selection
            and entity["class_id"] == "0x0A018000"
            and entities[entity["entity_index"]]["entity_class_id"] == "0x0A018000"
            and entity["SceneUId"] == entities[entity["entity_index"]]["scene_uid"],
            "Selected vehicle ownership differs",
        )
    visual = validate_sample_extent(raw, metadata, Path(meta_ref["path"]).parent)
    analyzer = module(tools["analyzer"])
    recomputed = json.loads(json.dumps(analyzer.inspect_run(raw, request, 1000)))
    j.require(
        recomputed == fit,
        "Control correlation differs when recomputed from all actual samples and held actions",
    )
    j.require(
        fit["status"] == "exact_control_reflection_correlated",
        "Control channels do not all match; no waiver permitted",
    )
    known = coverage.validate_metadata(metadata)["known_race_interval_ms"]
    j.require(
        fit["known_action_interval_ms"] == known, "Control correlation uses an incorrect known input interval"
    )
    intervals = fit["viable_control_reflection_offset_intervals_ms"]
    j.require(intervals in ([[-10, 0]], [[0, 10]]), "Unsupported or ambiguous full-run control interval")
    crosschecker = module(tools["crosschecker"])
    phase = json.loads(json.dumps(crosschecker.phase_offsets(raw, request, 1000)))
    j.require(phase == cross["phase_offset_search"], "Phase ambiguity differs from actual source controls")
    j.require(
        cross["exact_all_controls"] is True
        and cross["viable_control_reflection_offset_intervals_ms"] == intervals
        and cross["exact_checkpoint_series_verified"] is False,
        "Conditional evidence must preserve missing exact-notice corroboration",
    )
    j.require(
        family["records_checked"] == len(family["records"])
        and family["exact_checkpoint_series"] > 0
        and family["independently_crosschecked_sources"] > 0
        and family["exact_checkpoint_series"] + family["pending_sources_with_exact_controls"]
        == family["records_checked"],
        "Incomplete family cross-check evidence",
    )
    rows = [r for r in family["records"] if r["run_id"] == package["run_id"]]
    j.require(rows == [cross], "Per-source family cross-check differs from complete report")
    archives = [e["descriptor"]["StateArchiveVersion"] for e in fit["selected_entities"]]
    families = [
        f
        for f in family["families"]
        if f["state_archive_versions"] == archives and f["control_reflection_intervals_ms"] == intervals
    ]
    j.require(
        len(families) == 1 and families[0]["source_count"] > 0, "No independently observed calibration family"
    )
    observed = families[0]
    j.require(
        -10 <= observed["record_minus_exact_race_ms_min"] <= observed["record_minus_exact_race_ms_max"] <= 10,
        "Calibration-family notices exceed conditional assumption",
    )
    same_build = fit["game_build"] in observed["game_builds"]
    return dict(
        status="conditional_record_race_control_reflection_corroboration_v2",
        method="selected_vehicle_controls_with_archive_family_reference_v1",
        evidence_class="conditional_control_reflection",
        precision_bound_ms=None,
        conditional_precision_bound_ms=10,
        precision_bound_inclusive=None,
        nominal_mapping=dict(record_anchor_ms=0, race_anchor_ms=0, scale=1),
        nominal_identity_clock_consistent=True,
        checkpoints=[],
        finish_corroborated=False,
        corroborated_checkpoint_ordinals=[],
        full_race_uniform_error_guarantee=False,
        exact_subtick_offset_verified=False,
        video_alignment_verified=False,
        capture_order_or_latched_input_delay_disambiguated=False,
        strict_action_aligned_training_eligible=False,
        control_evidence=ref,
        control_evidence_index=index_ref,
        control_validator=j.pin(__file__),
        selected_source_samples=visual,
        selected_vehicle_entity=selection[0],
        state_archive_versions=archives,
        control_reflection_offset_intervals_ms=intervals,
        control_offset_upper_bounds_exclusive=True,
        full_extent_control_checks=fit["full_extent_candidate_checks"],
        control_phase_checks=fit["three_phase_checks"],
        phase_offset_search=phase,
        family_calibration=family_ref,
        family_reference=observed,
        same_game_build_reference_available=same_build,
        assumptions=[
            "Nominal record_ms=race_ms is an experimental inference from the same state archive and exact reflected controls.",
            "The assumed10ms source bound transfers archive-family checkpoint observations; it is not a measured physical-state error bound for this source.",
            "Engine capture order or latched-input delay is not independently identified; reflected controls must not silently rebase positions.",
            "Distinct phase-search ambiguities remain visible; a unique full-run control fit does not prove a uniform error bound.",
            "No exact finish-clock notice or fabricated third anchor is asserted.",
        ],
        limitations=family["limitations"],
        interpretation="All observed selected-vehicle steer/gas/brake bytes exactly match original held controls under the stated interval. This supports conditional alignment for experimental use. Physical race-clock phase is not directly verified, so precision_bound_ms is null and strict action-aligned training remains ineligible.",
    )
