"""Configuration, collection and discoverable access to the production tools."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

from trackmaniabench import __version__
from trackmaniabench.config import REPOSITORY, load

# Versioned implementations retain evidence-schema compatibility. This catalog
# gives them stable, human-facing names without copying runtime data into code.
TOOLS = {
    "extract": "dataset/batch_extract_partial_v1.py",
    "prepare-scenes": "dataset/prepare_native_scenes_partial_v1.py",
    "source-clocks": "dataset/build_source_clocks_v2.py",
    "finalize-clocks": "dataset/finalize_native_clock_manifest_v2.py",
    "render": "setup/render.py",
    "render-config": "setup/build_render_config.py",
    "native-ui": "setup/native_ui_worker.py",
    "ui-plans": "setup/build_ui_plans.py",
    "pipeline-spec": "setup/build_pipeline_spec.py",
    "supervisor": "renderer/durable_dataset_supervisor_v1.py",
    "scanner-validation": "renderer/validate_video_scanner.py",
    "audit": "renderer/production_video_audit_conditional_v2.py",
    "audit-automatic": "renderer/production_video_audit_automatic_v2.py",
    "review-exceptions": "renderer/production_video_audit_reviewed_exceptions_v1.py",
    "prepare-audits": "renderer/prepare_audit_queue_conditional_v2.py",
    "calibrate-pilot": "dataset/build_measured_calibration_automatic_v2.py",
    "pilot-gate": "dataset/assemble_actual_pilot_gate.py",
    "calibrate": "dataset/calibrate_production_automatic_v2.py",
    "join": "dataset/join_dataset_automatic_v2.py",
    "export": "dataset/export_portable_automatic_v2.py",
    "verify": "dataset/export_portable_automatic_v2.py",
}


def init(args):
    target = Path(args.output).expanduser().resolve()
    if target.exists():
        raise ValueError(f"Configuration already exists: {target}")
    example = (Path(__file__).parent / "example.toml").read_text()
    if args.map_uid:
        from collector.common import safe_uid

        example = example.replace("XJ_JEjWGoAexDWe8qfaOjEcq5l8", safe_uid(args.map_uid))
    if args.count is not None:
        if not 1 <= args.count <= 250:
            raise ValueError("Count must be 1..250")
        example = example.replace("count = 250", f"count = {args.count}")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Choose an ignored folder alongside a normal checkout-local config.
    example = example.replace('workspace = "../data/summer2020-01"', 'workspace = "./data/cohort"')
    target.write_text(example)
    print(f"Created {target}; edit track, dataset, paths and remote settings.")
    return 0


def doctor(args):
    config = load(args.config)
    required = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
    python_modules = {name: importlib.util.find_spec(name) is not None for name in ("numpy", "PIL")}
    optional = {name: shutil.which(name) for name in ("dotnet", "ssh", "tesseract")}
    scripts = {name: (REPOSITORY / path).is_file() for name, path in TOOLS.items()}
    result = {
        "version": __version__,
        "python": sys.version.split()[0],
        "workspace": str(config.workspace),
        "map_uid": config.map_uid,
        "count": config.count,
        "required_tools": required,
        "python_modules": python_modules,
        "build_and_remote_tools": optional,
        "durable_supervisor_capabilities": {
            "pidfd_open": hasattr(os, "pidfd_open"),
            "pidfd_send_signal": hasattr(signal, "pidfd_send_signal"),
        },
        "pipeline_tools": scripts,
        "native_game_check": "not performed; doctor makes no SSH connection or power changes",
    }
    print(json.dumps(result, indent=2))
    return 0 if all(required.values()) and all(python_modules.values()) and all(scripts.values()) else 1


def plan(args):
    config = load(args.config)
    root = config.workspace
    pilot_count = min(5, config.count)
    stages = [
        ("snapshot", "Freeze record UUIDs for the entire cohort", root / "snapshot/leaderboard.json"),
        (
            f"download --count {pilot_count}",
            "Acquire and hash the pilot ghosts",
            root / "collection/pilot-manifest.json",
        ),
        ("batch", "Generate signed GhostToReplay batch; run conversion in game", root / "replay-batch.txt"),
        (
            "tool extract",
            "Decode original controls and check account/map/time",
            root / "actions/action_dataset_manifest.json",
        ),
        (
            "tool prepare-scenes",
            "Compare native replays and prepare clean/diagnostic scenes",
            root / "scenes/manifest.json",
        ),
        ("tool render", "Render with installation-specific UI references", root / "render-state"),
        ("tool audit", "Review scene, camera, visibility, marker and alignment evidence", root / "audits"),
        (
            "tool calibrate-pilot + tool pilot-gate",
            "Approve measured first-five evidence before scaling",
            root / "pilot-gate.json",
        ),
        (
            f"download --count {config.count}",
            "Promote the same frozen cohort after the gate passes",
            root / "collection/manifest.json",
        ),
        (
            "tool calibrate + tool join + tool export",
            "Repeat rendering/QA; join and package verified runs",
            root / "release/release_manifest.json",
        ),
        (
            "tool verify -- --verify RELEASE",
            "Recompute all portable inventory hashes and audit closure",
            root / "release/release_manifest.json",
        ),
    ]
    print(
        json.dumps(
            {
                "map_uid": config.map_uid,
                "count": config.count,
                "workspace": str(root),
                "stages": [
                    {
                        "command": command,
                        "purpose": purpose,
                        "expected_artifact": str(path),
                        "exists": path.exists(),
                        "verified": False,
                    }
                    for command, purpose, path in stages
                ],
                "note": "File existence is not verification. See docs/workflow.md for measured review gates.",
            },
            indent=2,
        )
    )
    return 0


def collect(args):
    config = load(args.config)
    workspace = config.workspace
    if args.command == "snapshot":
        from collector.snapshot import freeze

        result = freeze(
            config.map_uid, config.count, config.cohort, workspace / "snapshot", config.user_agent
        )
        print(result)
    elif args.command == "download":
        from collector.download import collect as download
        from collector.common import validate_snapshot

        snapshot = validate_snapshot(json.loads((workspace / "snapshot/leaderboard.json").read_text()))
        if (snapshot["mapUid"], snapshot["cohort"], snapshot["count"]) != (
            config.map_uid,
            config.cohort,
            config.count,
        ):
            raise ValueError(
                "Snapshot map/cohort/count differs from configuration; use the original config or a new workspace"
            )
        count = args.count if args.count is not None else min(5, config.count)
        if count > config.count:
            raise ValueError("Download count exceeds configured cohort")
        result = download(
            workspace / "snapshot/leaderboard.json",
            workspace / "collection",
            count,
            config.user_agent,
            config.path("map_file"),
            args.gate or workspace / "pilot-gate.json",
            args.pilot or workspace / "collection/pilot-manifest.json",
            args.render_output or workspace / "rendered",
            args.preflight,
        )
        if not args.preflight and count == 5:
            manifest = workspace / "collection/manifest.json"
            target = workspace / "collection/pilot-manifest.json"
            if target.exists():
                # Collection timestamps may differ on resume. Preserve the first pilot.
                from collector.download import verify_run

                original = json.loads(target.read_text())
                new = json.loads(manifest.read_text())
                if (
                    original.get("requested_count") != 5
                    or len(original.get("runs", [])) != 5
                    or original.get("failures")
                ):
                    raise ValueError("Saved pilot manifest is incomplete")
                if original["snapshot_sha256"] != new["snapshot_sha256"] or original["map"] != new["map"]:
                    raise ValueError("Pilot snapshot or map changed")
                from collector.common import validate_snapshot

                snapshot = validate_snapshot(
                    json.loads((workspace / "snapshot/leaderboard.json").read_text())
                )
                for row, entry in zip(original["runs"], snapshot["entries"][:5]):
                    verify_run(row, entry)
            else:
                shutil.copyfile(manifest, target)
        print(json.dumps(result))
    elif args.command == "batch":
        from collector.batch import write_batch

        source_manifest = args.manifest or workspace / "collection/manifest.json"
        manifest = json.loads(source_manifest.read_text())
        if (manifest["map"]["uid"], manifest["cohort"], manifest["snapshot_count"]) != (
            config.map_uid,
            config.cohort,
            config.count,
        ):
            raise ValueError("Batch manifest map/cohort/count differs from configuration")
        result = write_batch(
            source_manifest,
            args.output or workspace / "replay-batch.txt",
            args.limit,
        )
        print(
            json.dumps(
                {
                    "records": result,
                    "note": "Paste the batch into the unmodified signed GhostToReplay plugin.",
                }
            )
        )
    return 0


def run_tool(args):
    config = load(args.config) if args.config else None
    env = config.environment() if config else os.environ.copy()
    path = REPOSITORY / TOOLS[args.name]
    if not path.is_file():
        raise ValueError(f"Missing pipeline component: {path}")
    roots = [str(REPOSITORY)] + [str(REPOSITORY / p) for p in ("renderer", "dataset", "extractor", "setup")]
    if env.get("PYTHONPATH"):
        roots.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(roots)
    arguments = args.arguments
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    return subprocess.run([sys.executable, str(path), *arguments], env=env, check=False).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description="Trackmania native replay dataset pipeline")
    parser.add_argument("--version", action="version", version=__version__)
    subs = parser.add_subparsers(dest="command", required=True)
    initialize = subs.add_parser("init", help="Create an editable configuration")
    initialize.add_argument("--output", default="bench.local.toml")
    initialize.add_argument("--map-uid")
    initialize.add_argument("--count", type=int)
    initialize.set_defaults(handler=init)
    for name, handler in (
        ("doctor", doctor),
        ("plan", plan),
        ("snapshot", collect),
        ("download", collect),
        ("batch", collect),
    ):
        sub = subs.add_parser(name)
        sub.add_argument("--config", default="bench.local.toml")
        sub.set_defaults(handler=handler)
        if name == "download":
            sub.add_argument("--count", type=int)
            sub.add_argument("--gate", type=Path)
            sub.add_argument("--pilot", type=Path)
            sub.add_argument("--render-output", type=Path)
            sub.add_argument("--preflight", action="store_true")
        if name == "batch":
            sub.add_argument("--manifest", type=Path)
            sub.add_argument("--output", type=Path)
            sub.add_argument("--limit", type=int)
    sub = subs.add_parser("tool", help="Run a production component; use 'tool NAME -- --help'")
    sub.add_argument("--config", help="Machine configuration for the native renderer")
    sub.add_argument("name", choices=sorted(TOOLS))
    sub.add_argument("arguments", nargs=argparse.REMAINDER)
    sub.set_defaults(handler=run_tool)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (KeyError, OSError, ValueError, RuntimeError) as exc:
        print(f"tmbench: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
