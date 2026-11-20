#!/usr/bin/env python3
"""Sequential native Trackmania render supervisor. Legacy loopback plugin support and shared native validators.

Uses Python stdlib + ffmpeg/ffprobe. Game compilation/runtime are a separate pilot gate.
Outputs are native full ghost clips; race-time alignment must be proven downstream.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SETTINGS = {
    "width": 640,
    "height": 360,
    "fps": 50,
    "gamecam": 2,
    "entity": 1,
    "container": "webm",
    "audio": False,
    "native_full_clip": True,
    "hud3d": False,
    "motion_blur": False,
    "dof": False,
    "remanence": 0,
    "sharpen": False,
    "stereo3d": False,
    "tile_split": False,
}
NATIVE_UI_SETTINGS = {
    **SETTINGS,
    "motion_blur": None,
    "dof": None,
    "tile_split": None,
    "antialias": 4,
    "persistence_duration_seconds": 0,
    "quality_preset": "low",
    "fx_tracks": False,
}
BUILD = "dataset-renderer-0.2.0"
NATIVE_UI_BUILD = "native-ui-0.2.0"
SAFE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,119}$")


def safe_name(value):
    if not isinstance(value, str) or not SAFE.fullmatch(value) or ".." in value:
        raise ValueError(f"Unsafe dataset name: {value!r}")
    return value


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def resolved_source(value, manifest_dir, user_dir, game_user_dir):
    text = str(value).replace("\\", "/")
    root = str(game_user_dir).replace("\\", "/").rstrip("/")
    if root and text.lower().startswith(root.lower() + "/"):
        path = user_dir / text[len(root) + 1 :]
    elif re.match(r"^[A-Za-z]:", text):
        raise ValueError("Windows path outside --game-user-dir: " + text)
    else:
        path = Path(text)
        if not path.is_absolute():
            path = manifest_dir / path
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("Not a regular input file: " + str(path))
    return path


def normalise_manifest(path, user_dir, game_user_dir):
    original = json.loads(path.read_text())
    m = original.get("map", {})
    uid = m.get("uid") or original.get("mapUid")
    safe_name(uid)
    source_map = m.get("path") or original.get("mapPath")
    map_path = resolved_source(source_map, path.parent, user_dir, game_user_dir)
    map_sha = sha256(map_path)
    if m.get("sha256") and m["sha256"] != map_sha:
        raise ValueError("Map hash differs from collection manifest")
    runs = []
    for item in original["runs"]:
        rank = int(item["rank"])
        ordinal = int(item.get("ordinal", rank))
        account = item.get("account_id") or item.get("accountId")
        rid = item.get("run_id") or f"rank-{rank:03d}-{account}"
        safe_name(rid)
        ghost = item.get("ghost_path") or item.get("ghostPath")
        ghost_path = resolved_source(ghost, path.parent, user_dir, game_user_dir)
        ghost_sha = sha256(ghost_path)
        if item.get("ghost_sha256") and item["ghost_sha256"] != ghost_sha:
            raise ValueError(f"Ghost hash differs from collection manifest: {rid}")
        milliseconds = int(item.get("record_time_ms", item.get("scoreMs", 0)))
        if not account or rank < 1 or milliseconds <= 0 or ghost_path.stat().st_size < 32:
            raise ValueError(f"Incomplete or invalid run: {rid}")
        runs.append(
            {
                "run_id": rid,
                "rank": rank,
                "ordinal": ordinal,
                "account_id": account,
                "record_time_ms": milliseconds,
                "ghost_path": str(ghost_path),
                "ghost_sha256": ghost_sha,
                "map_record_id": item.get("map_record_id", item.get("mapRecordId")),
            }
        )
    runs.sort(key=lambda r: r["ordinal"])
    for key in ("run_id", "ordinal", "account_id", "ghost_sha256"):
        if len({r[key] for r in runs}) != len(runs):
            raise ValueError(f"Duplicate {key} in manifest")
    return {
        "schema_version": 1,
        "source_manifest_sha256": sha256(path),
        "map": {"uid": uid, "path": str(map_path), "sha256": map_sha},
        "runs": runs,
    }


class Worker:
    def __init__(self, port=29801):
        self.base = f"http://127.0.0.1:{port}"

    def request(self, route, *, read=False, timeout=130, **params):
        url = self.base + route
        if params:
            url += "?" + urlencode(params, safe=":")
        req = Request(url, method="GET" if read else "POST", headers={"X-Dataset-Worker": "1"})
        with urlopen(req, timeout=timeout) as response:
            value = json.load(response)
        if value.get("ok") is False or value.get("err") or value.get("error"):
            raise RuntimeError(f"{route}: {value}")
        return value

    def status(self):
        return self.request("/status", read=True)

    def wait(self, condition, milliseconds=120000):
        return self.request("/await", c=condition, ms=milliseconds, timeout=milliseconds / 1000 + 15)


def copy_verified(source, destination, expected):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and sha256(destination) == expected:
        return
    tmp = destination.with_name(destination.name + ".partial")
    shutil.copyfile(source, tmp)
    if sha256(tmp) != expected:
        raise RuntimeError("Copy verification failed: " + str(source))
    os.replace(tmp, destination)


def snapshot(folder):
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in folder.glob("*.webm")}


def protect_native_exports(folder, backup_root):
    """Native exporter sometimes ignores ShootName and overwrites VideoNN.webm."""
    protected = {}
    for path in folder.glob("*.webm"):
        digest = sha256(path)
        backup = backup_root / (digest + ".webm")
        copy_verified(path, backup, digest)
        protected[str(path)] = {"backup": str(backup), "sha256": digest}
    return protected


def wait_changed(folder, before, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        changed = [Path(p) for p, state in snapshot(folder).items() if before.get(p) != state]
        if len(changed) > 1:
            raise RuntimeError("Multiple native outputs changed; output identity is ambiguous")
        if changed:
            return changed[0]
        time.sleep(0.2)
    raise TimeoutError("No native WebM output changed")


def check_scene(state, run, map_uid):
    if state.get("map_uid") != map_uid or state.get("ghost_blocks") != 1:
        raise ValueError("Loaded map/single-ghost identity check failed")
    if state.get("imported_name") != run["run_id"] + ".Ghost.Gbx":
        raise ValueError("Wrong imported ghost filename")
    blocks = state.get("blocks", [])
    cars = [b for b in blocks if b["type"] == "CGameCtnMediaBlockEntity"]
    cams = [b for b in blocks if b["type"] == "CGameCtnMediaBlockCameraGame"]
    if len(cars) != 1 or len(cams) != 1:
        raise ValueError("Expected exactly one entity and one camera block")
    car, camera = cars[0], cams[0]
    if camera.get("entity") != 1 or camera.get("gamecam") != 2 or not camera.get("target_name"):
        raise ValueError("Camera target is missing or wrong")
    if abs(camera["end_seconds"] - car["end_seconds"]) > 0.021:
        raise ValueError("Camera does not cover the complete entity clip")
    if car["end_seconds"] < run["record_time_ms"] / 1000 - 0.021:
        raise ValueError("Native ghost block is shorter than leaderboard race time")
    if car["start_seconds"] < 0 or camera["start_seconds"] > car["start_seconds"]:
        raise ValueError("Unsupported clip-start timing")
    return {
        "entity": car,
        "camera": camera,
        "expected_native_duration_seconds": max(b["end_seconds"] for b in blocks),
        "frame_zero_race_time_ms": None,
        "alignment_status": "requires_measured_race_clock_calibration",
    }


def video_check(path, expected_duration=None, decode=True):
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-count_frames",
        "-show_streams",
        "-show_format",
        "-show_frames",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,avg_frame_rate,nb_read_frames:"
        "frame=media_type,best_effort_timestamp_time:format=duration,size",
        "-of",
        "json",
        str(path),
    ]
    probe = json.loads(subprocess.check_output(cmd))
    videos = [s for s in probe["streams"] if s["codec_type"] == "video"]
    if len(videos) != 1:
        raise ValueError("Expected one video stream")
    s = videos[0]
    if (s["width"], s["height"]) != (640, 360):
        raise ValueError("Resolution mismatch")
    numerator, denominator = map(int, s["avg_frame_rate"].split("/"))
    if denominator == 0 or abs(numerator / denominator - 50) > 0.00001:
        raise ValueError("Frame-rate mismatch")
    pts = [float(f["best_effort_timestamp_time"]) for f in probe["frames"] if f["media_type"] == "video"]
    if len(pts) < 2 or any(not math.isfinite(t) for t in pts):
        raise ValueError("Missing/nonfinite frame timestamps")
    if abs(pts[0]) > 0.0011:
        raise ValueError("Native video does not start at PTS zero")
    if any(abs(b - a - 0.02) > 0.0011 for a, b in zip(pts, pts[1:])):
        raise ValueError("Missing/nonmonotonic/variable video timestamps")
    duration = pts[-1] - pts[0] + 0.02
    if expected_duration is not None and abs(duration - expected_duration) > 0.061:
        raise ValueError(f"Clip duration mismatch: {duration} vs {expected_duration}")
    if decode:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
            check=True,
            stdin=subprocess.DEVNULL,
        )
    # A correctly sized black/static render is still a failed observation.
    pixels = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            "fps=2,scale=32:18,format=gray",
            "-an",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        stdin=subprocess.DEVNULL,
    )
    samples = [pixels[i : i + 576] for i in range(0, len(pixels), 576)]
    if not samples or any(len(sample) != 576 for sample in samples):
        raise ValueError("Cannot decode content samples")
    means = [sum(sample) / 576 for sample in samples]
    unique = len({hashlib.sha256(sample).digest() for sample in samples})
    black_fraction = sum(mean < 3 for mean in means) / len(means)
    if black_fraction > 0.8 or unique < min(8, max(2, math.ceil(len(samples) / 4))):
        raise ValueError("Video is predominantly black or static")
    return {
        "codec": s["codec_name"],
        "width": s["width"],
        "height": s["height"],
        "fps": 50,
        "frames": len(pts),
        "duration_seconds": duration,
        "first_pts_seconds": pts[0],
        "last_pts_seconds": pts[-1],
        "audio_streams": sum(v["codec_type"] == "audio" for v in probe["streams"]),
        "timestamps_valid": True,
        "decoded_without_errors": decode,
        "content_samples": len(samples),
        "unique_content_samples": unique,
        "black_sample_fraction": black_fraction,
        "sha256": sha256(path),
        "bytes": path.stat().st_size,
    }


def backend_identity(backend="openplanet"):
    if backend == "openplanet":
        return {"renderer_backend": backend, "renderer_build": BUILD, "plugin_build": BUILD}
    if backend == "native_ui":
        return {"renderer_backend": backend, "renderer_build": NATIVE_UI_BUILD, "plugin_build": None}
    raise ValueError("Unknown rendering backend: " + str(backend))


def settings_for_backend(backend="openplanet"):
    backend_identity(backend)
    return dict(NATIVE_UI_SETTINGS if backend == "native_ui" else SETTINGS)


def required_gate_checks(backend="openplanet"):
    backend_identity(backend)
    return [
        "correct_single_ghost",
        "fixed_camera_whole_run",
        "no_hud",
        "render_effect_controls_documented" if backend == "native_ui" else "motion_blur_disabled",
        "frame_race_clock_alignment",
        "finish_covered",
        "repeat_render_consistency",
        "resume_preserves_outputs",
        "action_decode_complete",
    ]


def source_key(manifest, run, backend="openplanet"):
    identity = {
        "map_sha256": manifest["map"]["sha256"],
        "run": run,
        "settings": settings_for_backend(backend),
        "plugin_build": BUILD,
    }
    if backend != "openplanet":
        identity.update(backend_identity(backend))
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def validate_render_result(result, manifest, run, result_dir):
    """Shared identity/evidence check used by gates and the frame/action joiner."""
    backend = result.get("renderer_backend", "openplanet")
    expected_backend = backend_identity(backend)
    if result.get("status") != "native_video_verified" or result.get("settings") != settings_for_backend(
        backend
    ):
        raise ValueError("Render result is incomplete or settings differ")
    if result.get("plugin_build") != expected_backend["plugin_build"]:
        raise ValueError("Render plugin build differs")
    if backend != "openplanet" and any(result.get(k) != v for k, v in expected_backend.items()):
        raise ValueError("Native UI renderer identity differs")
    if any(result.get("map", {}).get(k) != manifest["map"][k] for k in ("uid", "sha256")):
        raise ValueError("Rendered map identity differs")
    if any(
        result.get("run", {}).get(k) != run[k]
        for k in ("run_id", "account_id", "record_time_ms", "ghost_sha256")
    ):
        raise ValueError("Rendered run identity differs")
    if result.get("source_key") != source_key(manifest, result["run"], backend):
        raise ValueError("Render source identity differs")
    video = Path(result_dir) / "video.webm"
    if not video.is_file() or sha256(video) != result.get("video", {}).get("sha256"):
        raise ValueError("Rendered video hash differs")
    if backend == "openplanet":
        return check_scene(result["scene"], run, manifest["map"]["uid"])
    import importlib.util

    module_path = Path(__file__).with_name("native_ui_queue.py")
    spec = importlib.util.spec_from_file_location("native_ui_evidence_validator", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_record_evidence(result, manifest, run, Path(result_dir))


def gate_file(reference, base):
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
        raise ValueError("Pilot gate is missing an evidence file reference")
    file = Path(reference["path"])
    if not file.is_absolute():
        file = base / file
    file = file.resolve(strict=True)
    if not file.is_file() or reference.get("sha256") != sha256(file):
        raise ValueError("Pilot evidence file hash changed: " + str(file))
    return file


def validate_gate(path, manifest, output):
    gate = json.loads(path.read_text())
    if gate.get("passed") is not True or gate.get("map_sha256") != manifest["map"]["sha256"]:
        raise ValueError("Pilot gate missing pass/map identity")
    backend = gate.get("renderer_backend", "openplanet")
    expected_backend = backend_identity(backend)
    required = required_gate_checks(backend)
    if (
        gate.get("settings") != settings_for_backend(backend)
        or gate.get("plugin_build") != expected_backend["plugin_build"]
    ):
        raise ValueError("Pilot settings/build differ from batch")
    if backend != "openplanet" and any(gate.get(k) != v for k, v in expected_backend.items()):
        raise ValueError("Pilot rendering backend identity differs")
    if not gate.get("verified_by") or not gate.get("verified_utc"):
        raise ValueError("Pilot gate must record who verified it and when")
    if not gate.get("evidence_files"):
        raise ValueError("Pilot gate needs hash-pinned evidence files")
    for reference in gate["evidence_files"]:
        gate_file(reference, path.parent)
    for check in required:
        item = gate.get("checks", {}).get(check, {})
        if item.get("passed") is not True or not item.get("evidence"):
            raise ValueError("Pilot gate missing evidence: " + check)
    pilots = gate.get("pilot_runs", [])
    pilot_count = min(5, len(manifest["runs"]))
    if len(pilots) != pilot_count or {p["ordinal"] for p in pilots} != set(range(1, pilot_count + 1)):
        raise ValueError("Batch requires verified pilot leaderboard positions 1–5")
    by_rank = {r["ordinal"]: r for r in manifest["runs"]}
    for pilot in pilots:
        run = by_rank[pilot["ordinal"]]
        if pilot["ghost_sha256"] != run["ghost_sha256"]:
            raise ValueError("Top-five snapshot changed: redo pilot")
        result_path = output / run["run_id"] / "result.json"
        if gate_file(pilot.get("render_result"), path.parent) != result_path.resolve():
            raise ValueError("Pilot result reference is outside its expected run directory")
        result = json.loads(result_path.read_text())
        video = output / run["run_id"] / "video.webm"
        if gate_file(pilot.get("video"), path.parent) != video.resolve():
            raise ValueError("Pilot video reference is outside its expected run directory")
        metadata = json.loads(gate_file(pilot.get("action_metadata"), path.parent).read_text())
        if any(
            metadata.get(k) != v
            for k, v in {
                "status": "extracted",
                "source_sha256": run["ghost_sha256"],
                "map_uid": manifest["map"]["uid"],
                "account_id": run["account_id"],
                "finish_time_ms": run["record_time_ms"],
            }.items()
        ):
            raise ValueError("Pilot action metadata belongs to a different run")
        if result.get("renderer_backend", "openplanet") != backend:
            raise ValueError("Pilot rendering backend changed")
        validate_render_result(result, manifest, run, result_path.parent)
    return gate


def run_one(worker, manifest, run, output, user_dir):
    folder = output / run["run_id"]
    folder.mkdir(parents=True, exist_ok=True)
    result_path = folder / "result.json"
    key = source_key(manifest, run)
    if result_path.exists():
        old = json.loads(result_path.read_text())
        if old.get("status") == "native_video_verified":
            video = folder / "video.webm"
            if old.get("source_key") != key or not video.exists() or sha256(video) != old["video"]["sha256"]:
                raise ValueError("Completed output/source changed; use a new output cohort")
            return {"run_id": run["run_id"], "status": "skipped_verified", "sha256": old["video"]["sha256"]}
    record = {
        "status": "rendering",
        "run": run,
        "map": manifest["map"],
        "source_key": key,
        "plugin_build": BUILD,
        "settings": SETTINGS,
        "started_unix": time.time(),
    }
    write_json(result_path, record)
    try:
        map_name = manifest["map"]["sha256"] + ".Map.Gbx"
        copy_verified(
            manifest["map"]["path"], user_dir / "Maps/_dataset" / map_name, manifest["map"]["sha256"]
        )
        copy_verified(
            run["ghost_path"],
            user_dir / "Replays/_dataset" / (run["run_id"] + ".Ghost.Gbx"),
            run["ghost_sha256"],
        )
        state = worker.status()
        if state["build"] != BUILD:
            raise ValueError("Unexpected renderer plugin version")
        worker.request("/setup", name=map_name, uid=manifest["map"]["uid"])
        if state.get("context") != 2:
            worker.wait("ctx:1")
        worker.request("/mediatracker")
        worker.wait("ctx:2")
        worker.request("/newclip")
        worker.request("/import", name=run["run_id"] + ".Ghost.Gbx")
        worker.wait("ghosts:1")
        scene = worker.request("/camera", cam=2)
        record["scene"] = scene
        record["timeline"] = check_scene(scene, run, manifest["map"]["uid"])
        worker.request("/shoot")
        worker.wait("shootdlg")
        record["native_export_settings"] = worker.request("/shootsetup", name=run["run_id"])
        write_json(result_path, record)
        screenshots = user_dir / "Screenshots"
        screenshots.mkdir(parents=True, exist_ok=True)
        record["protected_native_exports"] = protect_native_exports(
            screenshots, output / "_native_export_backups"
        )
        write_json(result_path, record)
        before = snapshot(screenshots)
        worker.request("/shootok")
        worker.wait("noshootdlg")
        native = wait_changed(screenshots, before)
        safe_name(native.name)
        # DenyWrite is checked inside Wine, where the encoder's Windows handle lives.
        record["file_release"] = worker.request("/awaitfile", name=native.name, ms=3600000, timeout=3615)
        changed = [p for p, st in snapshot(screenshots).items() if before.get(p) != st]
        if changed != [str(native)] or native.stat().st_size <= 0:
            raise ValueError("Native output identity changed or empty output")
        expected = record["timeline"]["expected_native_duration_seconds"]
        record["native_video"] = video_check(native, expected)
        native_copy = folder / "native.webm"
        copy_verified(native, native_copy, record["native_video"]["sha256"])
        if str(native) in record["protected_native_exports"]:
            original = record["protected_native_exports"][str(native)]
            copy_verified(original["backup"], native, original["sha256"])
            record["preexisting_native_export_restored"] = True
        final = folder / "video.webm"
        partial = folder / "video.partial.webm"
        # Drop native audio without changing, resampling or re-encoding video frames.
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(native_copy),
                "-map",
                "0:v:0",
                "-c:v",
                "copy",
                "-an",
                str(partial),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
        )
        record["video"] = video_check(partial, expected)
        if record["video"]["audio_streams"] != 0:
            raise ValueError("Final video contains audio")
        os.replace(partial, final)
        record["native_source_path"] = str(native)
        record["status"] = "native_video_verified"
        record["completed_unix"] = time.time()
        write_json(result_path, record)
        return {"run_id": run["run_id"], "status": record["status"], "frames": record["video"]["frames"]}
    except BaseException as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
        record["failed_unix"] = time.time()
        write_json(result_path, record)
        raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("manifest", type=Path)
    p.add_argument("--user-dir", type=Path, required=True, help="Linux path to Documents/Trackmania")
    p.add_argument("--game-user-dir", default="", help="Matching Windows path, for collector manifests")
    p.add_argument("--output", type=Path, required=True, help="Dataset output on data drive")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--gate", type=Path, help="Required for more than five runs")
    p.add_argument(
        "--preflight", action="store_true", help="Verify manifest/files without contacting the game"
    )
    p.add_argument("--port", type=int, default=29801)
    args = p.parse_args()
    if not 1 <= args.limit <= 250:
        p.error("--limit must be in 1..250")
    args.output.mkdir(parents=True, exist_ok=True)
    # One game/user-tree means one supervisor, even with different output cohorts.
    with (args.user_dir / ".dataset-renderer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = normalise_manifest(args.manifest.resolve(), args.user_dir.resolve(), args.game_user_dir)
        chosen = manifest["runs"][: args.limit]
        if len(chosen) != args.limit or [r["ordinal"] for r in chosen] != list(range(1, args.limit + 1)):
            raise ValueError("Manifest lacks the exact requested contiguous leaderboard positions")
        write_json(args.output / "render_manifest.json", manifest)
        if args.preflight:
            print(json.dumps({"preflight": "passed", "runs": len(chosen), "map": manifest["map"]}))
            return
        if args.limit > 5:
            if not args.gate:
                p.error("--gate required to render beyond pilot ranks 1–5")
            gate = validate_gate(args.gate, manifest, args.output)
            if gate.get("renderer_backend", "openplanet") != "openplanet":
                raise ValueError("Pilot backend differs from plugin batch")
        worker = Worker(args.port)
        for run in chosen:
            print(
                json.dumps(run_one(worker, manifest, run, args.output, args.user_dir.resolve())), flush=True
            )


if __name__ == "__main__":
    main()
