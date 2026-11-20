#!/usr/bin/env python3
"""Isolated tiny-file/process tests. No game, live watcher, media or remote host."""

import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import durable_dataset_supervisor_v1 as s
import dataset_pipeline_worker_v1 as w
import pipeline_common_v1 as c

FIXTURE = """import json,sys,time
from pathlib import Path
mode,out=sys.argv[1:3]
if mode=='slow':time.sleep(.4)
if mode=='fail':sys.exit(7)
if mode=='qa':
 rid,ordinal,accepted=sys.argv[3:6]
 if ordinal=='1':sys.exit(2)
 import hashlib
 def pin(p):return dict(path=str(p.resolve()),sha256=hashlib.sha256(p.read_bytes()).hexdigest())
 target=Path(accepted)/rid;target.mkdir(parents=True)
 (target/'video.webm').write_bytes(b'fixture-not-real-video')
 source=json.loads(Path(out).parent.parent.joinpath('source.json').read_text())
 result=dict(status='native_video_verified',run=source,video=dict(sha256=pin(target/'video.webm')['sha256']))
 (target/'result.json').write_text(json.dumps(result))
 folder=Path(out).parent;folder.mkdir(parents=True)
 review=folder/'automatic-review.json';review.write_text(json.dumps(dict(run_id=rid,fixture=True)))
 receipt=dict(schema_version=2,review_mode='deterministic_decoded_image_policy_v1',status='accepted_calibration_join_still_required',run_id=rid,ordinal=int(ordinal),automatic_review=pin(review),accepted_result=pin(target/'result.json'),evidence_files=[pin(review)])
 Path(out).write_text(json.dumps(receipt))
else:
 Path(out).write_text(json.dumps(dict(status='ok',count=250)))
 print(json.dumps(dict(status='ok',count=250)))
"""


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = self.root / "fixture.py"
        self.fixture.write_text(FIXTURE)
        self.reference = self.root / "reference.json"
        c.write_new(self.reference, {"fixture": True})
        self.actions = self.root / "actions.json"
        c.write_new(self.actions, {"fixture": True})
        self.queue = self.root / "queue.json"
        c.write_new(self.queue, {"runs": []})
        self.watcher = self.root / "watcher"
        self.watcher.mkdir()
        c.write_new(self.watcher / "progress.json", {"runs": {}})
        self.config = {
            "root": str(self.root),
            "state_root": str(self.root / "state"),
            "accepted_root": str(self.root / "accepted"),
            "automatic_reference": c.pin(self.reference),
            "actions": c.pin(self.actions),
            "audit_queue": c.pin(self.queue),
            "watcher_state": str(self.watcher),
            "tool_pins": [c.pin(self.fixture)],
            "input_pins": [c.pin(self.actions)],
            "preserved": [],
            "runs": [],
            "pending_pairs": [],
            "env": {},
            "calibration_batch_size": 10,
            "final_stages": [],
        }
        self.path = self.root / "config.json"

    def tearDown(self):
        self.temp.cleanup()

    def engine(self):
        if not self.path.exists():
            c.write_new(self.path, self.config)
        return s.Supervisor(self.path, self.config, self.root / "state")

    def stage(self, name="join", mode="ok"):
        output = self.root / (name + ".json")
        return {
            "name": name,
            "argv": [sys.executable, str(self.fixture), mode, str(output)],
            "guard": False,
            "retry": "never",
            "checks": [{"path": str(output), "json": {"status": "ok", "count": 250}}],
        }

    def await_finished(self, engine, timeout=5):
        deadline = time.monotonic() + timeout
        while engine.state["active"] is not None and time.monotonic() < deadline:
            engine.finish_active()
            time.sleep(0.03)
        self.assertIsNone(engine.state["active"])

    def source(self, n):
        return {
            "run_id": "run-%03d" % n,
            "ordinal": n,
            "account_id": "account-%03d" % n,
            "ghost_sha256": "a" * 64,
            "record_time_ms": 20000,
            "timing_class": "direct_checkpoint_notices" if n <= 158 else "conditional_control_reflection",
        }

    def accepted(self, n):
        run = self.source(n)
        folder = self.root / "accepted" / run["run_id"]
        folder.mkdir(parents=True)
        (folder / "video.webm").write_bytes(b"fixture-not-real-video")
        c.write_new(
            folder / "result.json",
            {
                "status": "native_video_verified",
                "run": run,
                "video": {"sha256": c.sha(folder / "video.webm")},
            },
        )
        return dict(
            run=s.run_identity(run), result=c.pin(folder / "result.json"), video=c.pin(folder / "video.webm")
        )

    def test_generic_worker_positive_and_checked_receipt(self):
        e = self.engine()
        e.launch("join", self.stage())
        self.await_finished(e)
        row = e.state["stages"]["join"]
        self.assertEqual(row["status"], "verified")
        receipt = c.read(c.verified(row["receipt"]))
        self.assertEqual(receipt["returncode"], 0)
        self.assertEqual(len(receipt["outputs"]), 1)

    def test_process_exit_zero_without_required_output_is_rejected(self):
        e = self.engine()
        spec = self.stage()
        spec["checks"][0]["path"] = str(self.root / "absent.json")
        e.launch("join", spec)
        self.await_finished(e)
        self.assertEqual(e.state["stages"]["join"]["status"], "quarantined")

    def test_crash_restart_waits_existing_worker_no_duplicate(self):
        e = self.engine()
        e.launch("join", self.stage(mode="slow"))
        attempt = e.state["active"]["attempt"]
        restarted = self.engine()
        restarted.finish_active()
        self.assertIsNotNone(restarted.state["active"])
        self.assertEqual(restarted.state["active"]["attempt"], attempt)
        self.await_finished(restarted)
        self.assertEqual(len(list((self.root / "state/attempts/join").iterdir())), 1)

    def test_unknown_launch_gap_is_quarantined_without_retry(self):
        e = self.engine()
        folder = self.root / "state/attempts/qa-gap/attempt-0001"
        folder.mkdir(parents=True)
        spec = folder / "spec.json"
        c.write_new(spec, {})
        e.state["active"] = {
            "key": "join",
            "kind": "join",
            "attempt": str(folder),
            "spec": c.pin(spec),
            "number": 1,
        }
        e.save()
        restarted = self.engine()
        restarted.finish_active()
        self.assertEqual(restarted.state["stages"]["join"]["status"], "quarantined")
        self.assertFalse((folder / "worker.json").exists())

    def test_one_bad_qa_does_not_block_next_independent_run(self):
        for n in (1, 2):
            run = self.source(n)
            out = self.root / ("audit" + str(n))
            out.mkdir()
            c.write_new(out / "source.json", run)
            c.write_new(out / "review-draft.json", {"run_id": run["run_id"], "ordinal": n})
            for role in ("clean", "clock"):
                c.write_new(out / (role + ".json"), {"fixture": True})
            self.config["runs"].append(run)
            self.config["pending_pairs"].append(
                dict(
                    source=run,
                    run_id=run["run_id"],
                    ordinal=n,
                    output=str(out),
                    clean_candidate=str(out / "clean.json"),
                    clock_candidate=str(out / "clock.json"),
                )
            )
        self.config["qa"] = {
            "name": "qa",
            "argv": [
                sys.executable,
                str(self.fixture),
                "qa",
                "{audit_dir}/automatic-policy-v1/acceptance-receipt.json",
                "{run_id}",
                "{ordinal}",
                "{accepted_dir}",
            ],
            "guard": False,
            "retry": "never",
            "checks": [
                {
                    "path": "{audit_dir}/automatic-policy-v1/acceptance-receipt.json",
                    "json": {"schema_version": 2, "status": "accepted_calibration_join_still_required"},
                }
            ],
        }
        e = self.engine()
        self.assertEqual(e.tick(), "running")
        self.await_finished(e)
        self.assertEqual(e.state["runs"]["run-001"]["status"], "quarantined")
        self.assertEqual(e.tick(), "running")
        self.await_finished(e)
        self.assertEqual(e.state["runs"]["run-002"]["status"], "automatic_accepted")
        self.assertEqual(e.tick(), "blocked")

    def test_preserved_video_mutation_rejected_before_new_work(self):
        self.config["runs"] = [self.source(1)]
        self.config["preserved"] = [self.accepted(1)]
        e = self.engine()
        e.preserve()
        Path(self.config["preserved"][0]["video"]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Pinned bytes changed"):
            e.preserve()

    def test_small_cohort_local_only_completion(self):
        self.config["runs"] = [self.source(i) for i in range(1, 4)]
        self.config["preserved"] = [self.accepted(i) for i in range(1, 4)]
        self.config["final_stages"] = [self.stage(name) for name in s.FINAL_ORDER[:4]]
        e = self.engine()
        e.preserve()
        for name in s.FINAL_ORDER[:4]:
            self.assertEqual(e.tick(), "running")
            self.assertEqual(e.state["active"]["kind"], name)
            self.await_finished(e)
        self.assertEqual(e.tick(), "complete")
        document = c.read(self.root / "state/completed-accepted-set.json")
        self.assertEqual(document["requested_count"], 3)
        self.assertEqual(document["source_timing_classes"], {"direct_checkpoint_notices": 3})
        self.assertEqual(e.state["status"], "complete_dataset_verified")
        self.assertNotIn("transfer", e.state["stages"])

    def test_full250_runs_all_final_stages_in_order(self):
        self.config["runs"] = [self.source(i) for i in range(1, 251)]
        self.config["preserved"] = [self.accepted(i) for i in range(1, 251)]
        self.config["final_stages"] = [self.stage(name) for name in s.FINAL_ORDER]
        e = self.engine()
        e.preserve()
        for name in s.FINAL_ORDER:
            self.assertEqual(e.tick(), "running")
            self.assertEqual(e.state["active"]["kind"], name)
            self.await_finished(e)
        self.assertEqual(e.tick(), "complete")
        completed = c.read(self.root / "state/completed-accepted-set.json")
        self.assertEqual(len(completed["runs"]), 250)
        self.assertEqual(completed["unknown_control_prefix_ordinals"], [])
        restarted = self.engine()
        restarted.preserve()
        self.assertEqual(restarted.tick(), "complete")
        self.assertEqual(len(list((self.root / "state/attempts").glob("*/attempt-*"))), 6)

    def test_incomplete249_never_exports(self):
        self.config["runs"] = [self.source(i) for i in range(1, 251)]
        self.config["preserved"] = [self.accepted(i) for i in range(1, 250)]
        e = self.engine()
        e.preserve()
        with self.assertRaisesRegex(ValueError, "every original identity"):
            e.validate_completed()
        self.assertFalse((self.root / "state/completed-accepted-set.json").exists())

    def test_source_or_code_pin_changed_rejects_resume(self):
        e = self.engine()
        self.fixture.write_text("changed")
        with self.assertRaisesRegex(ValueError, "Pinned bytes changed"):
            e.verify_inputs()

    def test_mutable_calibration_report_is_frozen_as_snapshot(self):
        e = self.engine()
        spec = self.stage("calibration_batch")
        spec["checks"][0]["snapshot"] = True
        e.launch("batch1", spec)
        self.await_finished(e)
        receipt = c.read(c.verified(e.state["calibration_batches"][0]["receipt"]))
        (self.root / "calibration_batch.json").write_text("mutated progress")
        self.assertEqual(c.read(c.verified(receipt["outputs"][0]))["status"], "ok")

    def test_stdout_positive_verification_required(self):
        e = self.engine()
        spec = self.stage("remote_verify")
        spec["checks"] = [{"stdout_json": {"status": "ok", "count": 250}}]
        e.launch("remote_verify", spec)
        self.await_finished(e)
        self.assertEqual(e.state["stages"]["remote_verify"]["status"], "verified")

    def test_dead_guard_never_resumes_live_orphan(self):
        attempt = self.root / "attempt"
        attempt.mkdir()
        c.write_new(
            attempt / "decode-slot.json",
            {
                "phase": "exclusive_decode_ready",
                "stop_requested": True,
                "owner": {"pid": 123, "starttime": "1"},
            },
        )
        with (
            mock.patch.object(w, "alive", return_value=False),
            mock.patch.object(w, "process_group_members", return_value=[{"pid": 124}]),
        ):
            with self.assertRaisesRegex(ValueError, "descendants"):
                w.reconcile_dead_guard(attempt)

    def test_guard_rejects_already_stopped_watcher_and_does_not_resume_it(self):
        # Mock process identity/signal only; no real prepare worker is touched.
        state = self.watcher
        attempt = self.root / "attempt"
        attempt.mkdir()
        found = {"pid": 123, "starttime": "1"}
        with (
            mock.patch.object(w.scheduler, "watcher", return_value=found),
            mock.patch.object(w.scheduler, "validate_state"),
            mock.patch.object(w.scheduler, "identity", return_value=found),
            mock.patch.object(w.scheduler, "require_identity"),
            mock.patch.object(w.scheduler, "process", return_value={"state": "T"}),
            mock.patch.object(w.os, "pidfd_open", return_value=9, create=True),
            mock.patch.object(w.os, "close"),
            mock.patch.object(w.signal, "pidfd_send_signal", create=True) as send,
            mock.patch.object(w, "process_group_members", return_value=[]),
        ):
            with self.assertRaisesRegex(ValueError, "another owner"):
                with w.decode_slot({"config": c.pin(self.queue), "state": str(state)}, attempt):
                    pass
            send.assert_not_called()

    def test_singleton_flock_prevents_duplicate_owner(self):
        path = self.root / "singleton.lock"
        with path.open("a") as first, path.open("a") as second:
            import fcntl

            fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_worker_does_not_act_before_durable_launch_admission(self):
        attempt = self.root / "barrier"
        attempt.mkdir()
        spec = self.stage()
        spec.update(inputs=[], cwd=str(self.root), env={})
        c.write_new(attempt / "spec.json", spec)
        child = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(s.HERE / "dataset_pipeline_worker_v1.py"),
                str(attempt / "spec.json"),
                "--attempt",
                str(attempt),
            ],
            cwd=self.root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 3
            while not (attempt / "worker.json").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((attempt / "worker.json").exists())
            time.sleep(0.1)
            self.assertFalse((self.root / "join.json").exists())
            self.assertFalse((attempt / "decode-slot.json").exists())
            active = {"attempt": str(attempt), "spec": c.pin(attempt / "spec.json")}
            self.assertTrue(s.admit_worker(active, self.root))
            self.assertEqual(child.wait(timeout=3), 0)
            self.assertTrue((self.root / "join.json").exists())
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)

    def test_restart_admits_registered_launch_gap_once(self):
        e = self.engine()
        attempt = self.root / "state/attempts/join/attempt-0001"
        attempt.mkdir(parents=True)
        spec = self.stage()
        spec.update(inputs=[], cwd=str(self.root), env={})
        c.write_new(attempt / "spec.json", spec)
        e.state["active"] = {
            "attempt": str(attempt),
            "spec": c.pin(attempt / "spec.json"),
            "key": "join",
            "kind": "join",
            "run_id": None,
            "number": 1,
        }
        e.save()
        child = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(s.HERE / "dataset_pipeline_worker_v1.py"),
                str(attempt / "spec.json"),
                "--attempt",
                str(attempt),
            ],
            cwd=self.root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 3
            while not (attempt / "worker.json").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            restarted = self.engine()
            restarted.finish_active()
            self.assertTrue((attempt / "launch-admitted.json").exists())
            child.wait(timeout=3)
            self.await_finished(restarted)
            self.assertEqual(restarted.state["stages"]["join"]["status"], "verified")
            self.assertEqual(len(list(attempt.parent.iterdir())), 1)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)

    def test_one_stage_smoke_cli_uses_same_receipt_gate(self):
        spec = self.stage()
        spec.update(inputs=[], cwd=str(self.root), env={})
        source = self.root / "one-spec.json"
        c.write_new(source, spec)
        self.assertEqual(s.single_stage(source, self.root / "one-attempt"), 0)
        receipt = c.read(self.root / "one-attempt/receipt.json")
        self.assertEqual(receipt["status"], "stage_verified")
        self.assertTrue((self.root / "one-attempt/launch-admitted.json").exists())

    def compile_fixture(self):
        runs = [self.source(i) for i in range(1, 251)]
        c.atomic(self.actions, {"status": "actions_verified", "requested_count": 250, "runs": runs})
        native = self.root / "native.json"
        c.write_new(
            native, {"runs": [{"run": r, "source_clock_evidence_class": r["timing_class"]} for r in runs]}
        )
        items = [
            dict(
                run_id=r["run_id"],
                ordinal=r["ordinal"],
                output=str(self.root / "audit" / r["run_id"]),
                clean_candidate="clean",
                clock_candidate="clock",
            )
            for r in runs[19:]
        ]
        c.atomic(self.queue, {"manifest": c.pin(self.actions), "runs": items})
        for n in range(1, 20):
            self.accepted(n)
        finals = [self.stage(name) for name in s.FINAL_ORDER]
        for stage in finals:
            stage["guard"] = stage["name"] in ("join", "export")
            if stage["name"] == "transfer":
                stage["argv"] = ["rsync", "--checksum", "--partial", "from/", "host:to/"]
        proposal = dict(
            root=str(self.root),
            actions=str(self.actions),
            audit_queue=str(self.queue),
            native_cohort=str(native),
            accepted_root=str(self.root / "accepted"),
            minimum_preserved=19,
            expected_preserved_ordinals=list(range(1, 20)),
            tool_paths=[],
            input_paths=[],
            watcher_state=str(self.watcher),
            state_root=str(self.root / "state"),
            automatic_reference=str(self.reference),
            qa={"guard": True, "retry": "never"},
            final_stages=finals,
        )
        return proposal

    def test_config_freezes_all250_ids_and_exact_preserved_set(self):
        proposal = self.compile_fixture()
        path = self.root / "proposal.json"
        c.write_new(path, proposal)
        s.compile_config(path, self.path)
        configured = c.read(self.path)
        self.assertEqual(len(configured["runs"]), 250)
        self.assertEqual(len(configured["pending_pairs"]), 231)
        self.assertEqual(len(configured["preserved"]), 19)
        for ref in configured["tool_pins"] + configured["input_pins"]:
            c.verified(ref)

    def test_config_rejects_destructive_transfer(self):
        proposal = self.compile_fixture()
        proposal["final_stages"][4]["argv"].append("--delete")
        path = self.root / "proposal.json"
        c.write_new(path, proposal)
        with self.assertRaisesRegex(ValueError, "without deletion"):
            s.compile_config(path, self.path)
        self.assertFalse(self.path.exists())

    def test_config_rejects_unguarded_qa(self):
        proposal = self.compile_fixture()
        proposal["qa"]["guard"] = False
        path = self.root / "proposal.json"
        c.write_new(path, proposal)
        with self.assertRaisesRegex(ValueError, "shared decode gate"):
            s.compile_config(path, self.path)

    def test_config_rejects_missing_source_identity(self):
        proposal = self.compile_fixture()
        actions = c.read(self.actions)
        actions["runs"].pop()
        c.atomic(self.actions, actions)
        path = self.root / "proposal.json"
        c.write_new(path, proposal)
        with self.assertRaisesRegex(ValueError, "cardinality"):
            s.compile_config(path, self.path)

    def test_config_preserves_prior_automatic_receipt_and_evidence(self):
        proposal = self.compile_fixture()
        run = self.source(20)
        accepted = self.accepted(20)
        folder = self.root / "audit" / run["run_id"] / "automatic-policy-v1"
        folder.mkdir(parents=True)
        review = folder / "automatic-review.json"
        c.write_new(review, {"run_id": run["run_id"], "fixture": True})
        c.write_new(
            folder / "acceptance-receipt.json",
            {
                "schema_version": 2,
                "review_mode": s.AUTO_MODE,
                "status": "accepted_calibration_join_still_required",
                "run_id": run["run_id"],
                "ordinal": 20,
                "automatic_review": c.pin(review),
                "accepted_result": accepted["result"],
                "evidence_files": [c.pin(review)],
            },
        )
        proposal["minimum_preserved"] = 20
        proposal["expected_preserved_ordinals"] = list(range(1, 21))
        path = self.root / "proposal.json"
        c.write_new(path, proposal)
        s.compile_config(path, self.path)
        configured = c.read(self.path)
        entry = next(x for x in configured["preserved"] if x["run"]["ordinal"] == 20)
        self.assertEqual(entry["automatic_evidence"]["review"], c.pin(review))
        engine = s.Supervisor(self.path, configured, self.root / "state")
        engine.preserve()
        review.write_text("changed")
        with self.assertRaisesRegex(ValueError, "Pinned bytes changed"):
            engine.preserve()

    def test_qa_spec_is_guarded_and_does_not_launch(self):
        proposal = self.compile_fixture()
        proposal["qa"] = {
            "name": "qa",
            "guard": True,
            "retry": "never",
            "argv": [
                sys.executable,
                "-B",
                str(self.fixture),
                "qa",
                "{audit_dir}/automatic-policy-v1/acceptance-receipt.json",
            ],
            "checks": [],
        }
        item = c.read(self.queue)["runs"][0]
        folder = Path(item["output"])
        folder.mkdir(parents=True)
        c.write_new(folder / "review-draft.json", {"run_id": item["run_id"], "ordinal": 20})
        for key in ("clean_candidate", "clock_candidate"):
            item[key] = str(folder / (key + ".json"))
            c.write_new(item[key], {"fixture": True})
        q = c.read(self.queue)
        q["runs"][0] = item
        c.atomic(self.queue, q)
        source = self.root / "proposal.json"
        c.write_new(source, proposal)
        output = self.root / "smoke.json"
        s.qa_spec(source, 20, output)
        spec = c.read(output)
        self.assertEqual(spec["guard"]["config"], c.pin(self.queue))
        self.assertFalse((folder / "automatic-policy-v1").exists())


if __name__ == "__main__":
    unittest.main()
