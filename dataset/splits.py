"""Deterministic whole-player splitting with explicit allocation fractions."""

from fractions import Fraction
import hashlib

DEFAULT_FRACTIONS = "0.8,0.1,0.1"


def fractions(value=DEFAULT_FRACTIONS):
    values = tuple(Fraction(str(x)) for x in (value.split(",") if isinstance(value, str) else value))
    if len(values) != 3 or any(x < 0 for x in values) or sum(values) != 1:
        raise ValueError("Split fractions must be three nonnegative values summing exactly to one")
    return values


def allocation(count, value=DEFAULT_FRACTIONS):
    if type(count) is not int or count < 1:
        raise ValueError("Cohort count must be a positive integer")
    values = fractions(value)
    sizes = [int(count * x) for x in values]
    remainder = count - sum(sizes)
    order = sorted(range(3), key=lambda i: (-(count * values[i] - sizes[i]), i))
    for i in order[:remainder]:
        sizes[i] += 1
    return dict(zip(("train", "validation", "test"), sizes))


def split_runs(runs, seed, value=DEFAULT_FRACTIONS):
    if len({r["account_id"] for r in runs}) != len(runs):
        raise ValueError("One leaderboard record per player is required")
    fractions(value)
    if len(runs) <= 5:
        return {r["run_id"]: "pilot" for r in runs}
    sizes = allocation(len(runs), value)
    order = sorted(
        runs,
        key=lambda r: (hashlib.sha256((seed + "\0" + r["account_id"]).encode()).hexdigest(), r["account_id"]),
    )
    return {
        r["run_id"]: (
            "train"
            if i < sizes["train"]
            else "validation"
            if i < sizes["train"] + sizes["validation"]
            else "test"
        )
        for i, r in enumerate(order)
    }
