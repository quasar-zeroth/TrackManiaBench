#!/usr/bin/env python3
"""Small standard-library primitives for the durable local dataset pipeline."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def pin(path):
    p = Path(path).resolve(strict=True)
    return {"path": str(p), "sha256": sha(p)}


def verified(ref):
    p = Path(ref["path"]).resolve(strict=True)
    require(str(p) == ref["path"] and sha(p) == ref["sha256"], "Pinned bytes changed: " + str(p))
    return p


def read_verified(ref):
    p = Path(ref["path"]).resolve(strict=True)
    data = p.read_bytes()
    require(
        str(p) == ref["path"] and hashlib.sha256(data).hexdigest() == ref["sha256"],
        "Pinned JSON changed: " + str(p),
    )
    return json.loads(data)


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def sync_directory(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encoded(value))
            f.flush()
            os.fsync(f.fileno())
        # Local workspace only. Hard-link publication is atomic and refuses an
        # existing destination; a killed writer cannot publish truncated JSON.
        os.link(temporary, path)
        sync_directory(path.parent)
    finally:
        os.unlink(temporary)


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    with tmp.open("wb") as f:
        f.write(encoded(value))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    sync_directory(path.parent)


def expand(value, variables):
    if isinstance(value, str):
        return value.format_map(variables)
    if isinstance(value, list):
        return [expand(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, variables) for k, v in value.items()}
    return value


def matching(actual, expected, prefix=""):
    """Exact scalar/list values, recursive object subset for explicit checks."""
    if isinstance(expected, dict):
        require(isinstance(actual, dict), "Expected JSON object at " + prefix)
        for key, value in expected.items():
            require(key in actual, "Missing checked field " + prefix + key)
            matching(actual[key], value, prefix + key + ".")
    else:
        require(type(actual) is type(expected) and actual == expected, "Checked JSON differs at " + prefix)
