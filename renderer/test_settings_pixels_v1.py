"""Synthetic visible-settings pixel comparison regression tests."""

import unittest
import numpy as np
import settings_pixels_v1 as s


class SettingsPixelsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = np.random.default_rng(1234).integers(0, 250, (1080, 1920, 3), dtype=np.int16)

    def test_single_changed_pixel_in_every_field_fails(self):
        for name, (x0, y0, x1, y1) in s.FIELDS.items():
            with self.subTest(field=name):
                changed = self.reference.copy()
                changed[y0 + 3, x0 + 3, 0] += 2
                self.assertFalse(s.compare(self.reference, changed)["passed"])

    def test_numeric_value_shift_and_checked_audio_fail(self):
        for kind in ("fps", "audio"):
            with self.subTest(kind=kind):
                changed = self.reference.copy()
                if kind == "fps":
                    changed[455:481, 837:880] = np.roll(changed[455:481, 837:880], 4, axis=1)
                else:
                    changed[415:425, 1664:1674] = 255
                self.assertFalse(s.compare(self.reference, changed)["passed"])

    def test_measured_quantization_only_is_allowed(self):
        changed = np.minimum(self.reference + 1, 255)
        self.assertTrue(s.compare(self.reference, changed)["passed"])
        with self.assertRaises(ValueError):
            s.compare(self.reference, changed[:1000])

    def test_unobserved_background_is_explicitly_out_of_scope(self):
        changed = self.reference.copy()
        changed[900:1000, 50:1800] = 255
        self.assertTrue(s.compare(self.reference, changed)["passed"])


if __name__ == "__main__":
    unittest.main()
