"""Independent, lightweight checks for automatic visibility and unchanged masks.

Synthetic records and policy mocks exercise the attestation boundary only. They
are never production visual evidence. No test decodes video or touches the rendering host.
"""

import copy
from fractions import Fraction as F
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import calibrate_production_automatic_v2 as supervisor
import join_dataset_automatic_v2 as automatic
import join_dataset_partial_v1 as frozen
import visibility_contract_automatic_v2 as contract

ROOT = Path(__file__).resolve().parents[1]


class AutomaticVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.review = self.base / "synthetic-automatic-review.json"
        self.document = dict(
            schema_version=2,
            status="automatic_checks_passed",
            run_id="synthetic-run",
            manual_review_performed=False,
            unresolved_issues=[],
        )
        self.bounds = dict(
            first_usable_frame_index=11,
            last_usable_frame_index=64,
            first_usable_video_pts_ms=220,
            last_usable_video_pts_ms=1280,
        )
        self.document["visibility"] = self.bounds.copy()
        self.window = dict(
            method="synthetic contract test only",
            evidence_files=[{"synthetic": True}],
            review_mode=contract.MODE,
            automatic_contiguous_race_visibility_verified=True,
            manual_review_performed=False,
            reviewed_contiguous_race_visibility=False,
        )
        self.window.update(self.bounds)
        self.native = self.write_pin("native.mock", b"synthetic native bytes; not a video")
        self.accepted = self.write_pin("accepted.mock", b"synthetic remux bytes; not a video")
        self.result = self.write_pin("result.mock", b"synthetic render result")
        self.fingerprint_one = self.write_pin("native.framemd5", b"synthetic frame fingerprints")
        self.fingerprint_two = self.write_pin("accepted.framemd5", b"synthetic frame fingerprints")
        self.document["native_video"] = self.native
        self.window.update(
            native_video=self.native, accepted_video=self.accepted, accepted_result=self.result
        )
        self.decode = dict(
            passed=True,
            all_decoded_frame_hashes_pts_dts_duration_size_equal=True,
            native_video=self.native,
            accepted_video=self.accepted,
            evidence_files=[self.fingerprint_one, self.fingerprint_two],
        )
        self.expected = dict(
            run_id="synthetic-run",
            video_sha256=self.accepted["sha256"],
            render_result_sha256=self.result["sha256"],
        )
        self.policy = SimpleNamespace(verify_record=mock.Mock())
        self.patch = mock.patch.dict(sys.modules, automatic_video_policy_v1=self.policy)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def write_pin(self, name, data):
        path = self.base / name
        path.write_bytes(data)
        return automatic.pin(path)

    def verify(self):
        self.window["native_accepted_decode_equality"] = self.write_pin(
            "decode.json", automatic.encoded(self.decode)
        )
        self.review.write_text(json.dumps(self.document))
        self.window["automatic_review"] = automatic.pin(self.review)
        self.policy.verify_record.return_value = self.document
        return contract.verify(self.window, self.expected, self.base, automatic.evidence)

    def test_v2_dispatches_only_to_explicit_v2_verifier(self):
        second = SimpleNamespace(verify_record=mock.Mock())
        self.document["policy_version"] = "2"
        self.window["policy_version"] = "2"
        with mock.patch.dict(sys.modules, automatic_video_policy_v2=second):
            self.assertEqual(self.verify(), contract.MODE)
        second.verify_record.assert_called_once_with(self.review)
        self.policy.verify_record.assert_not_called()
        self.assertEqual(contract.copy_attestation(self.window)["policy_version"], "2")

    def test_unknown_or_nonstring_policy_version_rejects(self):
        for value in ("3", "../../module", 2, True, None):
            with self.subTest(version=value):
                self.document["policy_version"] = value
                self.window["policy_version"] = value
                with self.assertRaisesRegex(ValueError, "Unknown automatic policy version"):
                    self.verify()
        self.policy.verify_record.assert_not_called()

    def test_window_cannot_hide_or_change_policy_version(self):
        self.document["policy_version"] = "2"
        with self.assertRaisesRegex(ValueError, "Window policy version differs"):
            self.verify()
        self.document.pop("policy_version")
        self.window["policy_version"] = "2"
        with self.assertRaisesRegex(ValueError, "Window policy version differs"):
            self.verify()

    def test_machine_acceptance_explicitly_calls_policy(self):
        self.assertEqual(self.verify(), contract.MODE)
        self.policy.verify_record.assert_called_once_with(self.review)

    def test_policy_reconstruction_failure_is_not_ignored(self):
        self.policy.verify_record.side_effect = ValueError("synthetic changed source pin")
        with self.assertRaisesRegex(ValueError, "changed source pin"):
            self.verify()

    def test_unaccepted_status_schema_and_other_run_are_rejected(self):
        for key, value in [
            ("status", "prepared"),
            ("status", "automatic_checks_failed"),
            ("schema_version", 1),
            ("run_id", "another-run"),
        ]:
            with self.subTest(key=key, value=value):
                old = self.document[key]
                self.document[key] = value
                with self.assertRaises(ValueError):
                    self.verify()
                self.document[key] = old

    def test_document_manual_claim_and_unresolved_issues_rejected(self):
        for key, value in [
            ("manual_review_performed", True),
            ("manual_review_performed", None),
            ("unresolved_issues", ["unreadable clock"]),
        ]:
            with self.subTest(key=key, value=value):
                old = self.document[key]
                self.document[key] = value
                with self.assertRaises(ValueError):
                    self.verify()
                self.document[key] = old

    def test_window_cannot_claim_manual_review_or_unpassed_machine_checks(self):
        for key, value in [
            ("manual_review_performed", True),
            ("reviewed_contiguous_race_visibility", True),
            ("automatic_contiguous_race_visibility_verified", False),
        ]:
            with self.subTest(key=key):
                old = self.window[key]
                self.window[key] = value
                with self.assertRaises(ValueError):
                    self.verify()
                self.window[key] = old

    def test_hash_changed_after_pinning_is_rejected(self):
        self.review.write_text(json.dumps(self.document))
        self.window["automatic_review"] = automatic.pin(self.review)
        self.review.write_text(json.dumps({**self.document, "run_id": "changed"}))
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            contract.verify(self.window, self.expected, self.base, automatic.evidence)

    def test_historical_evidence_remains_distinct(self):
        for mode in (None, "automated_scan_plus_sampled_visual_review"):
            window = dict(
                method="synthetic historical review",
                evidence_files=[{"synthetic": True}],
                reviewed_contiguous_race_visibility=True,
            )
            if mode is not None:
                window["review_mode"] = mode
            self.assertEqual(
                contract.verify(window, self.expected, self.base, automatic.evidence),
                "historical_sampled_and_flagged_image_review",
            )
        self.policy.verify_record.assert_not_called()

    def test_unknown_review_mode_cannot_be_downgraded_to_manual(self):
        window = dict(
            method="synthetic",
            evidence_files=[{"synthetic": True}],
            review_mode="tampered-automatic-mode",
            reviewed_contiguous_race_visibility=True,
        )
        with self.assertRaises(ValueError):
            contract.verify(window, self.expected, self.base, automatic.evidence)

    def test_machine_fields_cannot_masquerade_as_historical_review(self):
        for key, value in [
            ("automatic_review", {}),
            ("automatic_contiguous_race_visibility_verified", False),
            ("manual_review_performed", False),
        ]:
            with self.subTest(key=key):
                window = dict(
                    method="synthetic",
                    evidence_files=[{"synthetic": True}],
                    reviewed_contiguous_race_visibility=True,
                    **{key: value},
                )
                with self.assertRaises(ValueError):
                    contract.verify(window, self.expected, self.base, automatic.evidence)

    def test_copy_machine_attestation_does_not_copy_historical_boolean(self):
        self.verify()
        copied = contract.copy_attestation(self.window)
        self.assertNotIn("reviewed_contiguous_race_visibility", copied)
        self.assertIs(copied["manual_review_performed"], False)

    def test_raw_bounds_must_match_policy_record(self):
        for key in self.bounds:
            with self.subTest(key=key):
                self.window[key] += 1
                with self.assertRaisesRegex(ValueError, "bounds differ"):
                    self.verify()
                self.window[key] -= 1

    def test_same_run_other_native_video_cannot_attest_window(self):
        self.document["native_video"] = self.write_pin(
            "different-native.mock", b"another native render of same run"
        )
        with self.assertRaisesRegex(ValueError, "another native video"):
            self.verify()

    def test_other_accepted_video_or_result_is_rejected(self):
        for key, expected_key in [
            ("accepted_video", "video_sha256"),
            ("accepted_result", "render_result_sha256"),
        ]:
            with self.subTest(key=key):
                before = self.expected[expected_key]
                self.expected[expected_key] = "0" * 64
                with self.assertRaisesRegex(ValueError, "another accepted video/result"):
                    self.verify()
                self.expected[expected_key] = before

    def test_decode_equality_must_pass_and_pin_correct_videos(self):
        variants = [
            ("passed", False),
            ("all_decoded_frame_hashes_pts_dts_duration_size_equal", False),
            ("native_video", self.accepted),
            ("accepted_video", self.native),
            ("evidence_files", []),
        ]
        for key, value in variants:
            with self.subTest(key=key):
                before = self.decode[key]
                self.decode[key] = value
                with self.assertRaises(ValueError):
                    self.verify()
                self.decode[key] = before

    def test_different_decoded_frames_rejected_even_with_passed_flags(self):
        self.decode["evidence_files"][1] = self.write_pin(
            "different.framemd5", b"different frame fingerprints"
        )
        with self.assertRaisesRegex(ValueError, "fingerprints differ"):
            self.verify()

    def test_raw_window_cannot_skip_binding_by_injecting_intersection_field(self):
        self.window["first_usable_frame_index"] += 1
        self.window["measured_visibility_intersection"] = {}
        with self.assertRaises(ValueError):
            self.verify()

    def calibrated_fixture(self):
        """A raw visible tail extends beyond finish; calibration must trim it."""
        import test_join_dataset as prior_tests

        self.verify()
        report = self.write_pin("raw-window.json", automatic.encoded(self.window))
        cal, expected, pts, scene = prior_tests.JoinTests().native_calibration(self.base)
        expected.update(self.expected)
        cal.update(expected)
        cal["observation_window"] = dict(
            first_usable_frame_index=11,
            last_usable_frame_index=59,
            first_usable_race_ms=20,
            last_usable_race_ms=980,
            method="synthetic machine evidence",
            evidence_files=[report],
            **contract.copy_attestation(self.window),
            measured_visibility_intersection=dict(
                policy="measured_visible_frames_intersect_original_race_v1",
                raw_first_visible_frame_index=11,
                raw_last_visible_frame_index=64,
                raw_first_visible_race_ms=20,
                raw_last_visible_race_ms=1080,
                removed_before_race_frames=0,
                removed_after_race_frames=5,
                race_interval_ms=[0, 1000],
                action_coverage_mask_applied=False,
                source_report=report,
            ),
        )
        return cal, expected, pts, scene

    def test_calibration_exactly_trims_raw_postfinish_tail(self):
        cal, expected, pts, scene = self.calibrated_fixture()
        _, audit = automatic.validate_calibration(cal, expected, pts, F(1420), self.base, scene)
        self.assertEqual(audit["observation_window"]["last_usable_frame_index"], 59)
        self.assertEqual(audit["observation_window"]["visibility_verification_mode"], contract.MODE)

    def test_calibrated_bounds_must_be_exact_intersection_not_arbitrary_trim(self):
        cal, expected, pts, scene = self.calibrated_fixture()
        cal["observation_window"]["last_usable_frame_index"] = 58
        cal["observation_window"]["last_usable_race_ms"] = 960
        with self.assertRaisesRegex(ValueError, "not the measured visible/race intersection"):
            automatic.validate_calibration(cal, expected, pts, F(1420), self.base, scene)

    def test_join_rechecks_raw_report_against_machine_policy_bounds(self):
        cal, expected, pts, scene = self.calibrated_fixture()
        changed = copy.deepcopy(self.window)
        changed["last_usable_frame_index"] = 63
        changed["last_usable_video_pts_ms"] = 1260
        report = self.write_pin("changed-raw-window.json", automatic.encoded(changed))
        cal["observation_window"]["measured_visibility_intersection"]["source_report"] = report
        with self.assertRaisesRegex(ValueError, "bounds differ"):
            automatic.validate_calibration(cal, expected, pts, F(1420), self.base, scene)


