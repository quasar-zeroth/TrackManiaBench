#!/usr/bin/env python3
"""Run the portable offline Python suites without contacting a game or API."""

from pathlib import Path
import os
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        str(root / name) for name in ("", "renderer", "dataset", "setup", "extractor")
    )
    for directory in ("tests", "dataset", "renderer", "setup"):
        command = [sys.executable, "-m", "unittest", "discover", "-s", directory, "-p", "test_*.py", "-v"]
        print(f"Running {directory} tests", flush=True)
        result = subprocess.run(command, cwd=root, env=env, check=False)
        if result.returncode:
            return result.returncode
    return subprocess.run(
        [sys.executable, str(root / "setup/native_ui_worker.py"), "self-test"],
        cwd=root,
        env=env,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
