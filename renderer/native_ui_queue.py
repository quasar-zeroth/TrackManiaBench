#!/usr/bin/env python3
"""Accept evidence-bound native UI exports; intentionally performs no GUI actions.

Commands: plan, closure, accept. Native UI coordinates/automation are not guessed.
More than five accepted runs require the same verified pilot gate as the plugin.
"""

from __future__ import annotations
import argparse
import csv
import datetime
import fcntl
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("native_ui_common", HERE / "render_queue.py")
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
BUILD = common.NATIVE_UI_BUILD
SETTINGS = common.settings_for_backend("native_ui")


def require(value, message):
    if not value:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text())


def pin(path):
    path = Path(path).resolve(strict=True)
    return {"path": str(path), "sha256": common.sha256(path)}


def ref(value, base):
    return common.gate_file(value, Path(base))


def observed(observation, base, label):
    require(
        observation.get("verified_by") and observation.get("verified_utc"), label + " lacks observer/time"
    )
    refs = observation.get("evidence_files", [])
    require(refs, label + " lacks actual hash-pinned evidence")
    for item in refs:
        ref(item, base)


def metadata(path, source_hash, run, uid):
    m = read(path)
    expected = {
        "status": "extracted",
        "source_sha256": source_hash,
        "map_uid": uid,
        "account_id": run["account_id"],
        "finish_time_ms": run["record_time_ms"],
    }
    require(all(m.get(k) == v for k, v in expected.items()), "Extraction identity differs: " + str(path))
    for name in (
        "strict_bitstream",
        "gbx_semantics_match_strict_rows",
        "single_store",
        "race_coverage",
        "normalized_steering_range",
    ):
        require(m.get("checks", {}).get(name) is True, "Extraction check failed: " + name)
    required = {
        "actions_all_100hz.csv",
        "actions_race_100hz.csv",
        "input_events.json",
        "input_changes.json",
        "visual_samples.csv",
    }
    files = m.get("files", {})
    require(
        required <= files.keys() and any(k.startswith("input_store_") for k in files),
        "Extraction evidence incomplete",
    )
    for name, data in files.items():
        common.safe_name(name)
        require(
            common.sha256(Path(path).parent / name) == data["sha256"], "Extraction output changed: " + name
        )
    return m


