#!/usr/bin/env python3
"""Run one already-reviewed acceptance between local preparation jobs.

V3 recognizes Python interpreter options (including the authorized -B launcher),
pins the exact script/config/cwd/PID/start time, and signals through a Linux pidfd.
Only this local watcher is paused; its current preparation child finishes first.
An advisory state-directory lock serializes v3 acceptance wrappers. No game,
GUI, remote host, frozen preparation code or acceptance policy is changed.
"""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


PROC = Path("/proc")
WATCHER = Path(__file__).with_name("prepare_audit_queue_conditional_v2.py").resolve()


def process(pid):
    """Read Linux stat fields without mistaking spaces/parentheses in comm."""
    try:
        fields = (PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "state": fields[0], "ppid": int(fields[1]), "starttime": fields[19]}
    except (OSError, ValueError, IndexError):
        return None


def children(pid):
    return [
        int(p.name)
        for p in PROC.iterdir()
        if p.name.isdigit()
        and (info := process(int(p.name))) is not None
        and info["ppid"] == pid
        and info["state"] not in ("Z", "X")
    ]


def script_arguments(args):
    """Find Python's script position; never scan -c/-m payloads for a filename."""
    if not args or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(args[0]).name):
        return None
    i = 1
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return args[i + 1 :] or None
        if not arg.startswith("-"):
            return args[i:]
        if arg in ("-W", "-X"):
            if i + 1 >= len(args):
                return None
            i += 2
            continue
        if arg.startswith(("-W", "-X")) and len(arg) > 2:
            i += 1
            continue
        if re.fullmatch(r"-[bBEIOPqRsSuvx]+", arg):
            i += 1
            continue
        # -c, -m, stdin, help/version, or an unknown option is not this script.
        return None
    return None


def identity(pid):
    before = process(pid)
    if before is None or before["state"] in ("Z", "X"):
        return None
    try:
        directory = PROC / str(pid)
        args = [arg.decode() for arg in (directory / "cmdline").read_bytes().split(b"\0") if arg]
        cwd = str((directory / "cwd").resolve(strict=True))
    except (OSError, UnicodeError):
        return None
    after = process(pid)
    if after is None or after["state"] in ("Z", "X") or before["starttime"] != after["starttime"]:
        return None
    return {"pid": pid, "starttime": after["starttime"], "argv": args, "cwd": cwd}


def watcher(config):
    matches = []
    for directory in PROC.iterdir():
        if not directory.name.isdigit():
            continue
        found = identity(int(directory.name))
        if found is None:
            continue
        args = script_arguments(found["argv"])
        if args is None or len(args) < 3 or args[1] != "run":
            continue
        cwd = Path(found["cwd"])
        if (cwd / args[0]).resolve() == WATCHER and (cwd / args[2]).resolve() == config:
            matches.append(found)
    if len(matches) > 1:
        raise ValueError("More than one local preparation watcher owns this config")
    return matches[0] if matches else None


def require_identity(expected):
    if identity(expected["pid"]) != expected:
        raise RuntimeError("Watcher process identity changed")


def read_progress(path):
    # The frozen watcher rewrites this JSON in place. Only syntax/empty-file
    # races are retried; semantic mismatches and I/O errors still fail closed.
    deadline = time.monotonic() + 2
    for attempt in range(21):
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            remaining = deadline - time.monotonic()
            if attempt == 20 or remaining <= 0:
                raise
            time.sleep(min(0.1, remaining))


