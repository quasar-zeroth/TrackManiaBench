#!/usr/bin/env python3
"""Explicit admission dispatch for measured nonzero native RecordData starts.

The native export profile and historical pilot validator are unchanged. Every
ordinary result delegates to the original validator. This module never changes
the original module's globals or rewrites an old evidence file.
"""

import importlib.util
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


frozen = module("partial_dispatch_frozen_renderer", HERE / "render_queue.py")
ADMISSION_BUILD = "native-record-start-admission-1"
coverage_policy = module("partial_renderer_source_policy", HERE.parent / "dataset/coverage_policy_v1.py")
VARIANT_ORDINALS = tuple(sorted(case["ordinal"] for case in coverage_policy.RECORD_CASES.values()))


def __getattr__(name):
    # Public helpers retain their original code and globals, especially the
    # five-pilot gate. Only validate_render_result has an explicit new route.
    return getattr(frozen, name)


def exception_fact(run):
    return coverage_policy.record_case_for(run)


def validate_render_result(result, manifest, run, result_dir):
    fact = exception_fact(run)
    if fact is None:
        if result.get("source_admission") is not None:
            raise ValueError("Ordinary source cannot claim exceptional admission")
        return frozen.validate_render_result(result, manifest, run, result_dir)
    backend = result.get("renderer_backend", "openplanet")
    if backend != "native_ui":
        raise ValueError("Source-prefix admission only applies to native UI exports")
    expected = frozen.backend_identity(backend)
    if result.get("status") != "native_video_verified" or result.get(
        "settings"
    ) != frozen.settings_for_backend(backend):
        raise ValueError("Render result is incomplete or settings differ")
    if any(result.get(k) != v for k, v in expected.items()):
        raise ValueError("Native UI renderer identity differs")
    if any(result.get("map", {}).get(k) != manifest["map"][k] for k in ("uid", "sha256")):
        raise ValueError("Rendered map identity differs")
    for key in ("run_id", "ordinal", "account_id", "record_time_ms", "ghost_sha256"):
        if result.get("run", {}).get(key) != run[key]:
            raise ValueError("Rendered exceptional run identity differs: " + key)
    if result.get("source_key") != frozen.source_key(manifest, result["run"], backend):
        raise ValueError("Render source identity differs")
    video = Path(result_dir) / "video.webm"
    if not video.is_file() or frozen.sha256(video) != result.get("video", {}).get("sha256"):
        raise ValueError("Rendered video hash differs")
    validator = module("partial_native_evidence_validator", HERE / "native_ui_queue_partial_v1.py")
    if result.get("validator_sha256") != frozen.sha256(Path(validator.__file__)):
        raise ValueError("Exceptional result admission validator changed")
    return validator.validate_record_evidence(result, manifest, run, Path(result_dir))


def validation_modules():
    """Load the explicit versioned tools, without changing frozen globals."""
    previous = list(sys.path)
    try:
        sys.path[:0] = [str(HERE), str(HERE.parent / "dataset")]
        audit = module("source_variant_gate_audit", HERE / "production_video_audit_partial_v1.py")
        joiner = module("source_variant_gate_join", HERE.parent / "dataset/join_dataset_partial_v1.py")
    finally:
        sys.path[:] = previous
    if Path(joiner.coverage_policy.__file__).resolve() != Path(coverage_policy.__file__).resolve():
        raise ValueError("Variant join loaded another coverage policy")
    return audit, joiner


