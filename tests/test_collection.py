"""Offline acquisition tests: identity, resume, corruption and promotion gates."""

import copy
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from collector import batch, download, snapshot
from collector.common import inspect_gbx, sha256, validate_snapshot, write_json


def gbx(class_id=0x03092000):
    return b"GBX" + struct.pack("<H", 6) + b"BUCR" + struct.pack("<I", class_id) + b"synthetic-test"


def cohort(count=6):
    return {
        "mapUid": "test-map",
        "mapId": "33333333-3333-3333-3333-333333333333",
        "cohort": "test-v1",
        "count": count,
        "snapshot_atomic": False,
        "entries": [
            {
                "ordinal": i + 1,
                "rank": i + 1,
                "accountId": f"11111111-1111-1111-1111-{i:012d}",
                "mapRecordId": f"22222222-2222-2222-2222-{i:012d}",
                "scoreMs": 10000 + i,
                "leaderboardTimestamp": "2020-01-01T00:00:00Z",
            }
            for i in range(count)
        ],
    }


class CollectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.frozen = self.root / "leaderboard.json"
        write_json(self.frozen, cohort())
        self.map_file = self.root / "map.Map.Gbx"
        self.map_file.write_bytes(gbx(0x03043000))
        self.output = self.root / "collection"

    def fake_fetch(self, url, target, user_agent, classes, limit):
        target.write_bytes(gbx() + url.encode())
        return {
            "path": str(target),
            "sha256": sha256(target),
            "bytes": target.stat().st_size,
            "gbx_class_id": "0x03092000",
            "request": {"url": url},
        }

    def collect(self, count=5):
        with (
            patch.object(download, "fetch", side_effect=self.fake_fetch),
            patch.object(download.time, "sleep"),
        ):
            return download.collect(self.frozen, self.output, count, "TrackManiaBench/tests", self.map_file)

    def test_immutable_resume_does_not_download_again(self):
        self.collect()
        with patch.object(download, "fetch", side_effect=AssertionError("network call on resume")):
            result = download.collect(self.frozen, self.output, 5, "TrackManiaBench/tests", self.map_file)
        self.assertEqual(result["count"], 5)
        self.assertEqual(len(json.loads((self.output / "manifest.json").read_text())["runs"]), 5)

    def test_collection_manifest_is_accepted_by_actual_extractor_loader(self):
        from extractor.batch_extract import load_manifest

        self.collect()
        manifest = load_manifest(self.output / "manifest.json", 5)
        self.assertEqual(manifest["requested_count"], 5)
        self.assertEqual([r["ordinal"] for r in manifest["runs"]], [1, 2, 3, 4, 5])

    def test_missing_progress_cannot_replace_completed_map_pins(self):
        self.collect()
        before = (self.output / "manifest.json").read_bytes()
        (self.output / "collection-progress.json").unlink()
        self.map_file.write_bytes(gbx(0x03043000) + b"changed")
        with self.assertRaisesRegex(ValueError, "lacks its progress pins"):
            self.collect()
        self.assertEqual((self.output / "manifest.json").read_bytes(), before)

    def test_progress_cannot_replace_completed_source_pins(self):
        self.collect()
        progress_path = self.output / "collection-progress.json"
        progress = json.loads(progress_path.read_text())
        self.map_file.write_bytes(gbx(0x03043000) + b"changed")
        progress["map"]["sha256"] = sha256(self.map_file)
        write_json(progress_path, progress)
        with self.assertRaisesRegex(ValueError, "source pins differ"):
            self.collect()

    def promotion(self, destination, map_file):
        # Only the image-evidence gate is replaced. Snapshot, pilot/source bytes,
        # manifest normalization, target bindings and downloads use real code.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "renderer"))
        pilot = self.root / "pilot.json"
        pilot.write_bytes((self.output / "manifest.json").read_bytes())
        with (
            patch("render_queue.validate_gate"),
            patch.object(download, "fetch", side_effect=self.fake_fetch),
            patch.object(download.time, "sleep"),
        ):
            return download.collect(
                self.frozen,
                destination,
                6,
                "TrackManiaBench/tests",
                map_file,
                self.root / "gate.json",
                pilot,
                self.root / "renders",
            )

    def test_promotion_cannot_use_another_map_in_fresh_output(self):
        self.collect()
        changed_map = self.root / "changed.Map.Gbx"
        changed_map.write_bytes(gbx(0x03043000) + b"different")
        with self.assertRaisesRegex(ValueError, "differs from the approved pilot"):
            self.promotion(self.root / "promoted", changed_map)

    def test_promotion_preserves_five_original_paths_and_hashes(self):
        self.collect()
        old = json.loads((self.output / "manifest.json").read_text())
        destination = self.root / "promoted"
        self.promotion(destination, None)
        promoted = json.loads((destination / "manifest.json").read_text())
        self.assertEqual(promoted["map"], old["map"])
        self.assertEqual(promoted["runs"][:5], old["runs"])
        self.assertEqual(len(promoted["runs"]), 6)

    def test_modified_ghost_is_rejected(self):
        self.collect()
        next((self.output / "ghosts").glob("*.Gbx")).write_bytes(gbx() + b"modified")
        with self.assertRaisesRegex(ValueError, "ghost changed"):
            self.collect()

    def test_changed_map_is_rejected(self):
        self.collect()
        self.map_file.write_bytes(gbx(0x03043000) + b"changed")
        with self.assertRaisesRegex(ValueError, "Map bytes changed"):
            self.collect()

    def test_promotion_requires_real_gate(self):
        with self.assertRaisesRegex(ValueError, "five downloads require"):
            self.collect(6)
        self.assertFalse(self.output.exists())

    def test_failed_request_keeps_progress(self):
        with (
            patch.object(download, "fetch", side_effect=OSError("offline")),
            patch.object(download.time, "sleep"),
        ):
            with self.assertRaisesRegex(OSError, "offline"):
                download.collect(self.frozen, self.output, 5, "TrackManiaBench/tests", self.map_file)
        result = json.loads((self.output / "collection-progress.json").read_text())
        self.assertEqual(result["failures"][0]["ordinal"], 1)
        self.assertFalse((self.output / "manifest.json").exists())

    def test_no_snapshot_replacement_on_resume(self):
        self.collect()
        changed = cohort()
        changed["entries"][-1]["scoreMs"] += 1
        write_json(self.frozen, changed)
        with self.assertRaisesRegex(ValueError, "another snapshot"):
            self.collect()

    def test_no_shrinking_existing_cohort(self):
        self.collect()
        with self.assertRaisesRegex(ValueError, "shrink"):
            self.collect(3)

    def test_batch_matches_frozen_ids_and_refuses_overwrite(self):
        self.collect()
        output = self.root / "batch.txt"
        manifest = self.output / "manifest.json"
        self.assertEqual(batch.write_batch(manifest, output), 5)
        self.assertEqual(output.read_text().splitlines()[0], "test-map; 22222222-2222-2222-2222-000000000000")
        self.assertEqual(batch.write_batch(manifest, output), 5)
        with self.assertRaisesRegex(ValueError, "different replay-conversion"):
            batch.write_batch(manifest, output, 1)

    def test_only_official_download_hosts(self):
        for url in [
            "http://core.trackmania.nadeo.live/test",
            "https://core.trackmania.nadeo.live.evil.test/test",
            "https://user:password@core.trackmania.nadeo.live/test",
            "https://trackmania-evil.amazonaws.com.evil.test/test",
        ]:
            self.assertFalse(download.valid_download_url(url))
        self.assertTrue(download.valid_download_url("https://core.trackmania.nadeo.live/test"))
        self.assertTrue(
            download.valid_download_url("https://trackmania-prod.s3.eu-west-1.amazonaws.com/test")
        )

    def test_snapshot_rejects_duplicate_and_unordered_records(self):
        for field, value in (
            ("accountId", cohort()["entries"][0]["accountId"]),
            ("scoreMs", 1),
            ("ordinal", 99),
        ):
            modified = cohort()
            modified["entries"][-1][field] = value
            with self.assertRaises(ValueError):
                validate_snapshot(modified)

    def test_gbx_header_validation_is_not_just_magic(self):
        for content in [b"GBX", gbx(0x03043000), gbx().replace(b"GBX\x06", b"GBX\x05", 1)]:
            file = self.root / "bad.Gbx"
            file.write_bytes(content)
            with self.assertRaises(ValueError):
                inspect_gbx(file)