def clip_inspection(clip, inspection, run, action_csv, purpose):
    data = read(inspection)
    require(data.get("source_sha256") == common.sha256(clip), "Inspector source clip hash differs")
    require(data.get("container_class") == "CGameCtnMediaClip", "Expected a native Clip.Gbx inspection")
    require(
        data.get("gbx_net_version") == "2.5.1" and data.get("inspector_version"),
        "Unexpected or missing clip parser version",
    )
    tracks = data.get("tracks", [])
    require(
        all(not t.get("is_keep_playing") and not t.get("is_cycling") for t in tracks),
        "Cycling/keep-playing track unsupported",
    )
    blocks = [b for t in tracks for b in t.get("blocks", [])]
    entities = [b for b in blocks if b.get("class") == "CGameCtnMediaBlockEntity"]
    cameras = [b for b in blocks if b.get("class", "").startswith("CGameCtnMediaBlockCamera")]
    texts = [b for b in blocks if b.get("class") == "CGameCtnMediaBlockText"]
    require(len(entities) == 1 and len(cameras) == 1, "Exactly one entity and one camera required")
    require(
        len(blocks) == 2 + (len(texts) if purpose == "diagnostic" else 0),
        "Unexpected effects/HUD/camera/diagnostic tracks",
    )
    entity, camera = entities[0], cameras[0]
    require(
        camera["class"] == "CGameCtnMediaBlockCameraGame"
        and camera.get("game_camera_id") == 2
        and camera.get("clip_entity_id") == 1,
        "Wrong fixed native camera/target",
    )
    require(
        all(camera.get(k) is False for k in ("show_hud", "show_ping", "show_cross_hair")),
        "Camera HUD flags not disabled",
    )
    require(entity.get("race_time_ms") == run["record_time_ms"], "Wrong entity finish time")
    require(
        isinstance(entity.get("notice_records"), list)
        and entity["notice_records"]
        and all(type(x) is int and x >= 0 for x in entity["notice_records"]),
        "Native entity visibility bindings are missing",
    )
    require(
        entity.get("player_model") and "CarSport" in entity["player_model"],
        "Native car model identity missing",
    )
    require(
        entity.get("time_scale") == 1 and entity.get("start_offset_ms") == 0,
        "Uncalibrated entity speed/offset",
    )
    start, end = float(entity["start_ms"]), float(entity["end_ms"])
    record = entity.get("record_data", {})
    record_start, record_end = float(record["start_ms"]), float(record["end_ms"])
    require(
        all(math.isfinite(x) for x in (start, end, record_start, record_end)), "Nonfinite native scene timing"
    )
    require(
        record_start == 0 and abs(record_end - run["record_time_ms"]) <= 100,
        "Native RecordData bounds do not cover the finish within sample precision",
    )
    # A positive entity start is an explicit native preroll. Keeping the camera
    # active from clip zero permits renderer warm-up without shifting raw source
    # samples. This serialized mapping is not proof of video/race alignment.
    require(
        0 <= start <= 2000
        and end >= start + max(record_end, run["record_time_ms"])
        and end <= start + record_end + 1000,
        "Entity does not cover full race interval with bounded preroll/postroll",
    )
    require(
        abs(float(camera["start_ms"])) <= 0.1 and abs(float(camera["end_ms"]) - end) <= 0.1,
        "Camera does not cover full clip including preroll/postroll",
    )
    scene_timing = {
        "preroll_ms": start,
        "postroll_ms": end - start - record_end,
        "record_start_ms": record_start,
        "record_end_ms": record_end,
        "entity_start_ms": start,
        "entity_end_ms": end,
        "camera_start_ms": float(camera["start_ms"]),
        "camera_end_ms": float(camera["end_ms"]),
        "clip_to_record_expression": "record_ms = start_offset_ms + (clip_ms - entity_start_ms) * time_scale",
        "native_postroll_behavior_verified": False,
        "video_to_race_alignment_verified": False,
        "training_frame_selection": "Requires independently measured calibration; retain all native export frames",
    }
    actual = []
    for ent in entity.get("record_data", {}).get("entities", []):
        for sample in ent.get("vehicle_samples", []):
            actual.append(
                (
                    int(ent["entity_index"]),
                    int(sample["sample_index"]),
                    float(sample["raw_timestamp_ms"]),
                    sample["position"],
                    sample["rotation"],
                    sample["velocity"],
                )
            )
    with Path(action_csv).open(newline="") as f:
        rows = list(csv.DictReader(f))
    expected = []
    for row in rows:
        require(row["source"] == "record_data", "Unexpected visual sample source")
        expected.append(
            (
                int(row["entity_index"]),
                int(row["sample_index"]),
                float(row["raw_timestamp_ms"]),
                [float(row["position_" + k]) for k in "xyz"],
                [float(row["rotation_" + k]) for k in "xyzw"],
                [float(row["velocity_" + k]) for k in "xyz"],
            )
        )
    actual.sort(key=lambda x: x[:3])
    expected.sort(key=lambda x: x[:3])
    require(len(actual) == len(expected) and len(actual) > 2, "Clip/source vehicle sample counts differ")
    worst = 0.0
    for got, wanted in zip(actual, expected):
        require(got[:3] == wanted[:3], "Clip/source sample entity/index/timestamp differs")
        for a, b in zip(sum(got[3:], []), sum(wanted[3:], [])):
            require(math.isfinite(a) and math.isfinite(b), "Nonfinite vehicle sample")
            # Both parsers serialize the same float32; decimal formatting can
            # differ by a few final places. No temporal interpolation is done.
            error = abs(a - b)
            worst = max(worst, error)
            require(error <= max(0.0002, abs(b) * 2e-7), "Clip/source vehicle trajectory differs")
    return {
        "entity": {k: v for k, v in entity.items() if k != "record_data"},
        "camera": camera,
        "expected_native_duration_seconds": end / 1000,
        "vehicle_samples_compared": len(actual),
        "serialized_scene_timing": scene_timing,
        "max_sample_numeric_error": worst,
        "clip_to_record_verified": True,
        "alignment_status": "requires_measured_race_clock_calibration",
    }


