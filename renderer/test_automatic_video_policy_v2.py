"""Meaningful disagreement and provenance regressions for automatic policy v2."""

import hashlib
from pathlib import Path
import unittest
import automatic_video_policy_v1 as v1
import automatic_video_policy_v2 as p


def glyph(text=None, confident=False):
    return {"decoded_text": text if confident else None, "raw_candidate": text, "confident": confident}


class MarkerPolicyTests(unittest.TestCase):
    def test_original_ocr_primary_is_preserved(self):
        texts = ["CLIP 19340 ms"] * 9
        result = p.reconcile_marker("CLIP 19340 ms", texts, glyph())
        self.assertTrue(result["accepted"])
        self.assertEqual(result["reason"], "original_ocr_primary")

    def test_same_family_wrong_ocr_is_ambiguous(self):
        # Synthetic ambiguous transcripts: repeated wrong digits come from two
        # nearby white thresholds; one alpha view contained the correct label.
        texts = [
            "CLIP 19449 ms",
            "CLIP 19440 ms",
            "CLIP 19440 ms",
            "CLIP 19:0 ms",
            "CLIP 1834. + —™s",
            "CLIP 1$34. rs",
            "CLIP 19460 ms",
            "CLIP 193%40:10 ms",
            "CLIP 1994. ::) mms",
        ]
        inference = p.ocr_inference(texts)
        self.assertTrue(inference["legacy_consensus"])
        self.assertFalse(inference["strong_consensus"])
        result = p.reconcile_marker("CLIP 19460 ms", texts, glyph("CLIP 19460 ms", True))
        self.assertTrue(result["accepted"])
        self.assertEqual(result["reason"], "confident_independent_glyph_fallback")

    def test_strong_methods_disagree_quarantines(self):
        texts = [
            "CLIP 19440 ms",
            "CLIP 19440 ms",
            "unreadable",
            "CLIP 19440 ms",
            "unreadable",
            "unreadable",
            "unreadable",
            "unreadable",
            "unreadable",
        ]
        result = p.reconcile_marker("CLIP 19460 ms", texts, glyph("CLIP 19460 ms", True))
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "strong_ocr_glyph_disagreement")

    def test_confident_wrong_glyph_cannot_be_overruled(self):
        texts = ["CLIP 19460 ms", "CLIP 19460 ms"] + ["unreadable"] * 7
        result = p.reconcile_marker("CLIP 19460 ms", texts, glyph("CLIP 19440 ms", True))
        self.assertTrue(result["original_ocr_primary_passed"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "confident_glyph_contradicts_native_label")

    def test_weak_glyph_candidate_is_not_a_label(self):
        result = p.reconcile_marker("CLIP 19460 ms", ["unreadable"] * 9, glyph("CLIP 19460 ms", False))
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "marker_identity_ambiguous")

    def test_expected_value_does_not_select_ocr_candidate(self):
        texts = ["CLIP 19440 ms"] * 9
        first = p.reconcile_marker("CLIP 19440 ms", texts, glyph())
        second = p.reconcile_marker("CLIP 19460 ms", texts, glyph())
        self.assertEqual(first["ocr"], second["ocr"])
        self.assertTrue(first["accepted"])
        self.assertFalse(second["accepted"])

    def test_tied_votes_require_confident_glyph(self):
        texts = ["CLIP 19440 ms"] * 2 + ["CLIP 19460 ms"] * 2 + ["bad"] * 5
        self.assertFalse(p.ocr_inference(texts)["legacy_consensus"])
        self.assertTrue(p.reconcile_marker("CLIP 19460 ms", texts, glyph("CLIP 19460 ms", True))["accepted"])
        self.assertFalse(p.reconcile_marker("CLIP 19460 ms", texts, glyph())["accepted"])

    def test_ocr_family_layout_is_fixed(self):
        with self.assertRaises(ValueError):
            p.ocr_inference(["CLIP 1200 ms"] * 8)

    def test_v2_preserves_v1_scene_implementation(self):
        for name in (
            "decode_features",
            "scene_scores",
            "image_decision",
            "completion_pixels",
            "verify_camera",
        ):
            self.assertIs(getattr(p, name), getattr(v1, name))
        self.assertEqual(p.MODE, v1.MODE)
        self.assertEqual(p.POLICY_VERSION, "2")


if __name__ == "__main__":
    unittest.main()
