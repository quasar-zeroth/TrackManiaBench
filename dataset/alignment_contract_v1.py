"""Explicit separation between direct checkpoint timing and conditional controls."""

from fractions import Fraction

CONDITIONAL = "conditional_control_reflection"
DIRECT = "direct_checkpoint_notices"


def require(ok, message):
    if not ok:
        raise ValueError(message)


def conditional(value):
    return value.get("evidence_class") == CONDITIONAL


def source_bound(proof):
    if conditional(proof):
        require(
            proof["precision_bound_ms"] is None and proof["full_race_uniform_error_guarantee"] is False,
            "Conditional source cannot claim a uniform bound",
        )
        return Fraction(str(proof["conditional_precision_bound_ms"]))
    require(proof.get("evidence_class") == DIRECT, "Unknown source timing class")
    return Fraction(str(proof["precision_bound_ms"]))


def source_contract(proof, reference):
    keys = (
        "evidence_class",
        "precision_bound_ms",
        "conditional_precision_bound_ms",
        "nominal_mapping",
        "full_race_uniform_error_guarantee",
        "finish_corroborated",
        "interpretation",
        "assumptions",
        "scope",
        "limitations",
        "same_game_build_reference_available",
        "control_reflection_offset_intervals_ms",
        "control_offset_upper_bounds_exclusive",
        "control_phase_checks",
        "phase_offset_search",
        "capture_order_or_latched_input_delay_disambiguated",
    )
    result = {key: proof[key] for key in keys if key in proof}
    result["strict_action_aligned_training_eligible"] = not conditional(proof)
    result["source_proof"] = reference
    return result


def mapping_bound(mapping, is_conditional):
    if is_conditional:
        require(
            mapping.get("max_absolute_error_ms") is None,
            "Conditional alignment must not declare an unconditional error bound",
        )
        value = mapping["conditional_max_absolute_error_ms"]
    else:
        require(
            mapping.get("conditional_max_absolute_error_ms") is None,
            "Direct mapping has conflicting conditional bound",
        )
        value = mapping["max_absolute_error_ms"]
    result = Fraction(str(value))
    require(result >= 0, "Negative timing error")
    return result


def verify_nominal(proof, record_anchor, race_anchor, scale):
    nominal = proof["nominal_mapping"]
    require(
        (record_anchor, race_anchor, scale)
        == tuple(Fraction(str(nominal[key])) for key in ("record_anchor_ms", "race_anchor_ms", "scale")),
        "Conditional mapping must equal the independently checked nominal source mapping",
    )
