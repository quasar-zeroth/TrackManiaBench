import tempfile
from pathlib import Path
import unittest
import json
from types import SimpleNamespace
from unittest.mock import patch

from trackmaniabench.config import load, REPOSITORY


class ConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.file = self.root / "bench.toml"
        self.text = (REPOSITORY / "configs/example.toml").read_text()
        self.file.write_text(self.text)

    def test_paths_resolve_against_config(self):
        config = load(self.file)
        self.assertEqual(config.workspace, (self.root / "../data/summer2020-01").resolve())
        self.assertEqual(config.count, 250)
        self.assertEqual(config.environment()["TRACKMANIABENCH_SSH_HOST"], "render-pc")

    def test_rejects_invalid_count_and_unvalidated_profile(self):
        for old, new in [
            ("count = 250", "count = 0"),
            ("count = 250", "count = 1.5"),
            ("native-chase-360p50", "arbitrary-4k"),
        ]:
            self.file.write_text(self.text.replace(old, new))
            with self.assertRaises(ValueError):
                load(self.file)

    def test_map_uid_is_not_a_path_or_query(self):
        self.file.write_text(self.text.replace("XJ_JEjWGoAexDWe8qfaOjEcq5l8", "../../x?token=y"))
        with self.assertRaises(ValueError):
            load(self.file)

    def test_download_rejects_snapshot_from_previous_configuration(self):
        from trackmaniabench.cli import collect

        self.file.write_text(
            self.text.replace("../data/summer2020-01", "work").replace("count = 250", "count = 1")
        )
        folder = self.root / "work/snapshot"
        folder.mkdir(parents=True)
        snapshot = {
            "mapUid": "different-map",
            "mapId": "33333333-3333-3333-3333-333333333333",
            "cohort": "previous-cohort",
            "count": 1,
            "snapshot_atomic": False,
            "entries": [
                {
                    "ordinal": 1,
                    "rank": 1,
                    "scoreMs": 10000,
                    "accountId": "11111111-1111-1111-1111-111111111111",
                    "mapRecordId": "22222222-2222-2222-2222-222222222222",
                }
            ],
        }
        (folder / "leaderboard.json").write_text(json.dumps(snapshot))
        with patch("collector.download.collect") as acquire:
            with self.assertRaisesRegex(ValueError, "differs from configuration"):
                collect(SimpleNamespace(config=self.file, command="download", count=1))
            acquire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
