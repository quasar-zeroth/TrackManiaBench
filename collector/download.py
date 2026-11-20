"""Download the exact frozen record UUIDs through official Nadeo URLs.

Adapted from the production frozen-cohort downloader. Stops on API errors and
rate limits; completed bytes are verified on resume. Bulk acquisition requires
the evidence-backed five-run rendering gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from collector.common import (
    inspect_gbx,
    lock,
    now,
    public_url,
    sha256,
    valid_id,
    validate_snapshot,
    write_json,
)


def valid_download_url(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return (
        parsed.scheme == "https"
        and parsed.port in (None, 443)
        and not parsed.username
        and not parsed.password
        and (
            host == "core.trackmania.nadeo.live"
            or (host.startswith("trackmania-") and host.endswith(".amazonaws.com"))
        )
    )


class OfficialRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not valid_download_url(newurl):
            raise ValueError("Unexpected official download redirect host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, target, user_agent, classes, limit):
    if not valid_download_url(url):
        raise ValueError("Unsupported download URL; require official HTTPS Nadeo or Trackmania S3 host")
    temporary = target.with_suffix(target.suffix + ".partial")
    started, size = now(), 0
    digest = hashlib.sha256()
    request = Request(url, headers={"User-Agent": user_agent, "Accept": "application/octet-stream"})
    with (
        build_opener(OfficialRedirects()).open(request, timeout=45) as response,
        temporary.open("wb") as stream,
    ):
        if response.status != 200 or not valid_download_url(response.url):
            raise ValueError("Unexpected official download response")
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            size += len(chunk)
            if size > limit:
                raise ValueError("Download exceeded the size limit")
            stream.write(chunk)
            digest.update(chunk)
        provenance = {
            "url": public_url(url),
            "final_url": public_url(response.url),
            "status": response.status,
            "started_utc": started,
            "retrieved_utc": now(),
            "user_agent": user_agent,
        }
        stream.flush()
        os.fsync(stream.fileno())
    class_id = inspect_gbx(temporary, classes)
    if target.exists() and sha256(target) != digest.hexdigest():
        raise ValueError("Refusing to replace changed source bytes")
    os.replace(temporary, target)
    return {
        "path": str(target.resolve()),
        "sha256": digest.hexdigest(),
        "bytes": size,
        "gbx_class_id": f"0x{class_id:08x}",
        "request": provenance,
    }


def verify_run(run, entry):
    expected = {
        "run_id": entry["mapRecordId"],
        "account_id": entry["accountId"],
        "rank": entry["rank"],
        "ordinal": entry["ordinal"],
        "record_time_ms": entry["scoreMs"],
        "status": "downloaded",
    }
    if any(run.get(key) != value for key, value in expected.items()):
        raise ValueError("Downloaded run identity differs from snapshot")
    path = Path(run["ghost_path"])
    if sha256(path) != run["ghost_sha256"] or path.stat().st_size != run["ghost_bytes"]:
        raise ValueError("Previously downloaded ghost changed")
    inspect_gbx(path)


def check_bulk_gate(gate, pilot, render_root, frozen):
    if not gate or not pilot or not render_root:
        raise ValueError("More than five downloads require --pilot, --gate and --render-output")
    pilot_doc = json.loads(Path(pilot).read_text())
    if pilot_doc.get("snapshot_sha256") != sha256(frozen) or pilot_doc.get("requested_count") != 5:
        raise ValueError("Pilot is not the unchanged first five of this snapshot")
    snapshot = validate_snapshot(json.loads(Path(frozen).read_text()))
    if (pilot_doc["map"]["uid"], pilot_doc["map"]["id"]) != (snapshot["mapUid"], snapshot["mapId"]):
        raise ValueError("Pilot map identity differs from snapshot")
    if pilot_doc.get("failures") or len(pilot_doc["runs"]) != 5:
        raise ValueError("Pilot is incomplete")
    for run, entry in zip(sorted(pilot_doc["runs"], key=lambda r: r["ordinal"]), snapshot["entries"][:5]):
        verify_run(run, entry)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "renderer"))
    from render_queue import normalise_manifest, validate_gate

    normalized = normalise_manifest(Path(pilot), Path(pilot).parent, "")
    validate_gate(Path(gate).resolve(), normalized, Path(render_root).resolve())
    return pilot_doc


def collect(
    frozen, output, count, user_agent, map_file=None, gate=None, pilot=None, render_root=None, preflight=False
):
    frozen, output = Path(frozen).resolve(), Path(output).resolve()
    snapshot = validate_snapshot(json.loads(frozen.read_text()))
    if "map_metadata_receipt" in snapshot:
        from collector.snapshot import verify_sources

        verify_sources(snapshot, frozen.parent)
    if type(count) is not int or not 1 <= count <= snapshot["count"]:
        raise ValueError("Requested count exceeds frozen snapshot")
    if len(user_agent.strip()) < 12 or any(c in user_agent for c in "\r\n"):
        raise ValueError("Supply a descriptive User-Agent")
    approved_pilot = check_bulk_gate(gate, pilot, render_root, frozen) if count > 5 else None
    if approved_pilot and map_file and sha256(map_file) != approved_pilot["map"]["sha256"]:
        raise ValueError("Supplied map differs from the approved pilot")
    if preflight:
        return {
            "status": "preflight_passed",
            "count": count,
            "snapshot_sha256": sha256(frozen),
            "network_requests": 0,
        }
    with lock(output, ".download.lock"):
        folder = output / "ghosts"
        folder.mkdir(exist_ok=True)
        progress_path = output / "collection-progress.json"
        if progress_path.exists():
            progress = json.loads(progress_path.read_text())
            if progress["snapshot_sha256"] != sha256(frozen):
                raise ValueError("Output directory belongs to another snapshot")
            if progress["requested_count"] > count:
                raise ValueError("Refuse to shrink a resumed cohort")
            map_info = progress["map"]
            if sha256(map_info["path"]) != map_info["sha256"]:
                raise ValueError("Map bytes changed")
            known = {row["ordinal"]: row for row in progress["runs"]}
            completed_path = output / "manifest.json"
            if completed_path.exists():
                completed = json.loads(completed_path.read_text())
                if (
                    completed["snapshot_sha256"] != progress["snapshot_sha256"]
                    or completed["map"] != map_info
                ):
                    raise ValueError("Completed collection source pins differ from progress")
                for row in completed["runs"]:
                    if known.get(row["ordinal"]) != row:
                        raise ValueError("Completed run differs from saved progress")
        else:
            if any(folder.iterdir()) or any(
                (output / name).exists()
                for name in ("manifest.json", "pilot-manifest.json", "source.Map.Gbx")
            ):
                raise ValueError(
                    "Existing collection lacks its progress pins; preserve it and restore verified progress"
                )
            known = {row["ordinal"]: row for row in approved_pilot["runs"]} if approved_pilot else {}
            if approved_pilot:
                # Keep the exact pilot paths and bytes when extending into a fresh output.
                map_info = dict(approved_pilot["map"])
            elif map_file:
                path = Path(map_file).resolve(strict=True)
                inspect_gbx(path, (0x03043000,))
                map_info = {"path": str(path), "sha256": sha256(path), "bytes": path.stat().st_size}
            else:
                metadata_path = frozen.parent / "map.json"
                if sha256(metadata_path) != snapshot["map_metadata_receipt"]["sha256"]:
                    raise ValueError("Map metadata changed after snapshot")
                metadata = json.loads(metadata_path.read_text())
                map_info = fetch(
                    metadata["fileUrl"],
                    output / "source.Map.Gbx",
                    user_agent,
                    (0x03043000,),
                    256 * 1024 * 1024,
                )
            map_info.update(uid=snapshot["mapUid"], id=snapshot["mapId"])
        if (map_info["uid"], map_info["id"]) != (snapshot["mapUid"], snapshot["mapId"]):
            raise ValueError("Collection map identity differs from the frozen snapshot")
        if map_file and sha256(map_file) != map_info["sha256"]:
            raise ValueError("Supplied map differs from the existing collection")
        if approved_pilot:
            if any(map_info[key] != approved_pilot["map"][key] for key in ("uid", "id", "sha256")):
                raise ValueError("Collection map differs from the approved pilot")
            for row in approved_pilot["runs"]:
                existing = known.get(row["ordinal"])
                if existing and any(
                    existing[key] != row[key]
                    for key in (
                        "run_id",
                        "account_id",
                        "ordinal",
                        "rank",
                        "record_time_ms",
                        "ghost_sha256",
                        "ghost_bytes",
                    )
                ):
                    raise ValueError("Collection ghost differs from the approved pilot")
                known.setdefault(row["ordinal"], row)
        result = {
            "schema_version": 1,
            "cohort": snapshot["cohort"],
            "source": "Trackmania.io; official Nadeo replays",
            "attribution": "Powered by Trackmania.io",
            "requested_count": count,
            "snapshot_count": snapshot["count"],
            "snapshot_path": str(frozen),
            "snapshot_sha256": sha256(frozen),
            "snapshot_atomic": False,
            "map": map_info,
            "runs": [],
            "failures": [],
            "collection_started_utc": now(),
        }
        # Persist the map before the first ghost request, so interruption cannot replace it.
        result["runs"] = [known[k] for k in sorted(known)]
        write_json(progress_path, result)
        last_request = time.monotonic()
        for entry in snapshot["entries"][:count]:
            ordinal, record_id = entry["ordinal"], valid_id(entry["mapRecordId"])
            try:
                sidecar = folder / f"{record_id}.provenance.json"
                if ordinal not in known and sidecar.exists():
                    known[ordinal] = json.loads(sidecar.read_text())
                if ordinal in known:
                    verify_run(known[ordinal], entry)
                else:
                    wait = 2.0 - (time.monotonic() - last_request)
                    if wait > 0:
                        time.sleep(wait)
                    last_request = time.monotonic()
                    url = f"https://core.trackmania.nadeo.live/mapRecords/{record_id}/replay"
                    info = fetch(
                        url,
                        folder / f"{record_id}.Ghost.Gbx",
                        user_agent,
                        (0x03092000, 0x03093000),
                        64 * 1024 * 1024,
                    )
                    row = {
                        "run_id": record_id,
                        "account_id": entry["accountId"],
                        "ordinal": ordinal,
                        "rank": entry["rank"],
                        "record_time_ms": entry["scoreMs"],
                        "status": "downloaded",
                        "ghost_path": info["path"],
                        "ghost_sha256": info["sha256"],
                        "ghost_bytes": info["bytes"],
                        "gbx_class_id": info["gbx_class_id"],
                        "leaderboard_timestamp": entry["leaderboardTimestamp"],
                        "request": info["request"],
                    }
                    write_json(sidecar, row)
                    known[ordinal] = row
                result["runs"] = [known[k] for k in sorted(known) if k <= count]
                write_json(progress_path, result)
            except BaseException as exc:
                result["failures"] = [
                    {
                        "ordinal": ordinal,
                        "run_id": record_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "utc": now(),
                    }
                ]
                write_json(progress_path, result)
                raise
        result["collection_completed_utc"] = now()
        write_json(output / "manifest.json", result)
        return {"status": "collection_complete", "count": count, "manifest": str(output / "manifest.json")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--user-agent", required=True)
    parser.add_argument("--map-file", type=Path)
    parser.add_argument("--gate", type=Path)
    parser.add_argument("--pilot", type=Path)
    parser.add_argument("--render-output", type=Path)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args(argv)
    print(
        json.dumps(
            collect(
                args.frozen,
                args.output,
                args.count,
                args.user_agent,
                args.map_file,
                args.gate,
                args.pilot,
                args.render_output,
                args.preflight,
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
