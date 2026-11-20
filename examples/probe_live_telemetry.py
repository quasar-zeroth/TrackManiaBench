#!/usr/bin/env python3
"""Read fresh Trackmania telemetry without creating an input controller.

Manually restart the chosen map, leave the car stationary at its starting
line, then inspect the repeated position values for expected_start_position.
Each JSON line describes one fresh received packet. Speed is the plugin's raw
value, and timing describes host receipt rather than the simulation clock.
This program does not reset the game, send controls, or capture the desktop.

    python examples/probe_live_telemetry.py --samples 5 --interval 0.2
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", default="127.0.0.1", help="GrabData TCP host (default: localhost)")
    parser.add_argument("--port", type=int, default=9000, help="GrabData TCP port (default: 9000)")
    parser.add_argument(
        "--samples", type=int, default=5, help="Number of fresh packets to print (default: 5)"
    )
    parser.add_argument("--interval", type=float, default=0.2, help="Seconds between reads (default: 0.2)")
    parser.add_argument(
        "--timeout", type=float, default=2.0, help="Connection/fresh-packet timeout in seconds"
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.samples < 1:
        parser.error("--samples must be positive")
    if not math.isfinite(args.interval) or args.interval < 0:
        parser.error("--interval must be finite and nonnegative")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")

    # This source only receives socket data. TMRLBackend would also construct
    # input/capture resources and is intentionally not used by the probe.
    from trackmaniabench.live.tmrl import TMRLTelemetrySource, TelemetryError

    source = None
    previous_sequence = 0
    previous_receipt_ns = None
    try:
        source = TMRLTelemetrySource(args.host, args.port, timeout=args.timeout)
        for index in range(args.samples):
            if index:
                time.sleep(args.interval)
            requested_ns = time.monotonic_ns()
            packet = source.read(after_sequence=previous_sequence, min_received_ns=requested_ns)
            observed_ns = time.monotonic_ns()
            telemetry = packet.as_telemetry()
            print(
                json.dumps(
                    {
                        "sample": index + 1,
                        "position": list(telemetry.position),
                        "position_unit": telemetry.extra["position_unit"],
                        "raw_speed": telemetry.speed,
                        "speed_unit": telemetry.speed_unit,
                        "finished": telemetry.finished,
                        "source_sequence": packet.sequence,
                        "received_monotonic_ns": packet.received_ns,
                        "receipt_age_ms": (observed_ns - packet.received_ns) / 1e6,
                        "inter_receipt_ms": (
                            None
                            if previous_receipt_ns is None
                            else (packet.received_ns - previous_receipt_ns) / 1e6
                        ),
                        "read_wait_ms": (observed_ns - requested_ns) / 1e6,
                        "timing_basis": "host_monotonic_receipt",
                    },
                    allow_nan=False,
                ),
                flush=True,
            )
            previous_sequence = packet.sequence
            previous_receipt_ns = packet.received_ns
    except (TelemetryError, OSError) as error:
        print(f"Telemetry probe failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if source is not None:
            source.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