def held_rows(start=0, end=1000):
    return [
        dict(
            store_index=0,
            tick=i,
            timestamp_ms=t,
            valid_until_ms=t + 10,
            input_kind=2,
            steer_raw=0,
            steer_normalized=0.0,
            throttle=1,
            brake=0,
            horn=0,
            free_look=0,
            jump=0,
            action_slot_mask=0,
            respawn=0,
            secondary_respawn=0,
        )
        for i, t in enumerate(range(start, end, 10))
    ]


class AutomaticMaskTests(unittest.TestCase):
    def frames(self, finish=1000, last=59, conditional=False):
        return automatic.join_frames(
            [F(t) for t in range(0, 1420, 20)],
            F(1420),
            lambda p: p - 200,
            held_rows(end=1000),
            [],
            finish,
            dict(first_usable_frame_index=11, last_usable_frame_index=last),
            alignment_error_ms=30,
            conditional_alignment=conditional,
        )

    def test_postfinish_never_training_even_if_visibility_window_is_wide(self):
        for conditional in (False, True):
            frames = self.frames(last=70, conditional=conditional)
            for frame in frames:
                if frame["race_time_ms"] >= 1000:
                    self.assertFalse(frame["training_eligible"])
                    self.assertFalse(frame["transition_eligible"])
                    self.assertFalse(frame.get("experimental_training_eligible", False))
                    self.assertFalse(frame.get("experimental_transition_eligible", False))
                    self.assertFalse(frame.get("video_training_eligible", False))

    def test_transition_target_at_finish_or_outside_visibility_is_excluded(self):
        for finish, last in [(1000, 59), (997, 59), (1000, 58)]:
            with self.subTest(finish=finish, last=last):
                frames = self.frames(finish=finish, last=last)
                for frame in frames:
                    if frame["transition_eligible"]:
                        target = frames[frame["frame_index"] + 1]
                        self.assertTrue(target["training_eligible"])
                        self.assertLess(target["race_time_ms"], finish)
                self.assertFalse(frames[last]["transition_eligible"])

    def test_conditional_never_sets_strict_mask_or_unconditional_bound(self):
        frames = self.frames(conditional=True)
        self.assertTrue(any(f["experimental_training_eligible"] for f in frames))
        self.assertTrue(any(f["experimental_transition_eligible"] for f in frames))
        for frame in frames:
            self.assertFalse(frame["training_eligible"])
            self.assertFalse(frame["transition_eligible"])
            self.assertIsNone(frame["time_alignment_max_absolute_error_ms"])

    def test_calibration_cannot_mark_finish_or_postfinish_as_usable(self):
        import test_join_dataset as prior_tests

        with tempfile.TemporaryDirectory() as temp:
            cal, expected, pts, scene = prior_tests.JoinTests().native_calibration(temp)
            for index in (60, 61, 70):
                with self.subTest(index=index):
                    changed = copy.deepcopy(cal)
                    changed["observation_window"]["last_usable_frame_index"] = index
                    changed["observation_window"]["last_usable_race_ms"] = int(pts[index]) - 200
                    with self.assertRaisesRegex(ValueError, "within original race clock"):
                        automatic.validate_calibration(changed, expected, pts, F(1420), Path(temp), scene)

    def test_full250_split_stable_disjoint_and_duplicate_players_rejected(self):
        runs = [dict(run_id=str(i), account_id="player-" + str(i)) for i in range(250)]
        result = automatic.split_runs(runs, "unit-test")
        self.assertEqual(result, automatic.split_runs(list(reversed(runs)), "unit-test"))
        self.assertEqual(
            [list(result.values()).count(k) for k in ("train", "validation", "test")], [200, 25, 25]
        )
        runs[1]["account_id"] = runs[0]["account_id"]
        with self.assertRaises(ValueError):
            automatic.split_runs(runs, "unit-test")


