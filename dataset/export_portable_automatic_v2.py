#!/usr/bin/env python3
"""Publish verified training files with relative paths and an unchanged audit archive.

Export requires the original hash-pinned graph and performs a fresh join audit.
`--verify RELEASE` is standalone: only Python's standard library is needed, and
the original absolute source paths are never accessed during release verification.
"""

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
from types import SimpleNamespace


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def relative_file(root, name):
    path = PurePosixPath(name)
    require(
        isinstance(name, str) and not path.is_absolute() and ".." not in path.parts and "\\" not in name,
        "Unsafe portable relative path",
    )
    target = root.joinpath(*path.parts)
    require(target.resolve().is_relative_to(root.resolve()), "Portable file escapes release directory")
    return target


def pin(path, root):
    path = Path(path)
    return dict(path=path.relative_to(root).as_posix(), sha256=sha(path), bytes=path.stat().st_size)


def references(value):
    if isinstance(value, dict):
        if isinstance(value.get("path"), str) and isinstance(value.get("sha256"), str):
            yield value
        for child in value.values():
            yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)


def source_ref(ref, base):
    p = Path(ref["path"])
    # Lexical normalization only: offline verification never touches old hosts.
    return Path(os.path.normpath(str(p if p.is_absolute() else base / p)))


class Archive:
    def __init__(self, root):
        self.root = root
        self.entries = {}
        self.hash_locations = {}
        self.inventory = {}
        self.inspected = set()
        self.queue = []

    def copy(self, source, name, expected):
        source = Path(source).resolve(strict=True)
        require(sha(source) == expected, "Source hash changed: " + str(source))
        target = relative_file(self.root, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            require(sha(target) == expected, "Existing release file changed: " + name)
        else:
            temporary = target.with_name(target.name + ".copying")
            shutil.copyfile(source, temporary)
            require(sha(temporary) == expected, "Copied file hash changed")
            os.replace(temporary, target)
        ref = pin(target, self.root)
        self.inventory[ref["path"]] = ref
        self.hash_locations.setdefault(expected, ref)
        self.entries[(str(source), expected)] = ref
        if source.suffix.lower() == ".json" and str(source) not in self.inspected:
            self.queue.append(source)
            self.inspected.add(str(source))
        return ref

    def archive(self, source, expected=None):
        source = Path(source)
        digest = expected or sha(source)
        key = (str(source), digest)
        if key in self.entries:
            return self.entries[key]
        if not source.is_file() or sha(source) != digest:
            require(
                digest in self.hash_locations,
                "Historical source version unavailable: " + str(source) + " @ " + digest,
            )
            self.entries[key] = self.hash_locations[digest]
            return self.entries[key]
        name = self.hash_locations.get(digest, {}).get("path", f"audit/objects/{digest[:2]}/{digest}")
        return self.copy(source, name, digest)

    def drain(self):
        deferred = []
        while self.queue:
            source = self.queue.pop(0)
            for ref in references(read(source)):
                path = source_ref(ref, source.parent)
                if (not path.is_file() or sha(path) != ref["sha256"]) and ref[
                    "sha256"
                ] not in self.hash_locations:
                    deferred.append((path, ref["sha256"]))
                else:
                    self.archive(path, ref["sha256"])
        # Historical code versions may be reachable later through an explicit
        # preserved-source provenance document. Match bytes by SHA, never by name.
        for path, digest in deferred:
            self.archive(path, digest)

    def write(self, name, data):
        target = relative_file(self.root, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            require(target.read_bytes() == data, "Existing generated release file changed: " + name)
        else:
            target.write_bytes(data)
        ref = pin(target, self.root)
        self.inventory[name] = ref
        return ref


def verify_release(folder):
    root = Path(folder).resolve(strict=True)
    manifest = read(root / "release_manifest.json")
    require(manifest.get("status") == "portable_dataset_verified", "Incomplete portable release")
    count = manifest["requested_count"]
    runs = manifest["runs"]
    require(type(count) is int and count > 0 and len(runs) == count, "Release cohort count differs")
    require([r["ordinal"] for r in runs] == list(range(1, count + 1)), "Release ordinals incomplete")
    for key in ("run_id", "account_id"):
        require(len({r[key] for r in runs}) == count, "Duplicate portable " + key)
    files = manifest["files"]
    require(len({f["path"] for f in files}) == len(files), "Duplicate release inventory paths")
    inventory = {ref["path"]: ref for ref in files}
    for ref in files:
        path = relative_file(root, ref["path"])
        require(
            path.is_file() and path.stat().st_size == ref["bytes"] and sha(path) == ref["sha256"],
            "Portable file hash/size mismatch: " + ref["path"],
        )
    for ref in references({k: v for k, v in manifest.items() if k != "files"}):
        require(inventory.get(ref["path"]) == ref, "Manifest reference absent from release inventory")
    index = read(relative_file(root, manifest["audit_archive"]["path"]))
    locations = {
        (entry["original_path"], entry["release_file"]["sha256"]): entry["release_file"]
        for entry in index["entries"]
    }
    require(len(locations) == len(index["entries"]), "Duplicate original audit source")
    for (source, _), ref in locations.items():
        require(inventory.get(ref["path"]) == ref, "Audit archive file missing from inventory")
        if Path(source).suffix.lower() == ".json":
            for nested in references(read(relative_file(root, ref["path"]))):
                original = str(source_ref(nested, Path(source).parent))
                require(
                    (original, nested["sha256"]) in locations,
                    "Portable audit closure missing or changed: " + original,
                )
    totals = dict(
        frames=0,
        training_observations=0,
        valid_transitions=0,
        experimental_training_observations=0,
        experimental_valid_transitions=0,
        video_training_observations=0,
    )
    split_counts = {}
    for row in runs:
        summary = read(relative_file(root, row["summary"]["path"]))
        require(
            summary["run_id"] == row["run_id"] and summary["account_id"] == row["account_id"],
            "Portable run identity differs",
        )
        frames = [
            json.loads(line)
            for line in relative_file(root, row["frame_actions"]["path"]).read_text().splitlines()
        ]
        require(
            [f["frame_index"] for f in frames] == list(range(len(frames))), "Frame indices not contiguous"
        )
        require(len(frames) == row["frames"] == summary["frames"], "Portable frame count differs")
        require(
            sum(f["training_eligible"] for f in frames) == row["training_observations"],
            "Training frame count differs",
        )
        require(
            sum(f["transition_eligible"] for f in frames) == row["valid_transitions"],
            "Transition count differs",
        )
        require(
            sum(f.get("experimental_training_eligible", f["training_eligible"]) for f in frames)
            == row["experimental_training_observations"],
            "Experimental training count differs",
        )
        require(
            sum(f.get("experimental_transition_eligible", f["transition_eligible"]) for f in frames)
            == row["experimental_valid_transitions"],
            "Experimental transition count differs",
        )
        require(
            sum(
                f.get("video_training_eligible", f["phase"] == "race" and f["observation_usable"])
                for f in frames
            )
            == row["video_training_observations"],
            "Video-only count differs",
        )
        if row["timing_evidence_class"] == "conditional_control_reflection":
            require(
                row["training_observations"] == row["valid_transitions"] == 0,
                "Conditional source admitted to strict training",
            )
            require(
                summary["mapping"]["max_absolute_error_ms"] is None,
                "Conditional source declares unconditional timing bound",
            )
        for ref in references(summary):
            require(inventory.get(ref["path"]) == ref, "Run reference missing from inventory")
        for key in totals:
            totals[key] += row[key]
        split_counts[row["split"]] = split_counts.get(row["split"], 0) + 1
    require(totals == manifest["totals"], "Portable cohort totals differ")
    require(split_counts == manifest["split_counts"], "Portable player split counts differ")
    if count <= 5:
        require(split_counts == {"pilot": count}, "Pilot runs must retain pilot labels")
    else:
        from fractions import Fraction

        values = [Fraction(x) for x in manifest.get("split_fractions", "0.8,0.1,0.1").split(",")]
        require(
            len(values) == 3 and all(x >= 0 for x in values) and sum(values) == 1, "Invalid split fractions"
        )
        sizes = [int(count * x) for x in values]
        for i in sorted(range(3), key=lambda i: (-(count * values[i] - sizes[i]), i))[: count - sum(sizes)]:
            sizes[i] += 1
        expected = {k: v for k, v in zip(("train", "validation", "test"), sizes) if v}
        require(split_counts == expected, "Player split allocation differs from declared fractions")
        ordered = sorted(
            runs,
            key=lambda r: (
                hashlib.sha256((manifest["split_seed"] + "\0" + r["account_id"]).encode()).hexdigest(),
                r["account_id"],
            ),
        )
        for i, row in enumerate(ordered):
            label = "train" if i < sizes[0] else "validation" if i < sum(sizes[:2]) else "test"
            require(row["split"] == label, "Player split membership differs from declared seed")
    return dict(
        status="portable_release_hashes_and_audit_closure_verified",
        runs=count,
        files=len(files),
        bytes=sum(f["bytes"] for f in files),
        **totals,
        source_absolute_paths_accessed=False,
    )


def export(args):
    import join_dataset_automatic_v2 as j

    source = Path(args.dataset).resolve(strict=True)
    original = read(source)
    require(
        original.get("status") == "dataset_verified"
        and type(original["requested_count"]) is int
        and original["requested_count"] > 0,
        "Verified complete cohort required",
    )
    gate_path = Path(args.gate).resolve(strict=True) if args.gate else None
    actions_path = Path(original["inputs"]["actions"]).resolve(strict=True)
    actions = read(actions_path)
    render_root = Path(original["inputs"]["render_root"]).resolve(strict=True)
    if original["requested_count"] > 5:
        require(gate_path is not None, "Production export requires the measured five-run pilot gate")
    if gate_path is not None:
        j.renderer.validate_gate(gate_path, actions, render_root)
    root = Path(args.output).resolve()
    require(not source.is_relative_to(root), "Release must be separate from source dataset")
    supplemental = [Path(p).resolve(strict=True) for p in args.archive_reference]
    identity = dict(
        source_dataset_sha256=sha(source),
        pilot_gate_sha256=sha(gate_path) if gate_path else None,
        exporter_sha256=sha(Path(__file__)),
        supplemental_archive_sha256=sorted(sha(p) for p in supplemental),
    )
    if (root / "release_manifest.json").exists():
        require(
            read(root / "release_manifest.json")["source_identity"] == identity,
            "Completed release source/tool changed",
        )
        return verify_release(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".export.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        archive = Archive(root)
        archive.write("export_identity.json", encode(identity))
        # The existing joiner redecodes all native videos and reconstructs every
        # interval while requiring unchanged saved output bytes.
        audit_args = SimpleNamespace(
            actions=str(actions_path),
            render_root=str(render_root),
            calibrations=original["inputs"]["calibrations"],
            output=str(source.parent),
            count=original["requested_count"],
            gate=original["inputs"]["gate"],
            split_seed=original["split_seed"],
            split_fractions=original.get("split_fractions", "0.8,0.1,0.1"),
            audit=True,
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            j.execute(audit_args)
        audit_log = archive.write("audit/export-time-join-audit.jsonl", buffer.getvalue().encode())
        map_ref = archive.copy(original["map"]["path"], "map/track.Map.Gbx", original["map"]["sha256"])
        source_by_id = {r["run_id"]: r for r in actions["runs"]}
        runs = []
        totals = dict(
            frames=0,
            training_observations=0,
            valid_transitions=0,
            experimental_training_observations=0,
            experimental_valid_transitions=0,
            video_training_observations=0,
        )
        for item in original["runs"]:
            rid = item["run_id"]
            run = source_by_id[rid]
            summary_path = Path(item["summary"]["path"])
            summary = read(summary_path)
            folder = f"runs/{rid}"
            meta_path = Path(summary["action_metadata"]["path"])
            metadata = read(meta_path)
            video = archive.copy(summary["video"]["path"], folder + "/video.webm", summary["video"]["sha256"])
            joined = archive.copy(
                item["frame_actions"]["path"],
                folder + "/frame_actions.jsonl",
                item["frame_actions"]["sha256"],
            )
            ghost = archive.copy(run["ghost_path"], folder + "/source.Ghost.Gbx", run["ghost_sha256"])
            action_refs = {
                name: archive.copy(meta_path.parent / name, folder + "/actions/" + name, ref["sha256"])
                for name, ref in metadata["files"].items()
            }
            originals = {
                name: archive.archive(ref["path"], ref["sha256"])
                for name, ref in [
                    ("action_metadata", summary["action_metadata"]),
                    ("calibration", summary["calibration"]),
                    ("render_result", summary["render_result"]),
                    ("joined_summary", item["summary"]),
                ]
            }
            window = {k: v for k, v in summary["observation_window"].items() if k != "evidence_files"}
            portable = dict(
                schema_version=1,
                status="portable_run_verified",
                run_id=rid,
                account_id=run["account_id"],
                ordinal=run["ordinal"],
                rank=run["rank"],
                finish_time_ms=run["record_time_ms"],
                map_uid=original["map_uid"],
                split=item["split"],
                video=video,
                source_ghost=ghost,
                frame_actions=joined,
                actions=action_refs,
                original_provenance=originals,
                mapping=summary["mapping"],
                observation_window=window,
                settings=summary["settings"],
                renderer_backend=summary["renderer_backend"],
                renderer_build=summary["renderer_build"],
                semantics=summary["semantics"],
                frames=summary["frames"],
                training_observations=summary["training_frames"],
                valid_transitions=summary["transition_frames"],
                raw_race_coverage_ms=summary["race_coverage_ms"],
                video_probe=summary["video_probe"],
                action_coverage=summary["action_coverage"],
                source_game_build=metadata.get("game_build"),
                source_input_versions=sorted({store["input_version"] for store in metadata["input_stores"]}),
                full_race_duration_ms=run["record_time_ms"],
                missing_action_race_ms=summary.get("missing_action_race_ms", 0),
                training_time_guard_ms=summary.get("training_time_guard_ms", 0),
            )
            scope = summary.get("source_clock_contract")
            portable["timing_evidence_class"] = item["timing_evidence_class"]
            # Source proof references remain inside the immutable audit archive.
            portable["source_clock_scope"] = {k: v for k, v in (scope or {}).items() if k != "source_proof"}
            portable.update(
                experimental_training_observations=item["experimental_training_frames"],
                experimental_valid_transitions=item["experimental_transition_frames"],
                video_training_observations=item["video_training_frames"],
            )
            summary_ref = archive.write(folder + "/run.json", encode(portable))
            rows = dict(
                run_id=rid,
                account_id=run["account_id"],
                ordinal=run["ordinal"],
                rank=run["rank"],
                finish_time_ms=run["record_time_ms"],
                split=item["split"],
                summary=summary_ref,
                video=video,
                frame_actions=joined,
                frames=summary["frames"],
                training_observations=summary["training_frames"],
                valid_transitions=summary["transition_frames"],
                timing_evidence_class=item["timing_evidence_class"],
                experimental_training_observations=item["experimental_training_frames"],
                experimental_valid_transitions=item["experimental_transition_frames"],
                video_training_observations=item["video_training_frames"],
            )
            runs.append(rows)
            for key in totals:
                totals[key] += rows[key]
            print(
                json.dumps(dict(ordinal=run["ordinal"], run_id=rid, status="portable_training_files_copied")),
                flush=True,
            )
        archived_source = archive.archive(source)
        archived_gate = archive.archive(gate_path) if gate_path else None
        archive.archive(actions_path)
        for extra in supplemental:
            archive.archive(extra)
        archive.drain()
        audit_index = archive.write(
            "audit/path-index.json",
            encode(
                dict(
                    schema_version=1,
                    semantics="Original source JSON bytes are unchanged. Resolve their historical absolute/relative references through this index; original host paths need not exist.",
                    entries=[
                        dict(original_path=key[0], release_file=archive.entries[key])
                        for key in sorted(archive.entries)
                    ],
                )
            ),
        )
        verifier = archive.copy(Path(__file__), "tools/export_portable.py", sha(Path(__file__)))
        readme = archive.write(
            "README.txt",
            (
                "TrackManiaBench measured dataset\n\n"
                "All runtime paths in release_manifest.json and runs/*/run.json are relative to this release root.\n"
                "Videos preserve every native frame. Use training_eligible and transition_eligible for the directly checkpoint-supported action-aligned subset.\n"
                "Conditional control-reflection runs have false strict masks and null unconditional timing bounds. experimental_training_eligible and experimental_transition_eligible retain candidate pairs under the stated assumptions; they do not establish a uniform physical-state clock guarantee.\n"
                "For video-only autoregressive training, use video_training_eligible where present; otherwise select phase=race and observation_usable=true.\n"
                "frame_actions.jsonl uses actual video PTS and retains every observed100Hz control substep. Explicit unknown input intervals contain no fabricated controls. All original actions/events/stores and ghosts remain available.\n"
                "Timing evidence, assumptions, ambiguity and measured or conditional bounds are recorded per run. For incomplete input coverage, masks also apply the corresponding uncertainty margin inside known controls.\n"
                "Missing endpoint images and controls are not synthesized; inspect each run observation_window, action_coverage and training_time_guard_ms.\n"
                "Audit objects preserve original JSON bytes and historical host paths. audit/path-index.json resolves their full hash-bound file graph locally.\n"
                "Verify after transfer without the source host: python3 tools/export_portable.py --verify .\n"
            ).encode(),
        )
        manifest = dict(
            schema_version=1,
            status="portable_dataset_verified",
            requested_count=original["requested_count"],
            source_identity=identity,
            map_uid=original["map_uid"],
            map=map_ref,
            settings=original["settings"],
            renderer_backend=original["renderer_backend"],
            renderer_build=original["renderer_build"],
            split_seed=original["split_seed"],
            split_fractions=original.get("split_fractions", "0.8,0.1,0.1"),
            split_counts=original["split_counts"],
            totals=totals,
            runs=runs,
            timing_evidence_counts=original["timing_evidence_counts"],
            audit_archive=audit_index,
            original_dataset_manifest=archived_source,
            original_pilot_gate=archived_gate,
            export_time_join_audit=audit_log,
            standalone_verifier=verifier,
            readme=readme,
            original_audit_json_bytes_preserved=True,
            training_paths_relative_to_release_root=True,
            files=[archive.inventory[key] for key in sorted(archive.inventory)],
        )
        (root / "release_manifest.json").write_bytes(encode(manifest))
        verification = verify_release(root)
        return verification


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset")
    parser.add_argument("--gate")
    parser.add_argument("--output")
    parser.add_argument("--verify")
    parser.add_argument(
        "--archive-reference",
        action="append",
        default=[],
        help="Additional preserved-source provenance root; repeat as needed",
    )
    args = parser.parse_args()
    try:
        if args.verify:
            require(
                not any((args.dataset, args.gate, args.output, args.archive_reference)),
                "--verify takes only a release directory",
            )
            result = verify_release(args.verify)
        else:
            require(
                all((args.dataset, args.output)),
                "--dataset and --output are required; production also requires --gate",
            )
            result = export(args)
        print(json.dumps(result))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("Rejected: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
