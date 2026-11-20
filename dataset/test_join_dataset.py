import copy
from fractions import Fraction as F
import json
from pathlib import Path
import tempfile
import shutil
import subprocess
import unittest
import join_dataset as j


def rows():
    return [
        dict(
            store_index=0,
            tick=i,
            timestamp_ms=t,
            valid_until_ms=t + 10,
            input_kind=2,
            steer_raw=i,
            steer_normalized=i / 100,
            throttle=1,
            brake=0,
            horn=0,
            free_look=0,
            jump=0,
            action_slot_mask=0,
            respawn=int(t == 10),
            secondary_respawn=0,
        )
        for i, t in enumerate(range(-10, 80, 10))
    ]


class JoinTests(unittest.TestCase):
    def test_offset_substeps_pulse_final_interval(self):
        # 50 FPS video; race starts 5 ms after video PTS zero, not on either frame grid.
        frames = j.join_frames(
            [F(0), F(20), F(40), F(60), F(80)],
            F(100),
            lambda p: p - 5,
            rows(),
            [
                dict(sequence=0, store_index=0, timestamp_ms=10, type="RespawnTM2020"),
                dict(sequence=1, store_index=0, timestamp_ms=20, type="SteerTM2020"),
            ],
            47,
        )
        self.assertEqual(
            [f["phase"] for f in frames], ["preroll", "race", "race", "postfinish", "postfinish"]
        )
        self.assertEqual([p["duration_ms"] for p in frames[0]["action_substeps"]], [10, 5])
        self.assertEqual([p["duration_ms"] for p in frames[1]["action_substeps"]], [5, 10, 5])
        self.assertEqual([p["duration_ms"] for p in frames[2]["action_substeps"]], [5, 7])
        self.assertEqual(sum(p["duration_ms"] for f in frames for p in f["action_substeps"]), 47)
        self.assertEqual(sum(p["respawn_events"] for f in frames for p in f["action_substeps"]), 1)
        self.assertEqual(frames[1]["action_substeps"][0]["respawn_events"], 0)
        self.assertFalse(frames[0]["training_eligible"])
        self.assertTrue(frames[1]["transition_eligible"])
        self.assertFalse(frames[2]["transition_eligible"])

    def test_fractional_rate_no_lost_coverage(self):
        frames = j.join_frames(
            [F(i * 20) for i in range(5)], F(100), lambda p: p * F(3, 5) - F(1, 3), rows(), [], 47
        )
        self.assertAlmostEqual(sum(p["duration_ms"] for f in frames for p in f["action_substeps"]), 47)
        self.assertTrue(
            any(not float(p["duration_ms"]).is_integer() for f in frames for p in f["action_substeps"])
        )

    def test_two_hundred_ms_lead_and_trail_excluded(self):
        held = []
        for i, t in enumerate(range(0, 1010, 10)):
            row = rows()[0].copy()
            row.update(tick=i, timestamp_ms=t, valid_until_ms=t + 10)
            held.append(row)
        frames = j.join_frames([F(t) for t in range(0, 1420, 20)], F(1420), lambda p: p - 200, held, [], 1000)
        self.assertEqual(
            [sum(f["phase"] == p for f in frames) for p in ("preroll", "race", "postfinish")], [10, 50, 11]
        )
        self.assertEqual(sum(f["training_eligible"] for f in frames), 50)
        self.assertEqual(sum(f["transition_eligible"] for f in frames), 49)
        self.assertEqual(sum(s["duration_ms"] for f in frames for s in f["action_substeps"]), 1000)
        self.assertTrue(
            all(
                not f["action_substeps"] and not f["training_eligible"]
                for f in frames
                if f["phase"] != "race"
            )
        )

    def test_missing_start_rejected(self):
        with self.assertRaisesRegex(ValueError, "coverage"):
            j.join_frames([F(20), F(40)], F(60), lambda p: p, rows(), [], 47)

    def test_action_gap_rejected(self):
        r = rows()
        r[2]["valid_until_ms"] = 15
        with self.assertRaisesRegex(ValueError, "gap"):
            j.join_frames([F(0), F(20), F(40)], F(60), lambda p: p, r, [], 47)

    def test_event_at_boundary_exactly_once(self):
        events = [
            dict(sequence=i, store_index=0, timestamp_ms=t, type="RespawnTM2020")
            for i, t in enumerate([0, 20, 40, 47])
        ]
        out = j.join_frames([F(0), F(20), F(40), F(60)], F(80), lambda p: p, rows(), events, 47)
        self.assertEqual(
            [[e for s in f["action_substeps"] for e in s["raw_event_sequences"]] for f in out],
            [[0], [1], [2], []],
        )

    def test_hash_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "evidence"
            p.write_text("genuine first bytes")
            ref = j.pin(p)
            p.write_text("changed")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                j.evidence(ref, Path(temp))

    def calibration(self, temp):
        p = Path(temp) / "measurement"
        p.write_text("synthetic unit test evidence, never a production calibration")
        expected = dict(finish_time_ms=1000, video_sha256="test-video", run_id="test-only")
        cal = dict(
            schema_version=1,
            verified=True,
            verified_by="unit-test",
            verified_utc="synthetic",
            method="synthetic test only",
            **expected,
            mapping=dict(video_pts_anchor_ms=40, race_time_anchor_ms=0, race_ms_per_video_ms=1),
            measurements=[
                dict(phase=phase, frame_index=i, observed_race_ms=t, evidence=j.pin(p))
                for phase, i, t in [("start", 2, 0), ("middle", 27, 500), ("finish", 52, 1000)]
            ],
        )
        return cal, expected, [F(i * 20) for i in range(54)]

    def test_measured_nonzero_calibration(self):
        with tempfile.TemporaryDirectory() as t:
            cal, expected, pts = self.calibration(t)
            mapping, audit = j.validate_calibration(cal, expected, pts, F(1080), Path(t))
            self.assertEqual(mapping(F(40)), 0)
            self.assertEqual(max(r["absolute_residual_ms"] for r in audit["residuals"]), 0)

    def test_calibration_not_verified_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            cal, expected, pts = self.calibration(t)
            cal["verified"] = False
            with self.assertRaisesRegex(ValueError, "not been measured"):
                j.validate_calibration(cal, expected, pts, F(1080), Path(t))

    def test_calibration_video_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            cal, expected, pts = self.calibration(t)
            cal["video_sha256"] = "another-video"
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                j.validate_calibration(cal, expected, pts, F(1080), Path(t))

    def test_calibration_drift_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            cal, expected, pts = self.calibration(t)
            cal["measurements"][1]["observed_race_ms"] = 530
            with self.assertRaisesRegex(ValueError, "residual"):
                j.validate_calibration(cal, expected, pts, F(1080), Path(t))

    def native_calibration(self, temp):
        cal, expected, _ = self.calibration(temp)
        cal["mapping"]["video_pts_anchor_ms"] = 200
        cal["mapping"]["max_absolute_error_ms"] = 30
        for measurement in cal["measurements"]:
            measurement["frame_index"] += 8
        scene = dict(
            preroll_ms=200,
            postroll_ms=200,
            record_start_ms=0,
            record_end_ms=1000,
            entity_start_ms=200,
            entity_end_ms=1400,
            camera_start_ms=0,
            camera_end_ms=1400,
            start_offset_ms=0,
            time_scale=1,
        )
        # Nonzero measured links cancel here; none of the offsets is inferred
        # from the 200 ms serialized preroll.
        evidence = [cal["measurements"][0]["evidence"]]
        cal["native_timeline"] = dict(
            serialized_scene=scene.copy(),
            video_to_clip=dict(
                video_pts_anchor_ms=0,
                clip_time_anchor_ms=1,
                clip_ms_per_video_ms=1,
                max_absolute_error_ms=20,
                method="synthetic only",
                evidence_files=evidence,
            ),
            record_to_race=dict(
                record_time_anchor_ms=1,
                race_time_anchor_ms=0,
                race_ms_per_record_ms=1,
                max_absolute_error_ms=10,
                method="synthetic only",
                evidence_files=evidence,
            ),
        )
        cal["observation_window"] = dict(
            first_usable_frame_index=11,
            last_usable_frame_index=59,
            first_usable_race_ms=20,
            last_usable_race_ms=980,
            reviewed_contiguous_race_visibility=True,
            method="synthetic only",
            evidence_files=evidence,
        )
        return cal, expected, [F(t) for t in range(0, 1420, 20)], scene

    def test_native_measured_clock_chain_with_padding(self):
        with tempfile.TemporaryDirectory() as t:
            cal, expected, pts, scene = self.native_calibration(t)
            mapping, audit = j.validate_calibration(cal, expected, pts, F(1420), Path(t), scene)
            self.assertEqual(mapping(F(200)), 0)
            self.assertEqual(audit["race_start_video_pts_ms"], 200)
            self.assertEqual(audit["exported_trailing_race_ms"], 220)
            self.assertFalse(audit["native_timeline"]["native_end_hold_assumed"])
            self.assertEqual(audit["native_timeline"]["declared_max_absolute_error_ms"], 30)
            self.assertEqual(audit["observation_window"]["unobserved_prefix_race_ms"], 20)
            self.assertEqual(audit["observation_window"]["training_action_coverage_ms"], 980)

    def test_native_clock_chain_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            cal, expected, pts, scene = self.native_calibration(t)
            cal["native_timeline"]["record_to_race"]["record_time_anchor_ms"] = 0
            with self.assertRaisesRegex(ValueError, "does not compose"):
                j.validate_calibration(cal, expected, pts, F(1420), Path(t), scene)

    def test_native_scene_padding_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            cal, expected, pts, scene = self.native_calibration(t)
            cal["native_timeline"]["serialized_scene"]["preroll_ms"] = 0
            with self.assertRaisesRegex(ValueError, "scene mismatch"):
                j.validate_calibration(cal, expected, pts, F(1420), Path(t), scene)

    def test_native_clock_link_evidence_required(self):
        with tempfile.TemporaryDirectory() as t:
            cal, expected, pts, scene = self.native_calibration(t)
            cal["native_timeline"]["video_to_clip"]["evidence_files"] = []
            with self.assertRaisesRegex(ValueError, "lacks measured evidence"):
                j.validate_calibration(cal, expected, pts, F(1420), Path(t), scene)

    def test_missing_native_visibility_review_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            cal, expected, pts, scene = self.native_calibration(t)
            cal["observation_window"]["reviewed_contiguous_race_visibility"] = False
            with self.assertRaisesRegex(ValueError, "visibility review"):
                j.validate_calibration(cal, expected, pts, F(1420), Path(t), scene)

    def test_understated_clock_precision_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            cal, expected, pts, scene = self.native_calibration(t)
            cal["mapping"]["max_absolute_error_ms"] = 0
            with self.assertRaisesRegex(ValueError, "understates"):
                j.validate_calibration(cal, expected, pts, F(1420), Path(t), scene)

    def test_unobserved_prefix_actions_retained_but_training_excluded(self):
        frames = j.join_frames(
            [F(0), F(20), F(40), F(60)],
            F(80),
            lambda p: p,
            rows(),
            [],
            47,
            dict(first_usable_frame_index=1, last_usable_frame_index=2),
        )
        self.assertFalse(frames[0]["observation_usable"])
        self.assertFalse(frames[0]["training_eligible"])
        self.assertEqual(sum(p["duration_ms"] for p in frames[0]["action_substeps"]), 20)
        self.assertEqual(sum(f["race_action_overlap_ms"] for f in frames if f["training_eligible"]), 27)
        self.assertTrue(frames[1]["transition_eligible"])
        self.assertFalse(frames[2]["transition_eligible"])

    def test_split_whole_player_stable_exact(self):
        runs = [dict(run_id=str(i), account_id=f"player-{i}") for i in range(250)]
        a = j.split_runs(runs, "fixed")
        b = j.split_runs(list(reversed(runs)), "fixed")
        self.assertEqual(a, b)
        self.assertEqual([list(a.values()).count(k) for k in ["train", "validation", "test"]], [200, 25, 25])
        runs[1]["account_id"] = runs[0]["account_id"]
        with self.assertRaisesRegex(ValueError, "player"):
            j.split_runs(runs, "fixed")

    def test_immutable_resume(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "sample.json"
            j.checked_write(p, b"original")
            j.checked_write(p, b"original", audit=True)
            with self.assertRaisesRegex(ValueError, "differs"):
                j.checked_write(p, b"modified")
            self.assertEqual(p.read_bytes(), b"original")

    @unittest.skipUnless(shutil.which("ffprobe") and shutil.which("ffmpeg"), "ffmpeg/ffprobe not installed")
    def test_real_webm_pts_and_final_duration(self):
        # Disposable synthetic codec fixture; never used as calibration or training evidence.
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "synthetic.webm"
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
                    "-an",
                    "-c:v",
                    "libvpx-vp9",
                    "-deadline",
                    "realtime",
                    "-cpu-used",
                    "8",
                    str(path),
                ],
                check=True,
            )
            pts, end, probe = j.probe_video(path, j.renderer.SETTINGS)
            self.assertEqual(pts, [F(i * 20) for i in range(10)])
            self.assertEqual(end, 200)
            self.assertTrue(probe["decoded_without_errors"])


if __name__ == "__main__":
    unittest.main()