def validate_variant_gate(path, manifest, accepted, calibrations):
    """Revalidate all14 real exports and reconstruct their saved joins.

    Intended once before admitting the remaining queue. Subsequent GUI phases
    pin this gate, tools and referenced files; they need not repeat all14 decodes.
    """
    path = Path(path).resolve(strict=True)
    base = path.parent
    accepted = Path(accepted).resolve(strict=True)
    calibrations = Path(calibrations).resolve(strict=True)
    gate = json.loads(path.read_text())

    def require(ok, message):
        if not ok:
            raise ValueError(message)

    require(
        gate.get("schema_version") == 1 and gate.get("status") == "actual_source_variants_verified",
        "Actual source variant gate is incomplete",
    )
    require(gate.get("admission_build") == ADMISSION_BUILD, "Variant admission build differs")
    audit, j = validation_modules()
    require(gate.get("admission_tools") == audit.ui.admission_tools(), "Variant gate admission tools changed")
    require(
        frozen.gate_file(gate["audit_pipeline"], base) == Path(audit.__file__).resolve(),
        "Variant audit pipeline differs",
    )
    require(frozen.gate_file(gate["joiner"], base) == Path(j.__file__).resolve(), "Variant joiner differs")
    require(
        frozen.gate_file(gate["validator"], base) == Path(__file__).resolve(),
        "Variant gate validator differs",
    )
    actions_path = frozen.gate_file(gate["actions_manifest"], base)
    actions = j.read(actions_path)
    require(
        actions.get("status") == "actions_verified"
        and 1 <= actions.get("requested_count", 0) <= 250
        and len(actions.get("runs", [])) == actions.get("requested_count")
        and not actions.get("failures")
        and not actions.get("source_collection_failures"),
        "Variant gate needs the complete frozen action cohort",
    )
    for key in ("uid", "sha256"):
        require(actions["map"][key] == manifest["map"][key], "Variant map differs")
    pilot = frozen.gate_file(gate["pilot_gate"], base)
    frozen.validate_gate(pilot, actions, accepted)
    rows = gate.get("runs", [])
    require(
        [r.get("ordinal") for r in rows] == list(VARIANT_ORDINALS),
        "Variant gate needs all configured source variant ordinals",
    )
    by_ordinal = {r["ordinal"]: r for r in manifest["runs"]}
    actions_by_ordinal = {r["ordinal"]: r for r in actions["runs"]}
    for row in rows:
        ordinal = row["ordinal"]
        run = actions_by_ordinal[ordinal]
        source = by_ordinal[ordinal]
        rid = run["run_id"]
        for key in ("run_id", "ordinal", "account_id", "ghost_sha256", "record_time_ms"):
            require(
                run[key] == source[key] and row["run"][key] == run[key],
                "Variant source identity differs: " + key,
            )
        require(exception_fact(run) is not None, "Variant is outside the measured registry")
        result = frozen.gate_file(row["render_result"], base)
        require(
            result == (accepted / rid / "result.json").resolve(),
            "Variant result is outside canonical render output",
        )
        require(
            frozen.gate_file(row["video"], base) == (accepted / rid / "video.webm").resolve(),
            "Variant video differs",
        )
        require(
            frozen.gate_file(row["calibration"], base) == (calibrations / (rid + ".json")).resolve(),
            "Variant calibration is outside its canonical directory",
        )
        review_path = frozen.gate_file(row["explicit_review"], base)
        review, *_ = audit.validate_review(review_path, accepted)
        require(
            review["run_id"] == rid and review["ordinal"] == ordinal, "Variant visual review identity differs"
        )
        for key, filename in (
            ("pulses", "pulse-verification.json"),
            ("visibility", "observation-window-verification.json"),
            ("diagnostic_export_evidence", "diagnostic-export-evidence.json"),
        ):
            require(
                frozen.gate_file(row[key], base) == (review_path.parent / filename).resolve(),
                "Variant review report belongs to another acceptance: " + key,
            )
        require(
            frozen.gate_file(row["source_clock"], base)
            == frozen.gate_file(review["source_clock_proof"], review_path.parent),
            "Variant source clock evidence differs",
        )
        frames, summary = j.prepare_run(actions, run, accepted, calibrations)
        frame_path = frozen.gate_file(row["frame_actions"], base)
        summary_path = frozen.gate_file(row["joined_summary"], base)
        require(
            frame_path.read_bytes() == b"".join(j.encoded(frame) for frame in frames),
            "Variant frame/action reconstruction changed",
        )
        summary["frame_actions"] = j.pin(frame_path)
        require(summary_path.read_bytes() == j.encoded(summary), "Variant immutable join summary changed")
    return gate