class SnapshotTests(unittest.TestCase):
    def test_freeze_and_cache_keep_original_identities(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            rows = cohort(3)["entries"]

            def get(client, url, path):
                if "/api/map/" in url:
                    value = {"mapUid": "test-map", "mapId": cohort()["mapId"], "hasClones": False}
                else:
                    value = {
                        "tops": [
                            {
                                "player": {"id": row["accountId"]},
                                "position": row["rank"],
                                "time": row["scoreMs"],
                                "timestamp": row["leaderboardTimestamp"],
                                "url": "/api/download/ghost/" + row["mapRecordId"],
                            }
                            for row in rows
                        ]
                    }
                write_json(path, value)
                return value, {"path": path.name, "sha256": sha256(path), "url": url}

            with patch.object(snapshot.Client, "cached_get", get):
                target = snapshot.freeze("test-map", 3, "test-v1", output, "TrackManiaBench/tests")
            digest = sha256(target)
            with patch.object(snapshot.Client, "cached_get", side_effect=AssertionError("must not refresh")):
                self.assertEqual(
                    snapshot.freeze("test-map", 3, "test-v1", output, "TrackManiaBench/tests"), target
                )
            self.assertEqual(sha256(target), digest)
            original = target.read_bytes()
            altered = json.loads(original)
            altered["entries"][-1]["scoreMs"] += 1
            write_json(target, altered)
            with self.assertRaisesRegex(ValueError, "entries differ from source"):
                snapshot.freeze("test-map", 3, "test-v1", output, "TrackManiaBench/tests")
            target.write_bytes(original)
            (output / "page-00000.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "response changed"):
                snapshot.freeze("test-map", 3, "test-v1", output, "TrackManiaBench/tests")


if __name__ == "__main__":
    unittest.main()