class LegacyCalibrationMigrationTests(unittest.TestCase):
    def scenario(self, temporary):
        base = Path(temporary)
        source = base / "source.json"
        source.write_text("{}")
        output = base / "calibration.json"
        calibration = dict(
            builder_inputs={"builder_sha256": automatic.sha(Path(supervisor.legacy_builder.__file__))},
            observation_window={"method": "synthetic old calibration"},
            mapping={"max_absolute_error_ms": 30},
        )
        output.write_bytes(automatic.encoded(calibration))
        return output, base / "new-supervisor-checkpoint.json", dict(render_result=source)

    def test_legacy_migration_revalidates_read_only_and_preserves_exact_bytes_mtime(self):
        with tempfile.TemporaryDirectory() as temp:
            output, proof, paths = self.scenario(temp)
            before = (output.read_bytes(), output.stat().st_mtime_ns)

            def revalidate(args):
                self.assertTrue(args.check_only)
                self.assertEqual(args.output, str(output))
                return {"run_id": "synthetic", "status": "measured_calibration_checked"}

            with (
                mock.patch.object(supervisor, "verified_run_inputs"),
                mock.patch.object(supervisor, "measured_parameters", return_value={}),
                mock.patch.object(supervisor.legacy_builder, "build", side_effect=revalidate) as legacy,
                mock.patch.object(supervisor.builder, "build") as current,
            ):
                result = supervisor.process_one(
                    {"run_id": "synthetic", "ordinal": 78}, {}, {}, paths, output, proof, {}
                )
            self.assertEqual(result["status"], "calibration_reused_verified")
            self.assertEqual(before, (output.read_bytes(), output.stat().st_mtime_ns))
            self.assertTrue(automatic.read(proof)["preserved_existing_calibration"])
            legacy.assert_called_once()
            current.assert_not_called()

    def test_migration_detects_accidental_existing_calibration_write(self):
        with tempfile.TemporaryDirectory() as temp:
            output, proof, paths = self.scenario(temp)

            def invalid_builder(args):
                output.write_text("changed synthetic artifact")
                return {"run_id": "synthetic"}

            with (
                mock.patch.object(supervisor, "verified_run_inputs"),
                mock.patch.object(supervisor, "measured_parameters", return_value={}),
                mock.patch.object(supervisor.legacy_builder, "build", side_effect=invalid_builder),
            ):
                with self.assertRaisesRegex(ValueError, "Existing calibration changed"):
                    supervisor.process_one(
                        {"run_id": "synthetic", "ordinal": 78}, {}, {}, paths, output, proof, {}
                    )
            self.assertFalse(proof.exists())

    def test_previous_automatic_builder_revalidates_without_rewriting(self):
        with tempfile.TemporaryDirectory() as temp:
            output, proof, paths = self.scenario(temp)
            cal = automatic.read(output)
            cal["builder_inputs"]["builder_sha256"] = automatic.sha(
                Path(supervisor.previous_automatic_builder.__file__)
            )
            output.write_bytes(automatic.encoded(cal))
            before = (output.read_bytes(), output.stat().st_mtime_ns)

            def check(args):
                self.assertTrue(args.check_only)
                return {"run_id": "synthetic"}

            with (
                mock.patch.object(supervisor, "verified_run_inputs"),
                mock.patch.object(supervisor, "measured_parameters", return_value={}),
                mock.patch.object(
                    supervisor.previous_automatic_builder, "build", side_effect=check
                ) as previous,
                mock.patch.object(supervisor.builder, "build") as current,
            ):
                supervisor.process_one(
                    {"run_id": "synthetic", "ordinal": 14}, {}, {}, paths, output, proof, {}
                )
            previous.assert_called_once()
            current.assert_not_called()
            self.assertEqual(before, (output.read_bytes(), output.stat().st_mtime_ns))
            self.assertEqual(
                automatic.read(proof)["legacy_migration"]["policy"],
                "revalidate_preserved_automatic_calibration_v1",
            )

    def test_unknown_existing_builder_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            output, proof, paths = self.scenario(temp)
            cal = automatic.read(output)
            cal["builder_inputs"]["builder_sha256"] = "0" * 64
            output.write_bytes(automatic.encoded(cal))
            with (
                mock.patch.object(supervisor, "verified_run_inputs"),
                mock.patch.object(supervisor, "measured_parameters", return_value={}),
            ):
                with self.assertRaisesRegex(ValueError, "unknown builder"):
                    supervisor.process_one(
                        {"run_id": "synthetic", "ordinal": 14}, {}, {}, paths, output, proof, {}
                    )
            self.assertFalse(proof.exists())


if __name__ == "__main__":
    unittest.main()