def validate_state(config, state_directory, found):
    document = json.loads(config.read_text())
    state = read_progress(state_directory / "progress.json")
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    if state["config"]["sha256"] != digest:
        raise ValueError("Watcher state config differs")
    if found is not None:
        args = script_arguments(found["argv"])[3:]
        states = []
        for i, arg in enumerate(args):
            if arg == "--state" and i + 1 < len(args):
                states.append(args[i + 1])
            elif arg.startswith("--state="):
                states.append(arg.split("=", 1)[1])
        if len(states) != 1 or (Path(found["cwd"]) / states[0]).resolve() != state_directory:
            raise ValueError("Watcher state directory differs")
    else:
        if set(state["runs"]) != {run["run_id"] for run in document["runs"]}:
            raise ValueError("Missing watcher has unsettled source pairs")
        if not all(run["status"] in ("prepared_review_required", "failed") for run in state["runs"].values()):
            raise ValueError("Missing watcher has active preparations")
    return digest


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def accept_serialized(a, config, review, state_directory):
    found = watcher(config)
    digest = validate_state(config, state_directory, found)
    settled_lock = None
    descriptor = None
    stop_requested = False
    report = {
        "schema_version": 1,
        "scope": "local_preparation_acceptance_serialization_only",
        "watcher_pid": found["pid"] if found else None,
        "watcher_identity": found,
        "watcher_config": str(config),
        "watcher_config_sha256": digest,
        "review": str(review),
        "scheduler_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "watcher_already_settled": found is None,
        "watcher_resumed": False,
        "started_utc": utc(),
        "signal_method": "pidfd" if found else None,
    }
    proof = review.parent / "local-accept-scheduling.json"
    # Exclusive creation also preserves a crash/interruption as an attempt.
    with proof.open("x") as receipt:
        try:
            deadline = time.monotonic() + 900
            if found is None:
                # A newly starting watcher cannot enter while settled acceptance runs.
                settled_lock = (state_directory / "supervisor.lock").open("a")
                fcntl.flock(settled_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if watcher(config) is not None:
                    raise RuntimeError("Watcher appeared after settled-state validation")
            else:
                descriptor = os.pidfd_open(found["pid"])
                require_identity(found)
                if watcher(config) != found:
                    raise RuntimeError("Watcher ownership changed before pause")
                current = process(found["pid"])
                if current is None or current["state"] in ("T", "t"):
                    raise RuntimeError("Watcher is missing or already stopped by another owner")
                signal.pidfd_send_signal(descriptor, signal.SIGSTOP)
                stop_requested = True
                # SIGSTOP delivery is asynchronous. Await it before checking children.
                while True:
                    require_identity(found)
                    current = process(found["pid"])
                    if current is not None and current["state"] == "T":
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError("Local preparation watcher did not stop")
                    time.sleep(0.05)
                while children(found["pid"]):
                    require_identity(found)
                    if time.monotonic() > deadline:
                        raise TimeoutError("Current local preparation did not settle")
                    time.sleep(1)
                require_identity(found)
            if hashlib.sha256(config.read_bytes()).hexdigest() != digest:
                raise RuntimeError("Watcher config changed before acceptance")
            report["preparation_children_settled"] = True
            args = [
                sys.executable,
                "-B",
                str(Path(__file__).with_name("production_video_audit_conditional_v2.py")),
                "accept",
                str(review),
                "--accepted",
                str(a.accepted.resolve()),
            ]
            completed = subprocess.run(args)
            report["acceptance_exit_code"] = completed.returncode
            return completed.returncode
        except BaseException as exc:
            report["failure"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            try:
                if stop_requested:
                    if identity(found["pid"]) is not None:
                        require_identity(found)
                        signal.pidfd_send_signal(descriptor, signal.SIGCONT)
                        report["watcher_resumed"] = True
                    else:
                        report["watcher_exited_before_resume"] = True
            except BaseException as exc:
                report["resume_failure"] = {"type": type(exc).__name__, "message": str(exc)}
                raise
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                if settled_lock is not None:
                    settled_lock.close()
                report["finished_utc"] = utc()
                json.dump(report, receipt, indent=2)
                receipt.write("\n")
                receipt.flush()
                os.fsync(receipt.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("watcher-config", "watcher-state", "review", "accepted"):
        parser.add_argument("--" + name, type=Path, required=True)
    a = parser.parse_args(argv)
    config = a.watcher_config.resolve(strict=True)
    review = a.review.resolve(strict=True)
    state_directory = a.watcher_state.resolve(strict=True)
    with (state_directory / "acceptance-v3.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return accept_serialized(a, config, review, state_directory)


if __name__ == "__main__":
    raise SystemExit(main())
