"""Independent synthetic export tests; no render or calibration attestation."""

import json
from pathlib import Path
import shutil
import tempfile
import unittest
import export_portable_automatic_v2 as p


class ConditionalPortableReview(unittest.TestCase):
    def fixture(self, base, strict=False, numeric=False):
        source = base / "source"
        source.mkdir()
        release = base / "release"
        release.mkdir()
        original = source / "proof.json"
        original.write_text('{"scope":"synthetic exporter unit test"}\n')
        archive = p.Archive(release)
        archive.archive(original)
        archive.drain()
        index = archive.write(
            "audit/index.json",
            p.encode(
                {"entries": [{"original_path": k[0], "release_file": v} for k, v in archive.entries.items()]}
            ),
        )
        rows = []
        for n in range(1, 6):
            conditional = n == 1
            eligible = (not conditional) or strict
            frame = {
                "frame_index": 0,
                "phase": "race",
                "observation_usable": True,
                "training_eligible": eligible,
                "transition_eligible": False,
            }
            if conditional:
                frame.update(
                    experimental_training_eligible=True,
                    experimental_transition_eligible=False,
                    video_training_eligible=True,
                    time_alignment_max_absolute_error_ms=None,
                    time_alignment_conditional_max_absolute_error_ms=30,
                )
            table = archive.write(f"runs/{n}/frames.jsonl", p.encode(frame))
            mapping = {"max_absolute_error_ms": 30 if numeric or not conditional else None}
            if conditional:
                mapping["conditional_max_absolute_error_ms"] = 30
            summary = archive.write(
                f"runs/{n}/run.json",
                p.encode(
                    {
                        "run_id": str(n),
                        "account_id": str(n),
                        "frames": 1,
                        "frame_actions": table,
                        "mapping": mapping,
                    }
                ),
            )
            rows.append(
                dict(
                    run_id=str(n),
                    account_id=str(n),
                    ordinal=n,
                    split="pilot",
                    frames=1,
                    training_observations=int(eligible),
                    valid_transitions=0,
                    experimental_training_observations=1,
                    experimental_valid_transitions=0,
                    video_training_observations=1,
                    summary=summary,
                    frame_actions=table,
                    timing_evidence_class="conditional_control_reflection"
                    if conditional
                    else "direct_checkpoint_notices",
                )
            )
        totals = {
            key: sum(row[key] for row in rows)
            for key in (
                "frames",
                "training_observations",
                "valid_transitions",
                "experimental_training_observations",
                "experimental_valid_transitions",
                "video_training_observations",
            )
        }
        manifest = dict(
            status="portable_dataset_verified",
            requested_count=5,
            runs=rows,
            audit_archive=index,
            totals=totals,
            split_counts={"pilot": 5},
            files=list(archive.inventory.values()),
        )
        (release / "release_manifest.json").write_bytes(p.encode(manifest))
        return source, release

    def test_relocation_keeps_conditional_counts_without_source_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, release = self.fixture(root)
            shutil.rmtree(source)
            relocated = root / "relocated"
            release.rename(relocated)
            result = p.verify_release(relocated)
            self.assertEqual(result["training_observations"], 4)
            self.assertEqual(result["experimental_training_observations"], 5)
            self.assertEqual(result["video_training_observations"], 5)
            self.assertFalse(result["source_absolute_paths_accessed"])

    def test_self_consistent_conditional_strict_mask_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, release = self.fixture(Path(tmp), strict=True)
            with self.assertRaisesRegex(ValueError, "Conditional source admitted to strict"):
                p.verify_release(release)

    def test_self_consistent_unconditional_bound_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, release = self.fixture(Path(tmp), numeric=True)
            with self.assertRaisesRegex(ValueError, "Conditional source declares unconditional"):
                p.verify_release(release)

    def test_nested_missing_audit_proof_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            release = root / "release"
            release.mkdir()
            proof = source / "proof.json"
            proof.write_text(json.dumps({"nested": {"path": str(source / "missing.py"), "sha256": "0" * 64}}))
            archive = p.Archive(release)
            archive.archive(proof)
            with self.assertRaisesRegex(ValueError, "Historical source version unavailable"):
                archive.drain()


if __name__ == "__main__":
    unittest.main()
