#!/usr/bin/env python3
"""Deterministic positive and corruption negative tests for automatic native QA."""

import copy
import shutil
import subprocess
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
import automatic_video_policy_v1 as policy
import production_video_audit_automatic_v1 as accept


class AutomaticPolicyTests(unittest.TestCase):
    def fixture(self):
        return {
            "first": 11,
            "last": 983,
            "pts": list(range(0, 19900, 20)),
            "entity_start_ms": 200,
            "camera_end_ms": 19880,
            "finish_ms": 19455,
            "black_frames": [0],
            "means": [80] * 995,
            "maxima": [250] * 995,
            "deltas": [3] * 995,
            "scene_scores": [[2, 3, 2.5]] * 973,
            "clean_clock_world_mae": 1,
            "clean_clock_car_mae": 1,
            "scanner_failures": [],
        }

    def test_real_source_finish_can_precede_last_visible_image(self):
        m = self.fixture()
        self.assertGreater(m["pts"][m["last"]] - 200, m["finish_ms"])
        self.assertEqual(policy.image_decision(m), [])

    def test_black_freeze_wrong_camera_missing_prefix_and_clock_mismatch_quarantine(self):
        for mutation, reason in [
            ("black", "unexpected_black_frame"),
            ("freeze", "frozen_race_frames"),
            ("wrong_camera", "unrecognized_chase_camera_scene_or_anomaly"),
            ("prefix", "visible_start_outside_40ms"),
            ("clock", "clean_clock_scene_mismatch"),
            ("suffix", "visible_finish_missing_more_than_40ms"),
        ]:
            with self.subTest(mutation=mutation):
                m = self.fixture()
                if mutation == "black":
                    m["black_frames"].append(400)
                elif mutation == "freeze":
                    m["deltas"][100:106] = [0] * 6
                elif mutation == "wrong_camera":
                    m["scene_scores"][300] = [70, 50, 60]
                elif mutation == "prefix":
                    m["entity_start_ms"] = 100
                elif mutation == "clock":
                    m["clean_clock_car_mae"] = 30
                else:
                    m["finish_ms"] = 20000
                self.assertIn(reason, policy.image_decision(m))

    def test_no_frame_or_nonfinite_score_can_pass(self):
        m = self.fixture()
        m["scene_scores"][30] = [float("nan"), 0, 0]
        self.assertIn("incomplete_scene_comparison", policy.image_decision(m))
        m = self.fixture()
        m["scene_scores"].pop()
        self.assertIn("incomplete_scene_comparison", policy.image_decision(m))

    def test_matcher_finds_feasible_reference_before_smaller_joint_error(self):
        # The first reference has a smaller joint error but violates the world
        # bound; the second satisfies all independent constraints.
        bank = {
            "features": np.array([[31] * 480 + [10] * 210, [29] * 480 + [20] * 210], dtype=np.uint8),
            "progress": np.array([0.5, 0.5]),
            "ordinal": np.array([1, 2]),
            "occlusion": np.array([False, False]),
        }
        scores, indices = policy.scene_scores(np.zeros((1, 690), dtype=np.uint8), np.array([0.5]), bank)
        self.assertEqual(indices, [1])
        self.assertTrue(policy.scene_admission(scores, [False], [False])[0])

    def test_reviewed_occlusion_requires_local_normal_context_and_bound(self):
        scores = np.array([[3, 4, 3.5], [3, 4, 3.5], [35, 33, 34], [3, 4, 3.5], [3, 4, 3.5]])
        supported = [False, False, True, False, False]
        self.assertTrue(policy.scene_admission(scores, supported, supported)[0])
        self.assertFalse(policy.scene_admission(scores, [False] * 5, supported)[0])
        self.assertFalse(policy.scene_admission(scores, supported, [False] * 5)[0])
        long = np.array([[3, 4, 3.5]] + [[35, 33, 34]] * 4 + [[3, 4, 3.5]])
        self.assertFalse(policy.scene_admission(long, [True] * 6, [True] * 6)[0])
        edge = np.array([[35, 33, 34], [3, 4, 3.5], [3, 4, 3.5]])
        self.assertFalse(policy.scene_admission(edge, [True] * 3, [True] * 3)[0])

    def test_marker_exact_number_consensus_and_wrong_label(self):
        observed = ["CLIP 19380 ms", "CLiP 19380 ms", "unreadable"]
        self.assertTrue(policy.marker_text_decision("CLIP 19380 ms", observed))
        self.assertFalse(policy.marker_text_decision("CLIP 19360 ms", observed))
        self.assertFalse(policy.marker_text_decision("CLIP 19380 ms", ["CLIP 19380 ms", "unknown"]))
        self.assertFalse(
            policy.marker_text_decision("CLIP 19380 ms", ["CLIP 19380 ms"] * 2 + ["CLIP 19400 ms"] * 3)
        )
        self.assertFalse(
            policy.marker_text_decision("CLIP 19380 ms", ["CLIP 19380 ms"] * 2 + ["CLIP 19400 ms"] * 2)
        )

    def test_camera_needs_external_target_one(self):
        c = {"class": "CGameCtnMediaBlockCameraGame", "clip_entity_id": 1, "game_camera": "External"}
        policy.verify_camera({"camera": c})
        for key, value in [
            ("clip_entity_id", 0),
            ("game_camera", "Internal"),
            ("class", "CGameCtnMediaBlockCameraCustom"),
        ]:
            changed = dict(c)
            changed[key] = value
            with self.assertRaises(ValueError):
                policy.verify_camera({"camera": changed})

    def test_historical_manual_review_not_relabelled(self):
        r = {
            "schema_version": 1,
            "review_mode": "automated_scan_plus_sampled_visual_review",
            "status": "automatic_checks_passed",
            "manual_review_performed": False,
            "unresolved_issues": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.json"
            path.write_text(json.dumps(r))
            with self.assertRaisesRegex(ValueError, "Automatic policy did not pass"):
                policy.verify_record(path)

    def test_automatic_evidence_contains_no_explicit_or_claimed_manual_review(self):
        r = {
            "settings_equivalence": {"path": "settings", "sha256": "a"},
            "completion_equivalence": {"path": "completion", "sha256": "b"},
        }
        value = {
            "explicit_review": {"path": "wrong", "sha256": "x"},
            "review_mode": "old",
            "settings_observation": {"evidence_files": []},
            "scene_observation": {"evidence_files": []},
            "completion_observation": {"evidence_files": []},
        }
        with (
            patch.object(accept.historical, "evidence_for", return_value=value),
            patch.object(policy.ui, "pin", return_value={"path": "automatic", "sha256": "c"}),
        ):
            e = accept.export_evidence(r, {}, {}, None, Path("automatic"), "clean")
        self.assertNotIn("explicit_review", e)
        self.assertFalse(e["manual_review_performed"])
        self.assertEqual(e["review_mode"], policy.MODE)
        self.assertTrue(
            all(
                e[k]["manual_review_performed"] is False
                for k in ("settings_observation", "scene_observation", "completion_observation")
            )
        )

    def test_uncommitted_directory_never_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "Uncommitted automatic output"):
                accept.run(Path("draft"), Path("reference"), Path("accepted"), Path(tmp))

    def test_reject_incomplete_decodable_container(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg is required for container corruption test")
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "synthetic.webm"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=640x360:rate=50",
                    "-t",
                    "0.2",
                    "-c:v",
                    "libvpx",
                    str(video),
                ],
                check=True,
            )
            bad = Path(tmp) / "corrupt.webm"
            bad.write_bytes(video.read_bytes()[:100])
            with self.assertRaises(Exception):
                policy.decode_features(bad)


if __name__ == "__main__":
    unittest.main()
