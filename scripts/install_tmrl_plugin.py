#!/usr/bin/env python3
"""Install the unchanged upstream signed telemetry plugin, without TmrlData setup."""

import argparse
import hashlib
from io import BytesIO
from pathlib import Path
import urllib.request
import zipfile

URL = "https://github.com/trackmania-rl/tmrl/releases/download/v0.6.0/resources.zip"
MEMBER = "resources/Plugins/TMRL_GrabData.op"
SHA256 = "887317f74e9cca06ede9a119634f33031a27c71b166da90d2bca6c271f4b05ce"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("plugins_directory", type=Path, help="Existing OpenplanetNext/Plugins directory")
    a = p.parse_args()
    if not a.plugins_directory.is_dir():
        p.error("Select the existing Openplanet Plugins directory")
    target = a.plugins_directory / "TMRL_GrabData.op"
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() == SHA256:
            print("The pinned signed GrabData plugin is already installed")
            return
        p.error("A different GrabData plugin exists; preserve/review it before replacement")
    request = urllib.request.Request(URL, headers={"User-Agent": "TrackManiaBench/0.2"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise RuntimeError("Unexpectedly large upstream archive")
    with zipfile.ZipFile(BytesIO(data)) as archive:
        info = archive.getinfo(MEMBER)
        if info.file_size > 65536:
            raise RuntimeError("Unexpectedly large plugin")
        plugin = archive.read(MEMBER)
    if hashlib.sha256(plugin).hexdigest() != SHA256:
        raise RuntimeError("Plugin does not match the reviewed upstream hash")
    with target.open("xb") as stream:
        stream.write(plugin)
    print(f"Installed signed upstream plugin: {target}; restart/reload Openplanet to activate")


if __name__ == "__main__":
    main()
