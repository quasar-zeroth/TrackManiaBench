"""Explicit machine configuration for the native Linux/Proton renderer.

No host is guessed and no network call is made on import. The controller embeds
these variables into the remote Python payload, without forwarding other env.
"""

import os
from pathlib import Path
import re

KEYS = (
    "SSH_HOST",
    "EXPECTED_HOSTNAME",
    "GAME_DOCUMENTS",
    "REMOTE_DATA",
    "DISPLAY",
    "XAUTHORITY",
    "XDOTOOL",
    "LIBRARY_PATH",
)


def value(name, default=""):
    return os.environ.get("TRACKMANIABENCH_" + name, default)


def require_remote():
    for name in ("SSH_HOST", "EXPECTED_HOSTNAME", "GAME_DOCUMENTS", "REMOTE_DATA"):
        if not value(name):
            raise ValueError("Configure TRACKMANIABENCH_" + name + " before accessing the render machine")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", value("SSH_HOST")) or value("SSH_HOST").startswith("-"):
        raise ValueError("SSH_HOST must be an SSH configuration alias or user@hostname")
    for name in ("GAME_DOCUMENTS", "REMOTE_DATA"):
        if not Path(value(name)).is_absolute():
            raise ValueError(name + " must be an absolute path on the render machine")


def remote_prelude():
    require_remote()
    values = {"TRACKMANIABENCH_" + k: value(k) for k in KEYS if value(k)}
    return "import os\nos.environ.update(" + repr(values) + ")\n"


SSH_OPTIONS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=8",
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=3",
]
