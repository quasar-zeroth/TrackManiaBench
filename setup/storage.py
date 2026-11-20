#!/usr/bin/env python3
"""Data-only render-machine operations for the fixed-slot queue; no desktop input.

Requests arrive on stdin. Paths are restricted to this pipeline's one slot,
fixed readback and archive. Existing unknown files are never replaced.
"""

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

DOCS = Path(os.environ["TRACKMANIABENCH_GAME_DOCUMENTS"])
DATA = Path(os.environ["TRACKMANIABENCH_REMOTE_DATA"])
OWNER = "trackmania-dataset-fixed-slot-v1"
REQUESTED_VIDEO_NAME = "DatasetQueueVideo.webm"


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def safe(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,90}", value),
        "Unsafe transaction name",
    )
    return value


def atomic(path, data, replace=False):
    require(replace or not path.exists(), "Refuse existing destination: " + str(path))
    temp = path.with_name(path.name + ".partial")
    with temp.open("xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path, value, replace=False):
    atomic(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode(), replace)


def read(path):
    return json.loads(path.read_text())


def plain(path):
    require(not path.is_symlink(), "Symlink is not an owned regular path: " + str(path))
    if path.exists():
        require(path.is_file(), "Expected regular file: " + str(path))


def identity(expected):
    proc = Path("/proc") / str(int(expected["pid"]))
    require(proc.stat().st_uid == os.getuid(), "Game owner changed")
    start = proc.joinpath("stat").read_text().rsplit(")", 1)[1].split()[19]
    require(str(expected["process_start"]) == start, "Game process lifetime changed")
    # Read only argv[0], never auth-related command lines or environment.
    argv0 = proc.joinpath("cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
    require(
        argv0.replace("\\", "/").rsplit("/", 1)[-1].lower() == "trackmania.exe",
        "Writer is no longer Trackmania",
    )
    return expected


def inventory():
    folder = DATA / "render_staging/videos"
    require(folder.is_dir(), "Native video directory is missing")
    result = {}
    for path in folder.iterdir():
        if path.is_file():
            plain(path)
            stat = path.stat()
            result[path.name] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "inode": stat.st_ino}
    return result


def preserve_copy_no_replace(source, destination, digest, expected_stat):
    """Exclusive copy, fsync and verification before removing this owned source.

    This deliberately makes no atomic-rename claim. A failed partial copy remains
    visible and blocks automatic retry instead of replacing existing evidence.
    """
    lock_path = DATA / "native-fixed-slot/.video-preservation.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plain(source)
        plain(destination)
        now = source.stat()
        require(
            dict(size=now.st_size, mtime_ns=now.st_mtime_ns, inode=now.st_ino) == expected_stat
            and sha(source) == digest,
            "Owned source changed before exclusive preservation",
        )
        with source.open("rb") as src, destination.open("xb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        directory = os.open(destination.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        after = source.stat()
        require(
            dict(size=after.st_size, mtime_ns=after.st_mtime_ns, inode=after.st_ino) == expected_stat
            and sha(source) == digest == sha(destination),
            "Source or exclusive preserved copy changed",
        )
        source.unlink()
        directory = os.open(destination.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def fresh_video_name(before, after, expected):
    require(re.fullmatch(r"Video[0-9]{2,}\.webm", expected), "Invalid expected native video name")
    require(
        expected not in before and REQUESTED_VIDEO_NAME not in before,
        "Expected or fixed requested output already existed before rendering",
    )
    added = set(after) - set(before)
    require(
        len(added) == 1 and next(iter(added)) in (expected, REQUESTED_VIDEO_NAME),
        "Expected exactly one fresh native video under the declared or fixed requested name",
    )
    require(all(after.get(k) == v for k, v in before.items()), "Pre-existing native output changed")
    return next(iter(added))


def context(a):
    slotdir = DOCS / "Replays/Clips/DatasetQueue"
    slot = slotdir / "DatasetSlot.Clip.Gbx"
    marker = slotdir / ".dataset-fixed-slot-owner.json"
    readback = DOCS / "Replays/Clips/DatasetReadback.Clip.Gbx"
    archive = DATA / "native-fixed-slot" / safe(a["queue_id"])
    for p in (slotdir, archive):
        require(not p.is_symlink(), "Owned directory must not be a symlink")
    return slotdir, slot, marker, readback, archive


def dispatch(a):
    operation = a["operation"]
    if operation == "identity":
        return identity(a["identity"])
    if operation == "inventory":
        return inventory()
    slotdir, slot, marker, readback, archive = context(a)
    if operation == "init":
        require(DOCS.is_dir() and DATA.is_dir(), "Expected game/data roots are missing")
        if not slotdir.exists():
            slotdir.mkdir()
            write_json(marker, {"owner": OWNER, "slot_sha256": None, "pending": None})
        elif not marker.exists():
            require(
                set(p.name for p in slotdir.iterdir()) == {slot.name},
                "Cannot adopt a nonempty unknown input folder",
            )
            plain(slot)
            require(
                a.get("initial_seed_sha256") and sha(slot) == a["initial_seed_sha256"],
                "Unowned initial slot differs from the explicitly pinned seed",
            )
            write_json(marker, {"owner": OWNER, "slot_sha256": sha(slot), "pending": None})
        require(marker.is_file() and read(marker).get("owner") == OWNER, "Unowned slot directory")
        require(
            set(p.name for p in slotdir.iterdir()) <= {slot.name, marker.name},
            "Unexpected files in slot directory",
        )
        archive.mkdir(parents=True, exist_ok=False)
        tools = archive / "tools"
        tools.mkdir()
        require(
            set(a["tools"]) == {"native_ui_queue.py", "render_queue.py"}, "Closure tool bundle is incomplete"
        )
        for name, item in a["tools"].items():
            data = base64.b64decode(item["data"], validate=True)
            require(hashlib.sha256(data).hexdigest() == item["sha256"], "Closure tool hash differs")
            atomic(tools / name, data)
        return {"slot": str(slot), "readback": str(readback), "archive": str(archive)}
    require(read(marker).get("owner") == OWNER and archive.is_dir(), "Uninitialized owned slot/archive")
    state = read(marker)
    plain(slot)
    plain(readback)
    if operation == "stage":
        require(state.get("pending") is None, "Prior slot transaction requires explicit reconciliation")
        require(not readback.exists(), "Fixed native readback already exists; never overwrite it")
        require(
            (sha(slot) if slot.exists() else None) == state.get("slot_sha256"),
            "Slot bytes changed outside the queue",
        )
        job = safe(a["job_id"])
        out = archive / job
        out.mkdir(exist_ok=False)
        data = base64.b64decode(a["data"], validate=True)
        require(
            len(data) >= 32 and hashlib.sha256(data).hexdigest() == a["sha256"],
            "Source clip transfer hash differs",
        )
        stage = {
            "queue_id": a["queue_id"],
            "job_id": job,
            "source_sha256": a["sha256"],
            "staged_time_ns": time.time_ns(),
        }
        atomic(out / "intended.Clip.Gbx", data)
        atomic(slot, data, replace=True)
        require(sha(slot) == a["sha256"], "Staged slot hash differs")
        state.update(slot_sha256=a["sha256"], pending=None if a.get("seed_only") else stage)
        write_json(marker, state, replace=True)
        write_json(out / "staged.json", stage)
        return dict(stage, slot=str(slot), archive=str(out), seed_only=bool(a.get("seed_only")))
    job = safe(a["job_id"])
    out = archive / job
    pending = state.get("pending") or {}
    require(
        pending.get("queue_id") == a["queue_id"] and pending.get("job_id") == job,
        "Wrong active slot transaction",
    )
    require(sha(slot) == state["slot_sha256"], "Slot changed during transaction")
    identity(a["identity"])
    if operation == "archive_readback":
        require(readback.is_file(), "Expected fresh native readback was not created")
        require(
            readback.stat().st_mtime_ns >= state["pending"]["staged_time_ns"],
            "Readback predates this staged job",
        )
        observed = []
        for n in range(2):
            if n:
                time.sleep(1.2)
            observed.append((sha(readback), readback.stat().st_size, readback.stat().st_mtime_ns))
        require(observed[0] == observed[1], "Native readback is still changing")
        destination = out / "native-readback.Clip.Gbx"
        require(not destination.exists(), "Readback archive already exists")
        closure = out / "readback-closure.json"
        subprocess.run(
            [
                "python3",
                str(archive / "tools/native_ui_queue.py"),
                "closure",
                str(readback),
                str(closure),
                "--writer-pid",
                str(a["identity"]["pid"]),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        require(read(closure)["video"]["sha256"] == observed[0][0], "Readback changed before writer closure")
        # Different filesystems: copy, fsync, verify, then remove ONLY this owned export.
        atomic(destination, readback.read_bytes())
        require(sha(destination) == observed[0][0] == sha(readback), "Readback archive copy differs")
        identity(a["identity"])
        readback.unlink()
        return {
            "path": str(destination),
            "sha256": observed[0][0],
            "bytes": observed[0][1],
            "closure": str(closure),
        }
    if operation == "commit":
        proof = a["result"]
        require(
            proof["readback_sha256"] == sha(out / "native-readback.Clip.Gbx"),
            "Committed readback hash differs",
        )
        if proof.get("video_sha256"):
            require(proof["video_sha256"] == sha(out / "native-video.webm"), "Committed video hash differs")
        write_json(out / "committed.json", proof)
        state["pending"] = None
        write_json(marker, state, replace=True)
        return {"committed": job}
    raise ValueError("Unsupported storage operation")


V8_OWNER = "trackmania-native-empty-staging-v8"
_dispatch_v5 = dispatch


def canonical_dir():
    path = DATA / "native-preserved/videos"
    require(
        path.is_dir() and not path.is_symlink() and not path.parent.is_symlink(),
        "Preserved v8 directory is missing or linked",
    )
    require(read(path.parent / "owner.json") == {"owner": V8_OWNER}, "Preserved directory ownership differs")
    return path


def stat_record(path):
    stat = path.stat()
    return dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns, inode=stat.st_ino)


def exclusive_copy(source, destination, digest):
    plain(source)
    plain(destination)
    require(not destination.exists(), "Refuse existing preservation destination: " + str(destination))
    before = stat_record(source)
    require(sha(source) == digest, "Preservation source hash differs")
    with source.open("rb") as src, destination.open("xb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    descriptor = os.open(destination.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    require(
        stat_record(source) == before and sha(source) == sha(destination) == digest,
        "Preservation source or copy changed",
    )


def closed_video(source, proof, archive, expected_identity):
    identity(expected_identity)
    require(not proof.exists(), "Writer closure already exists; reconcile explicitly")
    subprocess.run(
        [
            "python3",
            str(archive / "tools/native_ui_queue.py"),
            "closure",
            str(source),
            str(proof),
            "--writer-pid",
            str(expected_identity["pid"]),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    identity(expected_identity)
    result = read(proof)
    require(result["video"]["sha256"] == sha(source), "Native source changed after closure")
    return result["video"]["sha256"]


def checked_pending(a):
    slotdir, slot, marker, readback, archive = context(a)
    state = read(marker)
    require(state.get("owner") == OWNER and archive.is_dir(), "Unowned transaction")
    pending = state.get("pending") or {}
    require(
        pending.get("queue_id") == a["queue_id"] and pending.get("job_id") == a["job_id"],
        "Wrong pending transaction",
    )
    require(sha(slot) == state["slot_sha256"] == pending["source_sha256"], "Pending slot changed")
    identity(a["identity"])
    return archive, archive / safe(a["job_id"]), state


def fresh_empty_name(before, after):
    require(before == {}, "v8 requires empty pre-render staging")
    require(len(after) == 1, "v8 requires exactly one fresh native export")
    name = next(iter(after))
    require(
        name == REQUESTED_VIDEO_NAME or re.fullmatch(r"Video[0-9]{2,}\.webm", name),
        "Unexpected fresh native filename",
    )
    return name


def harvest_empty(a):
    archive, out, state = checked_pending(a)
    before = a["inventory"]
    after = inventory()
    actual_name = fresh_empty_name(before, after)
    name = a["video_name"]
    require(re.fullmatch(r"Video[0-9]{2,}\.webm", name), "Invalid v8 logical video number")
    source = DATA / "render_staging/videos" / actual_name
    canonical = canonical_dir() / name
    proof = out / "video-closure.json"
    destination = out / "native-video.webm"
    receipt = out / "video-name-receipt.json"
    require(
        not any(p.exists() for p in (canonical, proof, destination, receipt)),
        "Harvest evidence already exists; reconcile explicitly",
    )
    require(
        source.stat().st_mtime_ns >= state["pending"]["staged_time_ns"],
        "Fresh source predates the staged scene",
    )
    digest = closed_video(source, proof, archive, a["identity"])
    require(inventory() == after, "Staging changed during writer closure")
    exclusive_copy(source, destination, digest)
    exclusive_copy(source, canonical, digest)
    require(inventory() == after and sha(source) == digest, "Source changed before owned staging removal")
    source.unlink()
    descriptor = os.open(source.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    require(inventory() == {}, "Staging did not become empty after preservation")
    record = dict(
        schema_version=1,
        status="native_video_preserved_from_empty_staging",
        queue_id=a["queue_id"],
        job_id=a["job_id"],
        identity=a["identity"],
        rendered_name=actual_name,
        canonical_name=name,
        native_original_source=dict(path=str(source), sha256=digest),
        preserved_source=dict(path=str(canonical), sha256=digest),
        archived_video=dict(path=str(destination), sha256=digest),
        writer_closure=dict(path=str(proof), sha256=sha(proof)),
        preservation_writer_closure=dict(path=str(proof), sha256=sha(proof)),
        pre_render_inventory={},
        post_render_source_stat=after[actual_name],
        post_preservation_stat=stat_record(canonical),
        post_preservation_staging_inventory={},
        native_bytes_modified=False,
        prior_preserved_outputs_replaced=False,
        preservation_method="two_exclusive_copies_fsync_hash_verify_then_unlink_closed_fresh_source",
        storage_verifier_sha256=a["storage_verifier_sha256"],
    )
    write_json(receipt, record)
    return dict(
        path=str(destination),
        sha256=digest,
        closure=str(proof),
        source=str(canonical),
        native_original_source=record["native_original_source"],
        preservation_closure=str(proof),
        name_receipt=str(receipt),
        name_receipt_sha256=sha(receipt),
    )


def dispatch(a):
    operation = a["operation"]
    if operation == "init":
        result = _dispatch_v5(a)
        preserved = DATA / "native-preserved"
        if not preserved.exists():
            preserved.mkdir()
            write_json(preserved / "owner.json", {"owner": V8_OWNER})
            (preserved / "videos").mkdir()
        canonical_dir()
        return result
    if operation == "empty_inventory":
        canonical_dir()
        result = inventory()
        require(result == {}, "Refuse render with pre-existing staging output")
        return result
    if operation == "harvest":
        return harvest_empty(a)
    if operation == "verify_commit":
        archive = DATA / "native-fixed-slot" / safe(a["original_queue_id"]) / safe(a["job_id"])
        commit = read(archive / "committed.json")
        require(
            commit.get("result") == a["candidate"]
            and commit.get("video_sha256") == a["video_sha256"] == sha(archive / "native-video.webm")
            and commit.get("readback_sha256")
            == a["readback_sha256"]
            == sha(archive / "native-readback.Clip.Gbx"),
            "Remote committed prefix differs",
        )
        return {"committed_verified": a["job_id"]}
    if operation == "resume_state":
        _, slot, marker, readback, _ = context(a)
        state = read(marker)
        require(
            state.get("owner") == OWNER and state.get("pending") is None,
            "Pending or unowned transaction requires explicit reconciliation",
        )
        require(
            not readback.exists() and sha(slot) == state["slot_sha256"],
            "Unarchived readback or externally changed slot blocks resume",
        )
        require(inventory() == {}, "Nonempty staging blocks resume")
        return {"state": state, "staging_empty": True}
    return _dispatch_v5(a)


if __name__ == "__main__":
    try:
        print(json.dumps(dispatch(json.load(sys.stdin))))
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print("Rejected: " + str(exc), file=sys.stderr)
        raise SystemExit(2)
