"""Conditional masks retain data without claiming a physical-state time guarantee."""

import copy
from fractions import Fraction
import unittest
from pathlib import Path
import alignment_contract_v1 as contract
import build_measured_calibration_partial_v1 as builder
import join_dataset_partial_v1 as j


class ConditionalAlignmentTests(unittest.TestCase):
    def proof(self):
        return {
            "evidence_class": contract.CONDITIONAL,
            "precision_bound_ms": None,
            "conditional_precision_bound_ms": 10,
            "full_race_uniform_error_guarantee": False,
            "nominal_mapping": {"record_anchor_ms": 0, "race_anchor_ms": 0, "scale": 1},
            "checkpoints": [],
        }

    def test_empty_checkpoint_list_cannot_admit_arbitrary_transform(self):
        for anchors in ((0, 10, 1), (10, 0, 1), (0, 0, 2), (10, 10, 1)):
            with self.assertRaises(ValueError):
                builder.validate_record_link(self.proof(), *map(Fraction, anchors), Fraction(10))
        builder.validate_record_link(self.proof(), Fraction(0), Fraction(0), Fraction(1), Fraction(10))

    def test_conditional_source_rejects_unconditional_or_tighter_bound(self):
        proof = self.proof()
        proof["precision_bound_ms"] = 10
        with self.assertRaises(ValueError):
            contract.source_bound(proof)
        with self.assertRaises(ValueError):
            builder.validate_record_link(self.proof(), Fraction(0), Fraction(0), Fraction(1), Fraction(9))
        with self.assertRaises(ValueError):
            contract.mapping_bound(
                {"max_absolute_error_ms": 30, "conditional_max_absolute_error_ms": 30}, True
            )
        self.assertEqual(
            contract.mapping_bound(
                {"max_absolute_error_ms": None, "conditional_max_absolute_error_ms": 30}, True
            ),
            30,
        )


if __name__ == "__main__":
    unittest.main()
