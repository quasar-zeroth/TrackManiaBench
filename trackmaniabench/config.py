"""Explicit machine configuration; no production paths or credentials."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tomllib

from collector.common import safe_cohort, safe_uid

REPOSITORY = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Config:
    source: Path
    cohort: str
    map_uid: str
    count: int
    workspace: Path
    user_agent: str
    settings: dict

    def path(self, name, default=None):
        value = self.settings.get("paths", {}).get(name, default)
        if not value:
            return None
        path = Path(os.path.expandvars(value)).expanduser()
        return path if path.is_absolute() else (self.source.parent / path).resolve()

    def environment(self):
        env = os.environ.copy()
        env.update(TRACKMANIABENCH_ROOT=str(REPOSITORY), TRACKMANIABENCH_WORKSPACE=str(self.workspace))
        mapping = {
            "ssh_host": "SSH_HOST",
            "expected_hostname": "EXPECTED_HOSTNAME",
            "data_root": "REMOTE_DATA",
            "game_documents": "GAME_DOCUMENTS",
            "display": "DISPLAY",
            "xauthority": "XAUTHORITY",
            "xdotool": "XDOTOOL",
            "library_path": "LIBRARY_PATH",
        }
        for key, suffix in mapping.items():
            value = self.settings.get("remote", {}).get(key)
            if value:
                env["TRACKMANIABENCH_" + suffix] = str(value)
        coverage = self.path("coverage_policy")
        if coverage:
            env["TRACKMANIABENCH_COVERAGE_POLICY"] = str(coverage)
        return env


def load(path):
    path = Path(path).expanduser().resolve(strict=True)
    with path.open("rb") as stream:
        value = tomllib.load(stream)
    if value.get("schema_version") != 1:
        raise ValueError("Unsupported configuration schema_version")
    cohort = safe_cohort(value["dataset"]["cohort"])
    uid = safe_uid(value["track"]["uid"])
    count = value["dataset"]["count"]
    if type(count) is not int or not 1 <= count <= 250:
        raise ValueError("This verified renderer supports 1..250 records per cohort")
    render = value.get("render", {})
    if render.get("profile", "native-chase-360p50") != "native-chase-360p50":
        raise ValueError(
            "Only native-chase-360p50 is validated; create and validate a new profile before changing it"
        )
    workspace = Path(os.path.expandvars(value["paths"]["workspace"])).expanduser()
    if not workspace.is_absolute():
        workspace = (path.parent / workspace).resolve()
    if workspace == REPOSITORY or workspace == REPOSITORY.parent:
        raise ValueError("Use a dedicated data workspace, not the repository root")
    return Config(
        path,
        cohort,
        uid,
        count,
        workspace,
        value.get("collection", {}).get("user_agent", "TrackManiaBench/0.1 (research replay dataset)"),
        value,
    )
