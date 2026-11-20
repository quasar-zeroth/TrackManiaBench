"""Portable cohort configuration and fail-closed evidence regression tests."""

import copy
from fractions import Fraction
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import assemble_actual_pilot_gate as gate
import calibrate_production_automatic_v2 as calibrator
import coverage_policy_v1 as coverage
import join_dataset_automatic_v2 as joiner
import splits
from test_automatic_visibility_contract_v2 import held_rows


class CustomizationTests(unittest.TestCase):
    def test_arbitrary_cohort_sizes_are_deterministic_player_splits(self):
        for size in (6, 17, 100, 250, 301):
            rows = [dict(run_id=str(i), account_id="player-" + str(i)) for i in range(size)]
            actual = splits.split_runs(rows, "custom-seed", "0.7,0.2,0.1")
            self.assertEqual(actual, splits.split_runs(list(reversed(rows)), "custom-seed", "0.7,0.2,0.1"))
            self.assertEqual(
                {key: sum(v == key for v in actual.values()) for key in ("train", "validation", "test")},
                splits.allocation(size, "0.7,0.2,0.1"),
            )
        self.assertEqual(splits.allocation(250), dict(train=200, validation=25, test=25))

    def test_bad_split_inputs_rejected(self):
        for value in ("0.8,0.1", "0.8,0.1,0.2", "-0.1,0.1,1", "nan,0,1"):
            with self.subTest(value=value), self.assertRaises((ValueError, ZeroDivisionError)):
                splits.allocation(17, value)
        with self.assertRaises(ValueError):
            splits.split_runs(
                [{"run_id": "a", "account_id": "x"}, {"run_id": "b", "account_id": "x"}], "seed"
            )

    def test_production_calibration_accepts_complete_seventeen_not_missing_player(self):
        runs = [
            dict(run_id=str(i), ordinal=i, account_id="p" + str(i), ghost_sha256=str(i)) for i in range(1, 18)
        ]
        actions = dict(
            status="actions_verified",
            requested_count=17,
            runs=runs,
            failures=[],
            source_collection_failures=[],
        )
        prepared = dict(
            status="native_cohort_prepared_import_pending",
            requested_count=17,
            runs=[{"run": r} for r in runs],
        )
        self.assertEqual(len(calibrator.validate_cohort(actions, prepared)[0]), 17)
        prepared["runs"].pop()
        with self.assertRaises(ValueError):
            calibrator.validate_cohort(actions, prepared)

    def test_unknown_prefix_keeps_no_control_rows_and_guarded_conditional_masks(self):
        rows = [r for r in held_rows(end=1000) if r["timestamp_ms"] >= 10]
        cov = dict(
            policy=coverage.POLICY,
            known_race_interval_ms=[10, 1000],
            missing_race_intervals_ms=[[0, 10]],
            fully_covered=False,
            extrapolated=False,
        )
        frames = joiner.join_frames(
            [Fraction(t) for t in range(0, 1420, 20)],
            Fraction(1420),
            lambda p: p - 200,
            rows,
            [],
            1000,
            dict(first_usable_frame_index=11, last_usable_frame_index=59),
            cov,
            30,
            True,
        )
        self.assertEqual(sum(b - a for f in frames for a, b in f["missing_action_intervals_ms"]), 10)
        self.assertEqual(sum(p["duration_ms"] for f in frames for p in f["action_substeps"]), 990)
        self.assertTrue(
            all(
                not f["training_eligible"] and f["time_alignment_max_absolute_error_ms"] is None
                for f in frames
            )
        )
        self.assertEqual(next(f["race_time_ms"] for f in frames if f["experimental_training_eligible"]), 40)
        self.assertTrue(all(f["race_time_ms"] <= 970 for f in frames if f["experimental_training_eligible"]))

    def test_unreviewed_record_variants_fail_closed_and_policy_mutation_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            policy = Path(name) / "reviewed.json"
            run = dict(
                run_id="synthetic",
                ordinal=1,
                account_id="account",
                ghost_sha256="a" * 64,
                record_time_ms=1000,
            )
            case = {k: v for k, v in run.items() if k != "run_id"}
            case.update(record_start_ms=10, record_end_ms=1000)
            policy.write_text(
                json.dumps(dict(schema_version=1, map_uid="Synthetic", record_cases={"synthetic": case}))
            )
            try:
                with patch.dict("os.environ", {"TRACKMANIABENCH_COVERAGE_POLICY": str(policy)}):
                    importlib.reload(coverage)
                    self.assertEqual(coverage.record_case_for(run)["record_start_ms"], 10)
                    altered = {**run, "ghost_sha256": "b" * 64}
                    with self.assertRaises(ValueError):
                        coverage.record_case_for(altered)
                    self.assertIsNone(coverage.record_case_for({**run, "run_id": "other"}))
                    policy.write_text("{}")
                    with self.assertRaises(ValueError):
                        coverage.policy_reference()
            finally:
                importlib.reload(coverage)

    def test_gate_does_not_upgrade_unpassed_or_unpinned_review(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            evidence = root / "evidence"
            evidence.write_text("synthetic test evidence only")
            ref = joiner.pin(evidence)
            review = dict(
                schema_version=1,
                passed=True,
                verified_by="synthetic",
                verified_utc="test",
                evidence_files=[ref],
                checks={"check": dict(passed=True, evidence="synthetic explanation", evidence_files=[ref])},
            )
            gate.review_checks(review, root, ["check"])
            for mutate in ("unpassed", "unpinned", "tampered"):
                changed = copy.deepcopy(review)
                if mutate == "unpassed":
                    changed["passed"] = False
                if mutate == "unpinned":
                    changed["checks"]["check"]["evidence_files"] = []
                if mutate == "tampered":
                    evidence.write_text("changed")
                with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                    gate.review_checks(changed, root, ["check"])


if __name__ == "__main__":
    unittest.main()
