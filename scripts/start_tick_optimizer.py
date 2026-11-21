#!/usr/bin/env python3
"""Start the separately installed native Linux TICK optimizer in the foreground.

This configures the official optimizer's game resource paths. It does not
download TICK, modify the game, inject a DLL, or start a desktop overlay.
Ctrl+C stops the optimizer. See docs/tickgym-setup.md for prerequisites.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--optimizer", required=True, type=Path, help="Installed TICK optimizer executable")
    parser.add_argument("--game-data", required=True, type=Path)
    parser.add_argument("--user-data", required=True, type=Path, help="Game's Documents/Trackmania directory")
    parser.add_argument("--packs", required=True, type=Path)
    parser.add_argument("--origin", default="http://127.0.0.1:46323")
    parser.add_argument(
        "--glibc-library-directory", type=Path,
        help="Optional isolated libc directory containing ld-linux-x86-64.so.2; does not replace system libc",
    )
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        parser.error("This launcher is for the native Linux optimizer")
    try:
        address = urlsplit(args.origin)
        loopback = address.hostname == "localhost" or ipaddress.ip_address(address.hostname).is_loopback
        if (address.scheme != "http" or not loopback or address.port is None or address.port == 0
                or address.username or address.password or address.path not in ("", "/")
                or address.query or address.fragment):
            raise ValueError("Expected a loopback HTTP origin with an explicit port")
    except (TypeError, ValueError) as error:
        parser.error(f"Invalid --origin: {error}")
    optimizer = args.optimizer.expanduser().resolve()
    if not optimizer.is_file() or not os.access(optimizer, os.X_OK):
        parser.error("--optimizer must name an executable file")
    environment = dict(os.environ)
    environment["TickOptimizer__ListenUrl"] = args.origin.rstrip("/")
    for argument, name in ((args.game_data, "GameData"), (args.user_data, "UserData"), (args.packs, "Packs")):
        path = argument.expanduser().resolve()
        if not path.is_dir():
            parser.error(f"{name} is not a directory: {path}")
        environment[f"TickOptimizer__{name}"] = str(path)
    command = [str(optimizer)]
    if args.glibc_library_directory:
        library = args.glibc_library_directory.expanduser().resolve()
        loader = library / "ld-linux-x86-64.so.2"
        if not loader.is_file() or not os.access(loader, os.X_OK) or not (library / "libc.so.6").is_file():
            parser.error("--glibc-library-directory must contain an executable loader and libc.so.6")
        command = [str(loader), "--library-path", str(library), str(optimizer)]
    os.chdir(optimizer.parent)
    os.execvpe(command[0], command, environment)


if __name__ == "__main__":
    main()
