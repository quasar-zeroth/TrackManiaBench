"""Acceptance routing rejects incomplete or wrong-run machine receipts."""

import json
from pathlib import Path
import tempfile
import unittest

import calibrate_production_automatic_v2 as c


class AutomaticAcceptanceRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = dict(run_id="synthetic-run", ordinal=14, actions={"directory": str(self.root / "actions")})
        self.prepared = {"source_clock": {"path": str(self.root / "clock.json")}}
        self.audit = self.root / "audit" / "synthetic-run" / "automatic-policy-v1"
        self.audit.mkdir(parents=True)
        self.receipt = dict(
            schema_version=2,
            status="accepted_calibration_join_still_required",
            run_id="synthetic-run",
            ordinal=14,
        )
        self.window = {"review_mode": "deterministic_decoded_image_policy_v1"}
        (self.audit / "pulse-verification.json").write_text("{}")
        (self.audit / "observation-window-verification.json").write_text(json.dumps(self.window))

    def paths(self, receipt=True):
        if receipt:
            (self.audit / "acceptance-receipt.json").write_text(json.dumps(self.receipt))
        return c.input_paths(self.run, self.prepared, self.root / "render", self.root / "audit")

    def test_complete_correct_receipt_routes_to_automatic_proofs(self):
        self.assertEqual(self.paths()["visibility"], self.audit / "observation-window-verification.json")

    def test_without_committed_receipt_route_stays_historical(self):
        self.assertEqual(
            self.paths(False)["visibility"], self.audit.parent / "observation-window-verification.json"
        )

    def test_wrong_schema_status_run_or_ordinal_is_rejected(self):
        for key, value in [
            ("schema_version", 1),
            ("status", "prepared"),
            ("run_id", "another-run"),
            ("ordinal", 15),
        ]:
            with self.subTest(key=key):
                old = self.receipt[key]
                self.receipt[key] = value
                with self.assertRaisesRegex(ValueError, "incomplete or belongs to another run"):
                    self.paths()
                self.receipt[key] = old

    def test_missing_proof_or_wrong_mode_is_rejected(self):
        pulse = self.audit / "pulse-verification.json"
        pulse.unlink()
        with self.assertRaisesRegex(ValueError, "lacks required reports"):
            self.paths()
        pulse.write_text("{}")
        (self.audit / "observation-window-verification.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "policy differs"):
            self.paths()


if __name__ == "__main__":
    unittest.main()
