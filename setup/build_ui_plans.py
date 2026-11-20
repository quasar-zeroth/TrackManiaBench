#!/usr/bin/env python3
"""Materialize native UI plan blueprints with the operator's local screenshots.

The supplied coordinates describe a 1920x1080 X11 desktop. This command makes
no claim that a capture or click was reviewed and never accesses the desktop.
"""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import native_ui_worker as worker


def build(blueprint, captures, output):
    plans = json.loads(blueprint.read_text())
    if output.exists():
        raise ValueError("Plan output directory already exists")
    for name, doc in plans.items():
        worker.validate_plan(doc)
        for state in doc["states"].values():
            for region in state["regions"]:
                image = captures / Path(region["image"]).name
                if not image.is_file():
                    raise ValueError("Capture this UI state first: " + str(image))
    output.mkdir(parents=True)
    (output / "references").mkdir()
    files = {}
    paths = {}
    for name, original in plans.items():
        if Path(name).name != name:
            raise ValueError("Unsafe plan filename")
        doc = copy.deepcopy(original)
        for state in doc["states"].values():
            for region in state["regions"]:
                image = captures / Path(region["image"]).name
                digest = hashlib.sha256(image.read_bytes()).hexdigest()
                target = output / "references" / (digest + ".png")
                if not target.exists():
                    shutil.copyfile(image, target)
                region["image"] = "references/" + target.name
                files[image.name] = digest
        path = output / name
        path.write_text(json.dumps(doc, indent=2) + "\n")
        worker.load_plan(path)
        paths[Path(name).stem] = name
    (output / "plans.json").write_text(json.dumps({"plans": paths}, indent=2) + "\n")
    (output / "captures.sha256.json").write_text(json.dumps(files, indent=2) + "\n")
    print(json.dumps({"plans": len(plans), "captures": len(files), "gui_inputs": 0, "review_required": True}))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--blueprint", type=Path, default=Path(__file__).with_name("ui_plan_blueprint.json"))
    p.add_argument("--captures", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    build(a.blueprint, a.captures, a.output)