def validate_evidence(evidence_path, manifest, run, purpose="production"):
    evidence_path = Path(evidence_path).resolve(strict=True)
    base = evidence_path.parent
    e = read(evidence_path)
    require(e.get("schema_version") == 1 and e.get("purpose") == purpose, "Wrong UI evidence schema/purpose")
    require(e.get("settings") == SETTINGS, "UI export settings differ")
    require(
        e.get("run_id") == run["run_id"]
        and e.get("ghost_sha256") == run["ghost_sha256"]
        and e.get("map_sha256") == manifest["map"]["sha256"],
        "UI evidence identity differs",
    )
    replay = ref(e["native_replay"], base)
    original_meta = ref(e["original_action_metadata"], base)
    replay_meta = ref(e["replay_action_metadata"], base)
    original = metadata(original_meta, run["ghost_sha256"], run, manifest["map"]["uid"])
    converted = metadata(replay_meta, common.sha256(replay), run, manifest["map"]["uid"])
    require(
        original["files"].keys() == converted["files"].keys(), "Native conversion extraction file set differs"
    )
    for name in original["files"]:
        require(
            original["files"][name]["sha256"] == converted["files"][name]["sha256"],
            "Native conversion changed " + name,
        )
    imported = ref(e["imported_clip"], base)
    imported_inspection = ref(e["imported_clip_inspection"], base)
    native = ref(e["reexported_clip"], base)
    native_inspection = ref(e["reexported_clip_inspection"], base)
    original_timeline = clip_inspection(
        imported, imported_inspection, run, original_meta.parent / "visual_samples.csv", purpose
    )
    timeline = clip_inspection(
        native, native_inspection, run, original_meta.parent / "visual_samples.csv", purpose
    )
    for field in (
        "start_ms",
        "end_ms",
        "start_offset_ms",
        "time_scale",
        "notice_records",
        "player_model",
        "appearance_archive_version",
        "appearance_color",
        "skin_names",
        "skin_options",
        "has_badges",
        "no_damage",
    ):
        require(
            original_timeline["entity"].get(field) == timeline["entity"].get(field),
            "Native import changed entity field: " + field,
        )
    require(
        original_timeline["serialized_scene_timing"] == timeline["serialized_scene_timing"],
        "Native import changed serialized scene timing",
    )
    # Native Replay_Save can rewrite map serialization. A hash-bound semantic
    # map comparison must therefore be supplied, not raw byte equality assumed.
    map_comparison_path = ref(e["map_comparison"], base)
    comparison = read(map_comparison_path)
    require(
        comparison.get("map_uid") == manifest["map"]["uid"]
        and comparison.get("original_map", {}).get("sha256") == manifest["map"]["sha256"]
        and comparison.get("native_replay", {}).get("sha256") == common.sha256(replay),
        "Map comparison source identity differs",
    )
    map_audit = comparison.get("semantic_map_verification", {})
    require(
        map_audit.get("passed") is True and map_audit.get("checks"),
        "Native embedded map equivalence not verified",
    )
    required_map_checks = {
        "identity_environment_rules",
        "blocks",
        "baked_blocks",
        "anchored_objects",
        "script_metadata",
        "embedded_assets",
        "only_declared_metadata_normalizations",
    }
    require(required_map_checks <= map_audit["checks"].keys(), "Incomplete semantic map comparison")
    require(comparison.get("canonical_record_equal") is True, "Full native RecordData semantics differ")
    require(
        all(v.get("passed") is True and v.get("evidence") for v in map_audit["checks"].values()),
        "Map semantic check lacks evidence",
    )
    for item in map_audit.get("evidence_files", []):
        ref(item, map_comparison_path.parent)
    require(map_audit.get("evidence_files"), "Map semantic audit needs actual evidence files")
    for name in ("settings_observation", "scene_observation", "completion_observation"):
        observed(e.get(name, {}), base, name)
    scene = e["scene_observation"]
    require(
        scene.get("native_replay_sha256") == common.sha256(replay)
        and scene.get("imported_clip_sha256") == common.sha256(imported)
        and scene.get("reexported_clip_sha256") == common.sha256(native)
        and scene.get("unchanged_until_render") is True,
        "Native scene observation does not bind this export",
    )
    require(e["settings_observation"].get("settings") == SETTINGS, "Observed settings differ")
    require(
        set(e["settings_observation"].get("unobserved_settings", [])) >= {"dof", "motion_blur", "tile_split"},
        "Unexposed native export controls must remain explicitly documented as unknown",
    )
    require(
        e["completion_observation"].get("native_ui_returned") is True,
        "Native exporter completion not observed",
    )
    video = ref(e["native_video"], base)
    closure_path = ref(e["file_closure"], base)
    closure = read(closure_path)
    require(
        closure.get("method") == "linux-declared-game-process-write-fd-scan-and-stable-sha256",
        "Unrecognized file closure proof",
    )
    require(closure.get("video", {}).get("sha256") == common.sha256(video), "Closure proof video differs")
    require(closure.get("writer_processes"), "Closure proof lacks declared game processes")
    samples = closure.get("samples", [])
    require(
        len(samples) >= 2 and samples[-1]["monotonic_seconds"] - samples[0]["monotonic_seconds"] >= 1,
        "File closure needs separated observations",
    )
    for sample in samples:
        require(
            sample.get("sha256") == common.sha256(video)
            and sample.get("size") == video.stat().st_size
            and sample.get("open_write_fds") == []
            and sample.get("unreadable_owned_processes") == [],
            "Native output was still open/changed or process scan incomplete",
        )
    return e, timeline, video


