"""Storage and runtime negatives without SSH, desktop input, or actual media."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import admission
import runtime


class RuntimeTests(unittest.TestCase):
    def test_no_host_is_implicitly_authorized(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "SSH_HOST"):
                runtime.require_remote()

    def test_remote_payload_forwards_only_renderer_configuration(self):
        with patch.dict(
            os.environ,
            {
                "TRACKMANIABENCH_SSH_HOST": "render-host",
                "TRACKMANIABENCH_EXPECTED_HOSTNAME": "render-host",
                "TRACKMANIABENCH_GAME_DOCUMENTS": "/games/Trackmania",
                "TRACKMANIABENCH_REMOTE_DATA": "/datasets/tm",
                "SECRET_NOT_FOR_RENDERER": "never-forward-this",
            },
            clear=True,
        ):
            code = runtime.remote_prelude()
            self.assertNotIn("never-forward-this", code)
            self.assertIn("/games/Trackmania", code)

    def test_shell_option_as_host_is_rejected(self):
        with patch.dict(
            os.environ,
            {
                "TRACKMANIABENCH_SSH_HOST": "-oProxyCommand=bad",
                "TRACKMANIABENCH_EXPECTED_HOSTNAME": "host",
                "TRACKMANIABENCH_GAME_DOCUMENTS": "/games",
                "TRACKMANIABENCH_REMOTE_DATA": "/data",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError):
                runtime.require_remote()

    def test_changed_pin_never_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source"
            path.write_bytes(b"original")
            ref = admission.pin(path)
            path.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "changed"):
                admission.checked(ref)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.ns = {"__name__": "storage_test"}
        with patch.dict(
            os.environ,
            {
                "TRACKMANIABENCH_GAME_DOCUMENTS": str(root / "docs"),
                "TRACKMANIABENCH_REMOTE_DATA": str(root / "data"),
            },
        ):
            exec(compile(Path(__file__).with_name("storage.py").read_text(), "storage.py", "exec"), self.ns)
        n = self.ns
        self.staging = n["DATA"] / "render_staging/videos"
        self.staging.mkdir(parents=True)
        self.canonical = n["DATA"] / "native-preserved/videos"
        self.canonical.mkdir(parents=True)
        n["write_json"](self.canonical.parent / "owner.json", {"owner": n["V8_OWNER"]})
        self.archive = n["DATA"] / "native-fixed-slot/test-queue"
        self.out = self.archive / "run-1-clean"
        self.out.mkdir(parents=True)
        self.slot = n["DOCS"] / "Replays/Clips/DatasetQueue/DatasetSlot.Clip.Gbx"
        self.slot.parent.mkdir(parents=True)
        self.slot.write_bytes(b"source scene")
        pending = {
            "queue_id": "test-queue",
            "job_id": "run-1-clean",
            "source_sha256": n["sha"](self.slot),
            "staged_time_ns": 0,
        }
        n["write_json"](
            self.slot.parent / ".dataset-fixed-slot-owner.json",
            {"owner": n["OWNER"], "pending": pending, "slot_sha256": n["sha"](self.slot)},
        )
        n["identity"] = lambda value: value

        def closure(source, proof, archive, identity):
            digest = n["sha"](source)
            n["write_json"](proof, {"video": {"sha256": digest}})
            return digest

        n["closed_video"] = closure
        self.request = {
            "queue_id": "test-queue",
            "job_id": "run-1-clean",
            "identity": {},
            "inventory": {},
            "video_name": "Video01.webm",
            "storage_verifier_sha256": "test",
        }

    def test_native_file_is_preserved_twice_before_staging_is_removed(self):
        source = self.staging / "DatasetQueueVideo.webm"
        source.write_bytes(b"untouched native pixels")
        result = self.ns["harvest_empty"](self.request)
        self.assertFalse(source.exists())
        self.assertEqual(Path(result["path"]).read_bytes(), b"untouched native pixels")
        self.assertEqual((self.canonical / "Video01.webm").read_bytes(), b"untouched native pixels")
        receipt = self.ns["read"](Path(result["name_receipt"]))
        self.assertFalse(receipt["native_bytes_modified"])
        self.assertFalse(receipt["prior_preserved_outputs_replaced"])

    def test_existing_canonical_video_is_never_overwritten_or_deleted(self):
        source = self.staging / "Video01.webm"
        source.write_bytes(b"new")
        (self.canonical / "Video01.webm").write_bytes(b"old")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.ns["harvest_empty"](self.request)
        self.assertEqual(source.read_bytes(), b"new")
        self.assertEqual((self.canonical / "Video01.webm").read_bytes(), b"old")

    def test_ambiguous_and_preexisting_outputs_block_render(self):
        self.assertEqual(self.ns["dispatch"]({"operation": "empty_inventory"}), {})
        (self.staging / "unknown.webm").write_bytes(b"unknown")
        with self.assertRaisesRegex(ValueError, "pre-existing"):
            self.ns["dispatch"]({"operation": "empty_inventory"})
        with self.assertRaises(ValueError):
            self.ns["fresh_empty_name"]({}, {"a.webm": {}, "b.webm": {}})

    def test_partial_copy_failure_keeps_source(self):
        source = self.staging / "Video01.webm"
        source.write_bytes(b"new")

        def failed_copy(*args):
            raise OSError("disk full")

        self.ns["exclusive_copy"] = failed_copy
        with self.assertRaisesRegex(OSError, "disk full"):
            self.ns["harvest_empty"](self.request)
        self.assertEqual(source.read_bytes(), b"new")

    def test_pending_transaction_blocks_resume(self):
        with self.assertRaisesRegex(ValueError, "Pending"):
            self.ns["dispatch"]({"operation": "resume_state", "queue_id": "test-queue"})


if __name__ == "__main__":
    unittest.main()
