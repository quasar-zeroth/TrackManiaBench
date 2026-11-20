"""Distinguish historical image review from reproducible automatic visibility QA."""

import json
from pathlib import Path
import sys

MODE = "deterministic_decoded_image_policy_v1"


def verify(window, expected, base, evidence, *, calibrated=False):
    """Verify either original review evidence or the new machine policy evidence.

    Machine mode never inherits a historical human/agent-reviewed boolean. Its
    policy validator rechecks the source pins and reconstructs the QA decision.
    """

    def require(ok, message):
        if not ok:
            raise ValueError(message)

    require(
        window.get("method") and window.get("evidence_files"), "Visibility verification lacks method/evidence"
    )
    automatic = window.get("review_mode") == MODE
    if not automatic:
        require(
            window.get("review_mode") in (None, "automated_scan_plus_sampled_visual_review"),
            "Unknown visibility verification mode",
        )
        require(
            window.get("reviewed_contiguous_race_visibility") is True,
            "Historical visibility lacks actual review",
        )
        require(
            not any(
                key in window
                for key in (
                    "automatic_contiguous_race_visibility_verified",
                    "automatic_review",
                    "manual_review_performed",
                )
            ),
            "Automatic visibility must not downgrade to historical review",
        )
        return "historical_sampled_and_flagged_image_review"
    require(
        window.get("automatic_contiguous_race_visibility_verified") is True,
        "Automatic visibility checks have not passed",
    )
    require(
        window.get("manual_review_performed") is False
        and window.get("reviewed_contiguous_race_visibility") is not True,
        "Automatic evidence must not claim manual image review",
    )
    ref = evidence(window.get("automatic_review"), base)
    document = json.loads(Path(ref["path"]).read_text())
    require(
        document.get("schema_version") == 2 and document.get("status") == "automatic_checks_passed",
        "Automatic review was not accepted by the versioned policy",
    )
    require(document.get("run_id") == expected["run_id"], "Automatic review belongs to another run")
    require(
        document.get("manual_review_performed") is False and document.get("unresolved_issues") == [],
        "Automatic review has unresolved issues or claims manual review",
    )
    renderer = str(Path(__file__).resolve().parents[1] / "renderer")
    if renderer not in sys.path:
        sys.path.insert(0, renderer)
    import automatic_video_policy_v1 as policy

    policy.verify_record(Path(ref["path"]))
    if calibrated:
        require(
            isinstance(window.get("measured_visibility_intersection"), dict),
            "Automatic calibration must preserve its raw visibility intersection evidence",
        )
    else:
        require(
            "measured_visibility_intersection" not in window,
            "Raw automatic visibility must not impersonate a calibrated window",
        )
        # This is the raw accepted visibility report. A calibrated window may
        # trim it to the source race, and the joiner separately reconstructs
        # that intersection using this exact source report.
        for key in (
            "first_usable_frame_index",
            "last_usable_frame_index",
            "first_usable_video_pts_ms",
            "last_usable_video_pts_ms",
        ):
            require(
                window.get(key) == document.get("visibility", {}).get(key),
                "Automatic visibility bounds differ from policy evidence: " + key,
            )
        native = evidence(window.get("native_video"), base)
        require(
            native == evidence(document.get("native_video"), Path(ref["path"]).parent),
            "Automatic visibility belongs to another native video",
        )
        accepted = evidence(window.get("accepted_video"), base)
        result = evidence(window.get("accepted_result"), base)
        require(
            accepted["sha256"] == expected["video_sha256"]
            and result["sha256"] == expected["render_result_sha256"],
            "Automatic visibility belongs to another accepted video/result",
        )
        decode_ref = evidence(window.get("native_accepted_decode_equality"), base)
        decode = json.loads(Path(decode_ref["path"]).read_text())
        decode_base = Path(decode_ref["path"]).parent
        require(
            decode.get("passed") is True
            and decode.get("all_decoded_frame_hashes_pts_dts_duration_size_equal") is True,
            "Automatic visibility lacks verified decoded-copy equality",
        )
        require(
            evidence(decode.get("native_video"), decode_base) == native
            and evidence(decode.get("accepted_video"), decode_base) == accepted,
            "Decode equality belongs to another native/accepted video",
        )
        frame_refs = [evidence(item, decode_base) for item in decode.get("evidence_files", [])]
        require(
            len(frame_refs) == 2
            and Path(frame_refs[0]["path"]).read_bytes() == Path(frame_refs[1]["path"]).read_bytes(),
            "Decoded frame fingerprints differ or are missing",
        )
    return MODE


def copy_attestation(source):
    """Copy only the attestation branch actually present in the source report."""
    if source.get("review_mode") == MODE:
        return {
            key: source[key]
            for key in (
                "review_mode",
                "automatic_contiguous_race_visibility_verified",
                "manual_review_performed",
                "automatic_review",
            )
        }
    return {"reviewed_contiguous_race_visibility": source["reviewed_contiguous_race_visibility"]}