def validate_record_evidence(result, manifest, run, result_dir):
    path = ref(result["native_ui_evidence"], result_dir)
    e, timeline, native_video = validate_evidence(path, manifest, run)
    require(
        result.get("native_video", {}).get("sha256") == common.sha256(native_video),
        "Native video result differs",
    )
    require(result.get("timeline") == timeline, "Stored native scene/timeline audit changed")
    return timeline


def scan_write_fds(path, pids=None):
    target = Path(path).stat()
    found = []
    unreadable = []
    processes = [Path("/proc") / str(pid) for pid in pids] if pids is not None else Path("/proc").iterdir()
    for proc in processes:
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            for fd in (proc / "fd").iterdir():
                try:
                    stat = fd.stat()
                    if (stat.st_dev, stat.st_ino) != (target.st_dev, target.st_ino):
                        continue
                    info = (proc / "fdinfo" / fd.name).read_text()
                    flag = next(
                        int(line.split()[1], 8) for line in info.splitlines() if line.startswith("flags:")
                    )
                    if flag & os.O_ACCMODE:
                        found.append({"pid": int(proc.name), "fd": int(fd.name), "flags": flag})
                except (FileNotFoundError, ProcessLookupError):
                    pass
        except (FileNotFoundError, ProcessLookupError):
            pass
        except PermissionError:
            unreadable.append(int(proc.name))
    return found, unreadable


