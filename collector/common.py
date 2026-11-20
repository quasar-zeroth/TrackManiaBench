"""Shared acquisition validation. No credentials or game session data are used."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import uuid
from urllib.parse import urlsplit, urlunsplit


def now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def lock(directory, name):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def safe_uid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("Map UID must contain only letters, digits, underscore or hyphen")
    return value


def safe_cohort(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("Cohort must contain only letters, digits, underscore or hyphen")
    return value


def valid_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value.lower():
        raise ValueError("Record/account/map ID must be a canonical UUID")
    return value.lower()


def inspect_gbx(path, classes=(0x03092000, 0x03093000)):
    with Path(path).open("rb") as stream:
        header = stream.read(13)
    if len(header) != 13 or header[:3] != b"GBX" or header[5:6] != b"B":
        raise ValueError("Not a binary GBX file")
    if struct.unpack_from("<H", header, 3)[0] != 6:
        raise ValueError("Unsupported GBX container version")
    class_id = struct.unpack_from("<I", header, 9)[0]
    if class_id not in classes:
        raise ValueError(f"Unexpected GBX class 0x{class_id:08x}")
    return class_id


def public_url(url):
    """Retain host/path provenance without signed query strings or user info."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))


def validate_snapshot(value):
    safe_uid(value["mapUid"])
    valid_id(value["mapId"])
    safe_cohort(value["cohort"])
    count = value["count"]
    entries = value["entries"]
    if type(count) is not int or count < 1 or len(entries) != count:
        raise ValueError("Snapshot count differs from its entries")
    if [entry["ordinal"] for entry in entries] != list(range(1, count + 1)):
        raise ValueError("Snapshot ordinals must be contiguous")
    for key in ("accountId", "mapRecordId"):
        if len({valid_id(entry[key]) for entry in entries}) != count:
            raise ValueError("Duplicate account or record in snapshot")
    scores = [entry["scoreMs"] for entry in entries]
    if any(type(score) is not int or score <= 0 for score in scores) or scores != sorted(scores):
        raise ValueError("Snapshot scores must be positive and ordered")
    ranks = [entry["rank"] for entry in entries]
    if any(type(rank) is not int or rank < 1 for rank in ranks) or ranks != sorted(ranks):
        raise ValueError("Snapshot ranks must be positive and ordered")
    if value.get("snapshot_atomic") is not False:
        raise ValueError("Paginated acquisition must not claim an atomic snapshot")
    return value
