"""Freeze a Trackmania.io leaderboard before any replay is downloaded.

Powered by Trackmania.io. Review https://openplanet.dev/tmio/api before use.
Only map metadata and leaderboard pages use that service, never bulk ghosts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from collector.common import (
    lock,
    now,
    public_url,
    safe_cohort,
    safe_uid,
    sha256,
    valid_id,
    validate_snapshot,
    write_json,
)


class SameOrigin(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https" or urlsplit(newurl).netloc != "trackmania.io":
            raise ValueError("Unexpected Trackmania.io redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, user_agent, interval=2.0):
        if len(user_agent.strip()) < 12 or any(c in user_agent for c in "\r\n"):
            raise ValueError("Supply a descriptive User-Agent for your project")
        self.user_agent = user_agent
        self.interval = max(2.0, interval)
        self.last_request = 0.0
        self.opener = build_opener(SameOrigin())

    def cached_get(self, url, path):
        receipt_path = path.with_suffix(".receipt.json")
        if path.exists() or receipt_path.exists():
            if not path.exists() or not receipt_path.exists():
                raise ValueError(
                    f"Incomplete cached response: {path}; preserve it and use a new snapshot directory"
                )
            receipt = json.loads(receipt_path.read_text())
            if receipt["url"] != url or receipt["sha256"] != sha256(path):
                raise ValueError("Cached response identity or content changed")
            return json.loads(path.read_text()), receipt
        wait = self.interval - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        started = now()
        self.last_request = time.monotonic()
        request = Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        with self.opener.open(request, timeout=45) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
            if response.status != 200 or len(raw) > 16 * 1024 * 1024:
                raise ValueError("Unexpected or oversized API response")
            value = json.loads(raw)
            receipt = {
                "url": url,
                "final_url": public_url(response.url),
                "status": response.status,
                "started_utc": started,
                "retrieved_utc": now(),
                "user_agent": self.user_agent,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "path": path.name,
            }
        path.write_bytes(raw)
        write_json(receipt_path, receipt)
        return value, receipt


def append_entries(entries, page, length):
    if page.get("lockedLeaderboard") or len(page["tops"]) != length:
        raise ValueError("Leaderboard is locked or does not contain the requested number of records")
    for row in page["tops"]:
        path = urlsplit(row["url"]).path
        if not path.startswith("/api/download/ghost/"):
            raise ValueError("Leaderboard did not provide a record UUID")
        entries.append(
            {
                "ordinal": len(entries) + 1,
                "rank": row["position"],
                "accountId": valid_id(row["player"]["id"]),
                "scoreMs": row["time"],
                "mapRecordId": valid_id(path.removeprefix("/api/download/ghost/")),
                "leaderboardTimestamp": row["timestamp"],
            }
        )


def verify_sources(result, directory):
    """Re-derive normalized identities from the actual cached source responses."""
    directory = Path(directory).resolve()

    def read_receipt(receipt, expected_url):
        path = (directory / receipt["path"]).resolve()
        if path.parent != directory or sha256(path) != receipt["sha256"] or receipt["url"] != expected_url:
            raise ValueError("Snapshot source response changed")
        return json.loads(path.read_text())

    uid = result["mapUid"]
    metadata = read_receipt(result["map_metadata_receipt"], f"https://trackmania.io/api/map/{uid}")
    if (metadata["mapUid"], metadata["mapId"]) != (uid, result["mapId"]) or metadata.get("hasClones"):
        raise ValueError("Snapshot map differs from source metadata")
    if len(result["pages"]) != (result["count"] + 99) // 100:
        raise ValueError("Snapshot page coverage differs")
    derived = []
    for offset, receipt in zip(range(0, result["count"], 100), result["pages"]):
        length = min(100, result["count"] - offset)
        url = f"https://trackmania.io/api/leaderboard/map/{uid}?" + urlencode(
            {"offset": offset, "length": length}
        )
        append_entries(derived, read_receipt(receipt, url), length)
    if result["entries"] != derived:
        raise ValueError("Snapshot entries differ from source leaderboard responses")


def freeze(map_uid, count, cohort, output, user_agent):
    safe_uid(map_uid)
    safe_cohort(cohort)
    if type(count) is not int or not 1 <= count <= 10000:
        raise ValueError("Count must be 1..10000")
    output = Path(output).resolve()
    with lock(output, ".snapshot.lock"):
        target = output / "leaderboard.json"
        if target.exists():
            result = validate_snapshot(json.loads(target.read_text()))
            if (result["mapUid"], result["count"], result["cohort"]) != (map_uid, count, cohort):
                raise ValueError("Existing snapshot belongs to another cohort/map/count")
            verify_sources(result, output)
            return target
        started = now()
        client = Client(user_agent)
        metadata, map_receipt = client.cached_get(
            f"https://trackmania.io/api/map/{map_uid}", output / "map.json"
        )
        if metadata["mapUid"] != map_uid or metadata.get("hasClones"):
            raise ValueError("Map UID mismatch or cloned leaderboard: explicit resolution required")
        map_id = valid_id(metadata["mapId"])
        entries, pages = [], []
        for offset in range(0, count, 100):
            length = min(100, count - offset)
            url = f"https://trackmania.io/api/leaderboard/map/{map_uid}?" + urlencode(
                {"offset": offset, "length": length}
            )
            page, receipt = client.cached_get(url, output / f"page-{offset:05}.json")
            pages.append(receipt)
            append_entries(entries, page, length)
        result = {
            "schema_version": 1,
            "source": "Trackmania.io",
            "attribution": "Powered by Trackmania.io",
            "policy_url": "https://openplanet.dev/tmio/api",
            "cohort": cohort,
            "mapUid": map_uid,
            "mapId": map_id,
            "count": count,
            "entries": entries,
            "snapshot_atomic": False,
            "snapshot_started_utc": min(r.get("started_utc", started) for r in [map_receipt, *pages]),
            "snapshot_completed_utc": now(),
            "pages": pages,
            "map_metadata_receipt": map_receipt,
        }
        validate_snapshot(result)
        write_json(target, result)
        return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map-uid", required=True)
    parser.add_argument("--count", type=int, default=250)
    parser.add_argument("--cohort", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--user-agent", required=True)
    args = parser.parse_args(argv)
    result = freeze(args.map_uid, args.count, args.cohort, args.output, args.user_agent)
    print(json.dumps({"snapshot": str(result), "sha256": sha256(result)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
