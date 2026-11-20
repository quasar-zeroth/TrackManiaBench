"""Standalone release verification must retain the separate eligibility counts."""

import json
from pathlib import Path
import tempfile
import unittest
import export_portable_automatic_v2 as export


class PortableConditionalTests(unittest.TestCase):
    def fixture(self, root, mutate=None):
        files = []
        runs = []
        totals = dict(
            frames=0,
            training_observations=0,
            valid_transitions=0,
            experimental_training_observations=0,
            experimental_valid_transitions=0,
            video_training_observations=0,
        )

        def write(name, data):
            p = root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            ref = export.pin(p, root)
            files.append(ref)
            return ref

        for n in range(1, 6):
            conditional = n == 5
            frames = [
                dict(
                    frame_index=i,
                    phase="race",
                    observation_usable=True,
                    training_eligible=not conditional,
                    transition_eligible=not conditional and i == 0,
                )
                for i in range(2)
            ]
            if conditional:
                for f in frames:
                    f.update(
                        experimental_training_eligible=True,
                        experimental_transition_eligible=f["frame_index"] == 0,
                        video_training_eligible=True,
                        time_alignment_max_absolute_error_ms=None,
                        time_alignment_conditional_max_absolute_error_ms=30,
                    )
            if conditional and mutate == "strict_mask":
                frames[0]["training_eligible"] = True
            summary = dict(
                run_id=str(n),
                account_id=str(n),
                frames=2,
                mapping={"max_absolute_error_ms": None if conditional else 30},
            )
            if conditional and mutate == "unconditional_bound":
                summary["mapping"]["max_absolute_error_ms"] = 30
            row = dict(
                run_id=str(n),
                account_id=str(n),
                ordinal=n,
                split="pilot",
                frames=2,
                summary=write(f"{n}/summary.json", export.encode(summary)),
                frame_actions=write(f"{n}/frames.jsonl", b"".join(export.encode(f) for f in frames)),
                training_observations=sum(f["training_eligible"] for f in frames),
                valid_transitions=sum(f["transition_eligible"] for f in frames),
                experimental_training_observations=2,
                experimental_valid_transitions=1,
                video_training_observations=2,
                timing_evidence_class="conditional_control_reflection"
                if conditional
                else "direct_checkpoint_notices",
            )
            if conditional and mutate == "experimental_count":
                row["experimental_training_observations"] = 0
            runs.append(row)
            for key in totals:
                totals[key] += row[key]
        archive = write("audit.json", export.encode({"entries": []}))
        manifest = dict(
            status="portable_dataset_verified",
            requested_count=5,
            runs=runs,
            files=files,
            audit_archive=archive,
            totals=totals,
            split_counts={"pilot": 5},
        )
        (root / "release_manifest.json").write_bytes(export.encode(manifest))

    def test_distinct_video_experimental_and_strict_counts_verify_offline(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.fixture(root)
            report = export.verify_release(root)
            self.assertEqual(report["training_observations"], 8)
            self.assertEqual(report["experimental_training_observations"], 10)
            self.assertEqual(report["video_training_observations"], 10)
            self.assertFalse(report["source_absolute_paths_accessed"])

    def test_conditional_strict_masks_bounds_and_counts_cannot_be_promoted(self):
        for mutation in ("strict_mask", "unconditional_bound", "experimental_count"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                self.fixture(root, mutation)
                with self.assertRaises(ValueError):
                    export.verify_release(root)


if __name__ == "__main__":
    unittest.main()
