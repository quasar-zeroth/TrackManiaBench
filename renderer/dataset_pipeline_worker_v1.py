#!/usr/bin/env python3
"""One durable, argv-only stage; optionally serialize against the live watcher.

The coordinator launches this in its own session. A coordinator crash therefore
does not kill or duplicate its child. PID/start-time and process-group evidence
allow restart to wait for an orphaned child; missing receipts never imply success.
No native render/GUI command belongs in this worker's configuration.
"""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pipeline_common_v1 as c
import serial_accept_review_v3 as scheduler


def process_group_members(pgid):
    result = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            fields = (p / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid and fields[0] not in ("Z", "X"):
                result.append({"pid": int(p.name), "starttime": fields[19]})
        except (OSError, ValueError, IndexError):
            pass
    return result


def alive(identity):
    current = scheduler.process(identity["pid"])
    return bool(
        current and current["state"] not in ("Z", "X") and current["starttime"] == identity["starttime"]
    )


def settle_owned_descendants():
    """Reap/terminate only this worker's session descendants before releasing GPU-independent decode slot."""
    group = os.getpgrp()
    c.require(group == os.getpid(), "Stage worker must own its process group")
    for sig, timeout in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        members = [p for p in process_group_members(group) if p["pid"] != os.getpid()]
        if not members:
            return
        for member in members:
            try:
                fd = os.pidfd_open(member["pid"])
                try:
                    if alive(member):
                        signal.pidfd_send_signal(fd, sig)
                finally:
                    os.close(fd)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not [p for p in process_group_members(group) if p["pid"] != os.getpid()]:
                return
            time.sleep(0.05)
    raise RuntimeError("Owned stage descendants remain alive; decode slot cannot safely resume")


@contextlib.contextmanager
def decode_slot(guard, attempt):
    """Same v3 lock and pidfd handshake; ledger is durable before each signal."""
    if not guard:
        yield
        return
    config = c.verified(guard["config"])
    state = Path(guard["state"]).resolve(strict=True)
    ledger_path = attempt / "decode-slot.json"
    with (state / "acceptance-v3.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        found = scheduler.watcher(config)
        scheduler.validate_state(config, state, found)
        ledger = {
            "schema_version": 1,
            "owner": scheduler.identity(os.getpid()),
            "watcher": found,
            "config": guard["config"],
            "state": str(state),
            "phase": "validated",
            "started_utc": c.now(),
            "stop_requested": False,
        }
        c.atomic(ledger_path, ledger)
        fd = None
        settled = None
        try:
            if found is None:
                settled = (state / "supervisor.lock").open("a")
                fcntl.flock(settled, fcntl.LOCK_EX | fcntl.LOCK_NB)
                c.require(scheduler.watcher(config) is None, "A new watcher appeared")
            else:
                fd = os.pidfd_open(found["pid"])
                scheduler.require_identity(found)
                c.require(
                    scheduler.process(found["pid"])["state"] not in ("T", "t"),
                    "Watcher already stopped by another owner",
                )
                c.require(scheduler.watcher(config) == found, "Watcher identity changed")
                ledger.update(phase="stop_requested", stop_requested=True)
                c.atomic(ledger_path, ledger)
                signal.pidfd_send_signal(fd, signal.SIGSTOP)
                deadline = time.monotonic() + guard.get("timeout_seconds", 900)
                while True:
                    scheduler.require_identity(found)
                    if scheduler.process(found["pid"])["state"] == "T":
                        break
                    c.require(time.monotonic() < deadline, "Watcher pause timed out")
                    time.sleep(0.05)
                ledger["phase"] = "watcher_paused"
                c.atomic(ledger_path, ledger)
                while scheduler.children(found["pid"]):
                    scheduler.require_identity(found)
                    c.require(time.monotonic() < deadline, "Preparation child did not settle")
                    time.sleep(0.2)
                scheduler.require_identity(found)
            c.verified(guard["config"])
            ledger["phase"] = "exclusive_decode_ready"
            c.atomic(ledger_path, ledger)
            yield
        finally:
            try:
                c.require(
                    not [p for p in process_group_members(os.getpgrp()) if p["pid"] != os.getpid()],
                    "Live stage descendants prevent safe watcher resume",
                )
                if ledger["stop_requested"] and scheduler.identity(found["pid"]) is not None:
                    scheduler.require_identity(found)
                    signal.pidfd_send_signal(fd, signal.SIGCONT)
                    ledger["watcher_resumed"] = True
                ledger["phase"] = "released"
                ledger["finished_utc"] = c.now()
                c.atomic(ledger_path, ledger)
            finally:
                if fd is not None:
                    os.close(fd)
                if settled is not None:
                    settled.close()


def reconcile_dead_guard(attempt):
    """Resume only this dead worker's stopped watcher, after all descendants end.

    This is local resource recovery, not a replay of an uncertain acceptance. The
    acceptance remains quarantined unless its immutable successful receipt exists.
    """
    ledger_path = attempt / "decode-slot.json"
    if not ledger_path.exists():
        return
    ledger = c.read(ledger_path)
    if ledger["phase"] == "released" or not ledger["stop_requested"]:
        return
    owner = ledger["owner"]
    c.require(not alive(owner), "Decode owner is still alive")
    c.require(not process_group_members(owner["pid"]), "Orphaned stage descendants remain active")
    recovery = attempt / "decode-slot-recovery.json"
    if recovery.exists():
        old = c.read(recovery)
        c.require(
            old["ledger"] == c.pin(ledger_path) and old["status"] == "dead_owner_local_guard_reconciled",
            "Recovery ledger differs",
        )
        return
    state = Path(ledger["state"])
    with (state / "acceptance-v3.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        found = ledger["watcher"]
        resumed = False
        if scheduler.identity(found["pid"]) is not None:
            scheduler.require_identity(found)
            c.require(
                scheduler.watcher(c.verified(ledger["config"])) == found, "Cannot recover changed watcher"
            )
            if scheduler.process(found["pid"])["state"] in ("T", "t"):
                fd = os.pidfd_open(found["pid"])
                try:
                    scheduler.require_identity(found)
                    signal.pidfd_send_signal(fd, signal.SIGCONT)
                    resumed = True
                finally:
                    os.close(fd)
        c.write_new(
            recovery,
            {
                "schema_version": 1,
                "status": "dead_owner_local_guard_reconciled",
                "ledger": c.pin(ledger_path),
                "watcher_resumed": resumed,
                "acceptance_retried": False,
                "finished_utc": c.now(),
            },
        )


def output_checks(spec, attempt):
    outputs = []
    for i, check in enumerate(spec.get("checks", [])):
        if "stdout_json" in check:
            values = []
            for line in (attempt / "stdout.log").read_text().splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    values.append(value)
            c.require(values, "Stage emitted no JSON verification report")
            c.matching(values[-1], check["stdout_json"])
            path = attempt / ("stdout-verification-%02d.json" % i)
            c.write_new(path, values[-1])
            outputs.append(c.pin(path))
            continue
        path = Path(check["path"]).resolve(strict=True)
        if "json" in check:
            c.matching(c.read(path), check["json"])
        if check.get("snapshot"):
            target = attempt / "snapshots" / ("%02d-" % i + path.name)
            target.parent.mkdir(exist_ok=True)
            data = path.read_bytes()
            with target.open("xb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            path = target
        outputs.append(c.pin(path))
    return outputs


def execute(spec_path, attempt):
    spec = c.read(spec_path)
    c.require(attempt.is_dir() and not (attempt / "receipt.json").exists(), "Attempt already finished")
    c.write_new(
        attempt / "worker.json",
        {
            "identity": scheduler.identity(os.getpid()),
            "session_id": os.getsid(0),
            "process_group": os.getpgrp(),
            "spec": c.pin(spec_path),
            "started_utc": c.now(),
        },
    )
    report = {
        "schema_version": 1,
        "spec": c.pin(spec_path),
        "started_utc": c.now(),
        "status": "stage_failed",
        "returncode": None,
        "outputs": [],
    }
    child = None

    def interrupted(signum, frame):
        raise InterruptedError("Stage worker received signal " + str(signum))

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        # The child registers before any signal or command. Its coordinator must
        # durably record this exact identity before admitting the task. A crash
        # in Popen's launch gap therefore cannot leave an untracked mutation.
        admitted = attempt / "launch-admitted.json"
        deadline = time.monotonic() + 300
        while not admitted.exists():
            c.require(time.monotonic() < deadline, "Launch admission was never committed")
            time.sleep(0.05)
        admission = c.read(admitted)
        c.require(
            admission["spec"] == c.pin(spec_path)
            and admission["worker_identity"] == scheduler.identity(os.getpid()),
            "Launch admission belongs to another worker/spec",
        )
        for ref in spec["inputs"]:
            c.verified(ref)
        with decode_slot(spec.get("guard"), attempt):
            with (attempt / "stdout.log").open("xb") as stdout, (attempt / "stderr.log").open("xb") as stderr:
                env = dict(os.environ, **spec.get("env", {}))
                child = subprocess.Popen(
                    spec["argv"],
                    cwd=spec["cwd"],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                )
                c.write_new(
                    attempt / "command-child.json",
                    {
                        "identity": scheduler.identity(child.pid),
                        "argv": spec["argv"],
                        "parent_process_group": os.getpgrp(),
                        "started_utc": c.now(),
                    },
                )
                try:
                    report["returncode"] = child.wait(timeout=spec.get("timeout_seconds", 86400))
                except BaseException:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                    raise
                finally:
                    settle_owned_descendants()
            c.require(report["returncode"] == 0, "Stage exited " + str(report["returncode"]))
            for ref in spec["inputs"]:
                c.verified(ref)
            report["outputs"] = output_checks(spec, attempt)
            report["status"] = "stage_verified"
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        report["finished_utc"] = c.now()
        report["logs"] = [
            c.pin(attempt / name) for name in ("stdout.log", "stderr.log") if (attempt / name).exists()
        ]
        if (attempt / "decode-slot.json").exists():
            report["decode_slot"] = c.pin(attempt / "decode-slot.json")
        c.write_new(attempt / "receipt.json", report)
    return 0 if report["status"] == "stage_verified" else 2


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("spec", type=Path)
    p.add_argument("--attempt", type=Path, required=True)
    a = p.parse_args()
    raise SystemExit(execute(a.spec.resolve(strict=True), a.attempt.resolve(strict=True)))
