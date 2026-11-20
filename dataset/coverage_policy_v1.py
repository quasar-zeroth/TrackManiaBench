"""Exact, source-bound missing-prefix policy; never manufactures control rows.

Reviewed partial records may have inputs beginning at10ms.
Native replay identity/source hashing and original/native equality are separate
required checks by callers. This module admits their observed input extent only.
"""

from fractions import Fraction

import json
import os
import hashlib
from pathlib import Path

POLICY = "observed_10ms_prefix_gap_v1"
_POLICY_PATH = os.environ.get("TRACKMANIABENCH_COVERAGE_POLICY")
if _POLICY_PATH:
    _POLICY_PATH = str(Path(_POLICY_PATH).resolve(strict=True))
    _POLICY_BYTES = Path(_POLICY_PATH).read_bytes()
    _policy = json.loads(_POLICY_BYTES)
    if _policy.get("schema_version") != 1 or not isinstance(_policy.get("map_uid"), str):
        raise ValueError("Coverage policy requires schema_version=1 and map_uid")
else:
    _policy = {}
MAP_UID = _policy.get("map_uid")
CASES = _policy.get("cases", {})
RECORD_END_CASES = _policy.get("record_end_cases", {})
RECORD_CASES = {**CASES, **_policy.get("record_cases", {}), **RECORD_END_CASES}


def require(value, message):
    if not value:
        raise ValueError(message)


def policy_reference():
    if not _POLICY_PATH:
        return None
    data = Path(_POLICY_PATH).read_bytes()
    require(data == _POLICY_BYTES, "Reviewed coverage policy changed during this process")
    return {"path": _POLICY_PATH, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def validate_run(run):
    require(
        isinstance(run, dict) and run.get("run_id") in CASES,
        "Missing-prefix policy is restricted to explicitly reviewed source records",
    )
    policy_reference()
    case = CASES[run["run_id"]]
    for key in ("ordinal", "account_id", "ghost_sha256", "record_time_ms"):
        require(run.get(key) == case[key], "Missing-prefix source identity differs: " + key)
    return dict(case, run_id=run["run_id"])


def record_case_for(run):
    """Return an exact source-record variant, separately from control coverage."""
    require(isinstance(run, dict), "Run identity is missing")
    rid = run.get("run_id")
    if rid not in RECORD_CASES:
        return None
    policy_reference()
    case = RECORD_CASES[rid]
    for key in ("ordinal", "account_id", "ghost_sha256", "record_time_ms"):
        require(run.get(key) == case[key], "Exceptional RecordData source identity differs: " + key)
    return dict(case, run_id=rid, partial_input_coverage=rid in CASES)


def validate_metadata(metadata, run=None):
    """Return declared known coverage; require real metadata, never fabricated rows.

    With run=None, the exact account/map/finish/store identities establish the
    applicable case. Native Replay_Save hashes intentionally differ from ghosts;
    callers must validate the source hash and native equivalence separately.
    """
    m = metadata
    require(m.get("status") == "extracted", "Input metadata has not been extracted")
    finish = m.get("finish_time_ms")
    require(type(finish) is int and finish > 0, "Invalid source finish time")
    checks = m.get("checks", {})
    start = m.get("first_input_timestamp_ms")
    last = m.get("last_input_timestamp_ms")
    require(type(start) is int and type(last) is int and last >= start, "Invalid observed input bounds")
    if checks.get("race_coverage") is True:
        require(start <= 0 and last + 10 >= finish, "Complete coverage claim contradicts observed inputs")
        require(
            m.get("action_coverage") is None or m["action_coverage"].get("fully_covered") is True,
            "Complete coverage contradicts partial coverage declaration",
        )
        return {
            "schema_version": 1,
            "policy": "complete_observed_inputs_v1",
            "race_interval_ms": [0, finish],
            "known_race_interval_ms": [0, finish],
            "original_input_interval_ms": [start, last + 10],
            "missing_race_intervals_ms": [],
            "fully_covered": True,
            "extrapolated": False,
        }
    require(
        checks.get("race_coverage") is False and checks.get("observed_race_coverage") is True,
        "Partial coverage needs explicit negative full-coverage and positive observed-coverage checks",
    )
    require(
        m.get("extractor_version") in ("0.1.3", "0.2.0") and m.get("tick_period_ms") == 10,
        "Unverified partial-coverage extractor/grid",
    )
    if run is None:
        matches = [
            dict(c, run_id=rid)
            for rid, c in CASES.items()
            if c["account_id"] == m.get("account_id") and c["record_time_ms"] == finish
        ]
        require(len(matches) == 1, "Partial coverage is outside the reviewed source allowlist")
        case = matches[0]
    else:
        case = validate_run(run)
    require(
        m.get("map_uid") == MAP_UID
        and m.get("account_id") == case["account_id"]
        and finish == case["record_time_ms"],
        "Partial coverage source map/account/time differs",
    )
    require(
        m.get("container_class") in ("CGameCtnGhost", "CGameCtnReplayRecord"),
        "Unverified partial-coverage source container",
    )
    if m["container_class"] == "CGameCtnGhost":
        require(m.get("source_sha256") == case["ghost_sha256"], "Partial original ghost hash differs")
    stores = m.get("input_stores", [])
    require(len(stores) == 1, "Partial coverage requires one exact original input store")
    store = stores[0]
    expected = {
        "store_index": 0,
        "input_version": case["input_version"],
        "start_offset_ms": 10,
        "ticks": case["ticks"],
        "data_sha256": case["packed_sha256"],
        "packed_file": "input_store_000.bin",
    }
    require(
        all(store.get(k) == v for k, v in expected.items()), "Partial input-store identity or extent differs"
    )
    require(
        start == 10 and last + 10 == case["input_end_ms"] and m.get("all_ticks") == case["ticks"],
        "Partial observed input bounds differ",
    )
    require(
        m.get("files", {}).get("input_store_000.bin", {}).get("sha256") == case["packed_sha256"],
        "Partial raw packed-input file differs",
    )
    expected_coverage = {
        "schema_version": 1,
        "policy": POLICY,
        "race_interval_ms": [0, finish],
        "known_race_interval_ms": [10, finish],
        "original_input_interval_ms": [10, case["input_end_ms"]],
        "missing_race_intervals_ms": [[0, 10]],
        "fully_covered": False,
        "extrapolated": False,
    }
    require(m.get("action_coverage") == expected_coverage, "Missing-prefix metadata contract differs")
    records = [
        s for s in m.get("visual_sample_data", {}).get("sources", []) if s.get("source") == "record_data"
    ]
    require(
        len(records) == 1
        and records[0].get("start_ms") == case["record_start_ms"]
        and records[0].get("end_ms") == case["record_end_ms"],
        "Original RecordData bounds differ",
    )
    return expected_coverage


def training_time_bounds(coverage, alignment_error_ms):
    """Conservatively keep uncertain observation clocks inside known actions."""
    error = Fraction(str(alignment_error_ms))
    require(0 <= error <= 30, "Unverified alignment uncertainty for prefix-gap training")
    first, last = map(Fraction, coverage["known_race_interval_ms"])
    return first + error, last - error
