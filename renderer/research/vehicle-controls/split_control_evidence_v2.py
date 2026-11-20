#!/usr/bin/env python3
"""Package immutable per-source diagnostic inputs without approving clock alignment."""

import argparse
import json
from pathlib import Path
import analyze_selected_controls as a


def write_new(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if path.exists():
        a.require(path.read_bytes() == payload, "Existing diagnostic differs: " + str(path))
    else:
        path.write_bytes(payload)
    return a.pin(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in (
        "requests",
        "correlation",
        "crosscheck",
        "samples",
        "output",
        "probe-project",
        "primary-catalog",
    ):
        parser.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args()
    folder = Path(__file__).resolve().parent
    root = folder.parents[2]
    requests = json.loads((args.requests).read_text())
    request_by = {r["ordinal"]: r for r in requests["files"]}
    correlation = json.loads((args.correlation).read_text())
    fit_by = {r["ordinal"]: r for r in correlation["records"]}
    crosscheck_path = args.crosscheck
    cross = json.loads(crosscheck_path.read_text())
    cross_by = {r["ordinal"]: r for r in cross["records"]}
    provenance = dict(
        probe_source=a.pin(args.probe_project / "Program.cs"),
        probe_project=a.pin(args.probe_project / "VehicleControlProbe.csproj"),
        probe_dll=a.pin(args.probe_project / "bin/Release/net8.0/VehicleControlProbe.dll"),
        gbx_net_dll=a.pin(args.probe_project / "bin/Release/net8.0/GBX.NET.dll"),
        analyzer=a.pin(folder / "analyze_selected_controls.py"),
        crosschecker=a.pin(folder / "crosscheck_controls_checkpoints_v2.py"),
        source_primary_catalog=a.pin(args.primary_catalog),
    )
    family_ref = a.pin(crosscheck_path)
    index = []
    with (args.samples).open() as source:
        for line in source:
            raw = json.loads(line)
            ordinal = raw["ordinal"]
            request = request_by[ordinal]
            fit = fit_by[ordinal]
            target = args.output / "records" / request["run_id"]
            raw_ref = write_new(target / "raw-controls.json", raw)
            request_ref = write_new(target / "request.json", request)
            fit_ref = write_new(target / "correlation.json", fit)
            cross_ref = write_new(target / "checkpoint-crosscheck.json", cross_by[ordinal])
            evidence = dict(
                schema_version=1,
                status="diagnostic_control_reflection_evidence",
                ordinal=ordinal,
                run_id=request["run_id"],
                raw_controls=raw_ref,
                request=request_ref,
                correlation=fit_ref,
                checkpoint_crosscheck=cross_ref,
                family_calibration=family_ref,
                extraction_and_analysis=provenance,
                source_clock_approved=False,
                video_alignment_verified=False,
                method="Recompute analyzer.inspect_run(raw_controls,request,1000) and compare every field with correlation. The family report compares exact-control sources with independently typed checkpoint times. Control reflection lag is not exact physical-state clock phase.",
            )
            ref = write_new(target / "evidence.json", evidence)
            index.append(
                dict(
                    ordinal=ordinal,
                    run_id=request["run_id"],
                    exact_controls=fit["status"] == "exact_control_reflection_correlated",
                    exact_checkpoint_series=cross_by[ordinal]["exact_checkpoint_series_verified"],
                    evidence=ref,
                )
            )
    index_ref = write_new(
        args.output / "evidence-index-v2.json",
        dict(
            schema_version=1,
            status="diagnostic_per_source_control_evidence_packaged",
            source_manifest=requests["manifest"],
            requests=a.pin(args.requests),
            raw_selected_samples=a.pin(args.samples),
            correlations=a.pin(args.correlation),
            family_calibration=family_ref,
            splitter=a.pin(__file__),
            runs=index,
            production_clock_approval=False,
        ),
    )
    print(json.dumps(dict(records=len(index), evidence_index=index_ref)))


if __name__ == "__main__":
    main()