def closure(video, output, pids):
    video = video.resolve(strict=True)
    require(not output.exists(), "Closure proof already exists")
    require(pids, "Declare the native game writer process IDs")
    processes = []
    for pid in sorted(set(pids)):
        proc = Path("/proc") / str(pid)
        require(proc.stat().st_uid == os.getuid(), "Game process must belong to current user")
        processes.append(
            {
                "pid": pid,
                "comm": (proc / "comm").read_text().strip(),
                "starttime": (proc / "stat").read_text().rsplit(")", 1)[1].split()[19],
            }
        )
    samples = []
    for index in range(2):
        if index:
            time.sleep(1.2)
        before = video.stat()
        digest = common.sha256(video)
        writes, unreadable = scan_write_fds(video, pids)
        after = video.stat()
        require(
            (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
            "Video changed during closure observation",
        )
        require(not writes and not unreadable, "A writer remains open or process scan was incomplete")
        samples.append(
            {
                "monotonic_seconds": time.monotonic(),
                "size": after.st_size,
                "mtime_ns": after.st_mtime_ns,
                "sha256": digest,
                "open_write_fds": writes,
                "unreadable_owned_processes": unreadable,
            }
        )
    require(
        samples[0]["sha256"] == samples[1]["sha256"] and samples[0]["mtime_ns"] == samples[1]["mtime_ns"],
        "Video still changing",
    )
    common.write_json(
        output,
        {
            "schema_version": 1,
            "method": "linux-declared-game-process-write-fd-scan-and-stable-sha256",
            "video": pin(video),
            "writer_processes": processes,
            "samples": samples,
            "verified_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "verifier_sha256": common.sha256(Path(__file__)),
        },
    )


def accept(manifest, run, output, evidence):
    folder = output / run["run_id"]
    folder.mkdir(parents=True, exist_ok=True)
    result_path = folder / "result.json"
    key = common.source_key(manifest, run, "native_ui")
    if result_path.exists():
        old = read(result_path)
        if old.get("status") == "native_video_verified":
            common.validate_render_result(old, manifest, run, folder)
            require(old.get("source_key") == key, "Completed source identity changed")
            return {"run_id": run["run_id"], "status": "skipped_verified", "sha256": old["video"]["sha256"]}
    record = {
        "schema_version": 1,
        "status": "validating_native_ui_export",
        "run": run,
        "map": manifest["map"],
        "source_key": key,
        "settings": SETTINGS,
        **common.backend_identity("native_ui"),
        "native_ui_evidence": pin(evidence),
        "validator_sha256": common.sha256(Path(__file__)),
    }
    common.write_json(result_path, record)
    try:
        e, timeline, native = validate_evidence(evidence, manifest, run)
        record["timeline"] = timeline
        record["native_video"] = common.video_check(native, timeline["expected_native_duration_seconds"])
        common.copy_verified(native, folder / "native.webm", record["native_video"]["sha256"])
        # The game's next export may overwrite Video01.webm. Bind accepted
        # evidence to the immutable per-run copy, retaining original provenance.
        accepted_evidence = json.loads(json.dumps(e))
        for key, value in accepted_evidence.items():
            if isinstance(value, dict) and "path" in value and "sha256" in value:
                value["path"] = str(ref(value, evidence.parent))
            if isinstance(value, dict) and isinstance(value.get("evidence_files"), list):
                for item in value["evidence_files"]:
                    item["path"] = str(ref(item, evidence.parent))
        accepted_evidence["source_export_evidence"] = pin(evidence)
        accepted_evidence["native_video"] = pin(folder / "native.webm")
        common.write_json(folder / "export-evidence.json", accepted_evidence)
        record["native_ui_evidence"] = pin(folder / "export-evidence.json")
        partial = folder / "video.partial.webm"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(folder / "native.webm"),
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
        record["video"] = common.video_check(partial, timeline["expected_native_duration_seconds"])
        require(record["video"]["audio_streams"] == 0, "Final video contains audio")
        os.replace(partial, folder / "video.webm")
        record["status"] = "native_video_verified"
        record["completed_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        common.write_json(result_path, record)
        common.validate_render_result(record, manifest, run, folder)
        return {"run_id": run["run_id"], "status": record["status"], "frames": record["video"]["frames"]}
    except BaseException as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
        common.write_json(result_path, record)
        raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("closure")
    c.add_argument("video", type=Path)
    c.add_argument("output", type=Path)
    c.add_argument("--writer-pid", action="append", type=int, required=True)
    for name in ("plan", "accept"):
        c = sub.add_parser(name)
        c.add_argument("manifest", type=Path)
        c.add_argument("--user-dir", type=Path, required=True)
        c.add_argument("--game-user-dir", default="")
        c.add_argument("--output", type=Path, required=True)
        c.add_argument("--limit", type=int, default=5)
        c.add_argument("--gate", type=Path)
        if name == "accept":
            c.add_argument("--ordinal", type=int, required=True)
            c.add_argument("--evidence", type=Path, required=True)
    a = p.parse_args()
    if a.command == "closure":
        closure(a.video, a.output, a.writer_pid)
        return
    require(1 <= a.limit <= 250, "Limit must be1..250")
    a.output.mkdir(parents=True, exist_ok=True)
    manifest = common.normalise_manifest(a.manifest.resolve(), a.user_dir.resolve(), a.game_user_dir)
    chosen = manifest["runs"][: a.limit]
    require(
        [r["ordinal"] for r in chosen] == list(range(1, a.limit + 1)),
        "Manifest lacks exact requested positions",
    )
    if a.command == "plan":
        jobs = {
            "schema_version": 1,
            **common.backend_identity("native_ui"),
            "settings": SETTINGS,
            "map": manifest["map"],
            "status": "awaiting_native_ui_exports",
            "jobs": [
                {
                    "run": r,
                    "source_key": common.source_key(manifest, r, "native_ui"),
                    "evidence_template": str(HERE / "native-ui-evidence.template.json"),
                }
                for r in chosen
            ],
        }
        common.write_json(a.output / "native-ui-jobs.json", jobs)
        print(json.dumps({"planned": len(chosen), "gui_actions": 0}))
        return
    if a.limit > 5 or a.ordinal > 5:
        require(a.gate is not None, "Verified top-five gate required beyond pilot")
        gate = common.validate_gate(a.gate, manifest, a.output)
        require(gate.get("renderer_backend") == "native_ui", "Pilot backend differs from native UI batch")
    selected = [r for r in chosen if r["ordinal"] == a.ordinal]
    require(len(selected) == 1, "Ordinal outside requested cohort")
    with (a.user_dir / ".dataset-renderer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(json.dumps(accept(manifest, selected[0], a.output, a.evidence.resolve())))


if __name__ == "__main__":
    main()
