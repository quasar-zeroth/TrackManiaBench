#!/usr/bin/env python3
"""Guarded game-window primitives; no built-in workflow or credentials.

Host commands:
  native_ui_worker.py capture --output DIR
  native_ui_worker.py run plan.json --output DIR [--execute]
  native_ui_worker.py validate plan.json
  native_ui_worker.py self-test

`run` without --execute only verifies the first precondition. It never clicks.
Each executed action needs before/after visual guards from reviewed screenshots.
Screenshots are of the verified Trackmania window only, never the desktop.
"""

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import zlib

SIZE = (1920, 1080)
GUARD_SAMPLE_GAP_SECONDS = 0.10
GUARD_REUSE_MAX_AGE_SECONDS = 1.0
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes"]
PRIVATE_XDOTOOL = os.environ.get("TRACKMANIABENCH_XDOTOOL", "xdotool")
PRIVATE_LIBS = os.environ.get("TRACKMANIABENCH_LIBRARY_PATH", "")
ALLOWED_KEYS = {
    "Return",
    "Escape",
    "Tab",
    "shift+Tab",
    "BackSpace",
    "Delete",
    "Home",
    "End",
    "Left",
    "Right",
    "Up",
    "Down",
    "ctrl+a",
    "F3",
    "F11",
    "space",
}


def need(condition, message):
    if not condition:
        raise ValueError(message)


def validate_plan(plan):
    need(isinstance(plan, dict) and plan.get("version") == 1, "Plan version must be 1")
    need(plan.get("size", [1920, 1080]) == [1920, 1080], "Only reviewed 1920x1080 coordinates supported")
    states = plan.get("states", {})
    references = plan.get("reference_images", {})
    need(
        isinstance(references, dict) and len(references) <= 512,
        "At most 512 shared reference images supported",
    )
    need(isinstance(states, dict) and 1 <= len(states) <= 32, "states must contain 1..32 named states")
    for name, state in states.items():
        need(re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", name), "Unsafe state name")
        need(isinstance(state, dict), "Invalid state")
        regions = state.get("regions", [])
        need(isinstance(regions, list) and 1 <= len(regions) <= 16, "Each state needs 1..16 visual regions")
        for region in regions:
            need(isinstance(region, dict), "Invalid region")
            need(
                bool(region.get("image") or region.get("png_b64") or region.get("png_ref")),
                "Region requires reference image",
            )
            if "png_ref" in region:
                need(region["png_ref"] in references, "Shared reference image is missing")
            need(type(region.get("reference_cropped", False)) is bool, "reference_cropped must be boolean")
            need(region.get("match_mode", "pixels") in {"pixels", "white_text"}, "Unknown visual match mode")
            need(
                0 <= region.get("max_text_mask_error", 0.06) <= 0.10,
                "Text-mask error threshold too permissive",
            )
            need(
                type(region.get("min_text_foreground_pixels", 50)) is int
                and 20 <= region.get("min_text_foreground_pixels", 50) <= 200,
                "Text foreground minimum must be 20..200 pixels",
            )
            need(
                type(region.get("max_text_shift_x", 0)) is int
                and 0 <= region.get("max_text_shift_x", 0) <= 12,
                "Horizontal text shift must be 0..12 pixels",
            )
            roi = region.get("rect")
            need(
                isinstance(roi, list) and len(roi) == 4 and all(type(v) is int for v in roi),
                "rect must be [x,y,width,height]",
            )
            x, y, w, h = roi
            need(
                x >= 0 and y >= 0 and w >= 16 and h >= 16 and x + w <= SIZE[0] and y + h <= SIZE[1],
                "Invalid reference rectangle",
            )
            need(0 <= region.get("max_mean_error", 6.0) <= 20, "Mean-error threshold too permissive")
            need(
                0 <= region.get("max_changed_fraction", 0.15) <= 0.35,
                "Changed-pixel threshold too permissive",
            )
            need(5 <= region.get("pixel_tolerance", 24) <= 40, "Pixel tolerance out of range")
    steps = plan.get("steps", [])
    need(
        isinstance(steps, list) and 1 <= len(steps) <= 100,
        "Plan must contain 1..100 steps; supervisor queues runs",
    )
    for i, step in enumerate(steps):
        need(isinstance(step, dict), "Invalid step")
        need(
            step.get("before") in states and step.get("after") in states,
            f"Step {i} requires named before and after states",
        )
        need(1 <= step.get("timeout_seconds", 30) <= 120, "Each wait must be bounded at 1..120 seconds")
        action = step.get("action", {})
        kind = action.get("kind")
        need(kind in {"click", "move", "key", "type", "scroll", "observe"}, "Unsupported action")
        if kind in {"click", "move"}:
            need(
                type(action.get("x")) is int and type(action.get("y")) is int,
                "Integer mouse coordinates required",
            )
            need(0 <= action["x"] < SIZE[0] and 0 <= action["y"] < SIZE[1], "Mouse outside game window")
            need(action.get("button", 1) in [1, 3], "Only left/right mouse buttons allowed")
        if kind == "key":
            need(action.get("key") in ALLOWED_KEYS, "Key not allowed; no global shortcuts")
        if kind == "type":
            need(
                action.get("purpose") in {"filename", "path", "batch", "numeric"},
                "Text requires explicit non-secret purpose",
            )
            text = action.get("text")
            need(isinstance(text, str) and 0 < len(text) <= 32768 and "\x00" not in text, "Invalid text")
            need(
                not re.search(r"password|authorization|bearer|refresh_token|access_token", text, re.I),
                "Credential-like text prohibited",
            )
            need(
                action["purpose"] == "batch" or not re.search(r"[\r\n]", text),
                "Only batch text can contain newlines",
            )
        if kind == "scroll":
            need(
                type(action.get("steps")) is int and 1 <= abs(action["steps"]) <= 12,
                "Scroll must be 1..12 ticks",
            )
    return plan


class VisualMatcher:
    def __init__(self, states, reference_images=None):
        from PIL import Image
        import numpy as np

        self.np = np
        self.states = {}
        references = reference_images or {}
        decoded = {}
        for name, state in states.items():
            parts = []
            for r in state["regions"]:
                encoded = references[r["png_ref"]] if "png_ref" in r else r["png_b64"]
                key = r.get("png_ref") or hashlib.sha256(encoded.encode()).hexdigest()
                if key not in decoded:
                    decoded[key] = Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))).convert(
                        "RGB"
                    )
                im = decoded[key]
                x, y, w, h = r["rect"]
                if r.get("reference_cropped"):
                    need(im.size == (w, h), "Cropped reference dimensions must equal rectangle")
                    ref = np.asarray(im, dtype=np.int16)
                else:
                    need(im.size == SIZE, "Reference screenshot must be exactly 1920x1080")
                    ref = np.asarray(im.crop((x, y, x + w, y + h)), dtype=np.int16)
                # A flat region can match unrelated menus. Require texture/text.
                need(float(ref.std(axis=(0, 1)).mean()) >= 8, "Reference region lacks distinguishing detail")
                if r.get("match_mode") == "white_text":
                    mask = self.white_text_mask(ref)
                    need(
                        r.get("min_text_foreground_pixels", 50) <= int(mask.sum()) <= mask.size * 0.45,
                        "Text mask lacks enough distinctive foreground",
                    )
                parts.append((r, ref))
            self.states[name] = parts

    @staticmethod
    def white_text_mask(rgb):
        # White glyph cores survive translucent scenery changes. Both missing
        # and extra foreground pixels count; the entire crop is never ignored.
        return (rgb.min(axis=2) >= 180) & ((rgb.max(axis=2) - rgb.min(axis=2)) <= 45)

    def match(self, image, name):
        np = self.np
        results = []
        for r, ref in self.states[name]:
            x, y, w, h = r["rect"]
            live = np.asarray(image.crop((x, y, x + w, y + h)), dtype=np.int16)
            if r.get("match_mode") == "white_text":
                expected_mask = self.white_text_mask(ref)
                shift = r.get("max_text_shift_x", 0)
                candidates = []
                for dx in range(-shift, shift + 1):
                    if x + dx < 0 or x + w + dx > SIZE[0]:
                        continue
                    candidate = (
                        live
                        if dx == 0
                        else np.asarray(image.crop((x + dx, y, x + w + dx, y + h)), dtype=np.int16)
                    )
                    mask = self.white_text_mask(candidate)
                    union = int((expected_mask | mask).sum())
                    candidates.append(
                        (float((expected_mask ^ mask).sum() / max(1, union)), dx, int(mask.sum()))
                    )
                error, best_dx, foreground = min(candidates)
                results.append(
                    {
                        "rect": r["rect"],
                        "mode": "white_text",
                        "text_mask_error": round(error, 4),
                        "matched_shift_x": best_dx,
                        "reference_foreground_pixels": int(expected_mask.sum()),
                        "observed_foreground_pixels": foreground,
                        "match": error <= r.get("max_text_mask_error", 0.06),
                    }
                )
                continue
            delta = np.abs(live - ref)
            mean = float(delta.mean())
            changed = float((delta.max(axis=2) > r.get("pixel_tolerance", 24)).mean())
            ok = mean <= r.get("max_mean_error", 6.0) and changed <= r.get("max_changed_fraction", 0.15)
            results.append(
                {
                    "rect": r["rect"],
                    "mean_error": round(mean, 3),
                    "changed_fraction": round(changed, 4),
                    "match": ok,
                }
            )
        return all(r["match"] for r in results), results


class Desktop:
    def __init__(self, expected_window=None):
        need(
            os.environ.get("TRACKMANIABENCH_EXPECTED_HOSTNAME") == os.uname().nodename,
            "Remote hostname differs from configured expected hostname",
        )
        self.env = os.environ.copy()
        self.env["DISPLAY"] = os.environ.get("TRACKMANIABENCH_DISPLAY", ":0")
        if os.environ.get("TRACKMANIABENCH_XAUTHORITY"):
            self.env["XAUTHORITY"] = os.environ["TRACKMANIABENCH_XAUTHORITY"]
        if PRIVATE_LIBS:
            self.env["LD_LIBRARY_PATH"] = PRIVATE_LIBS + (
                ":" + self.env["LD_LIBRARY_PATH"] if self.env.get("LD_LIBRARY_PATH") else ""
            )
        self.window = expected_window
        self.pid = None
        self.process_start = None
        self.identity()

    def command(self, argv, input_bytes=None, timeout=20):
        result = subprocess.run(
            argv,
            input=input_bytes,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        need(result.returncode == 0, f"Desktop command failed: {Path(argv[0]).name}")
        return result.stdout

    def xdo(self, *args, input_bytes=None, timeout=20):
        return self.command([PRIVATE_XDOTOOL, *map(str, args)], input_bytes, timeout).decode().strip()

    def identity(self):
        active = int(self.xdo("getactivewindow"))
        if self.window is None:
            self.window = active
        need(active == int(self.window), "Trackmania lost focus; refusing input or capture")
        wm_class = self.command(["xprop", "-id", str(self.window), "WM_CLASS"]).decode()
        classes = re.findall(r'"([^"]+)"', wm_class)
        need(
            classes and all(c == "steam_app_2225070" for c in classes),
            "Active window is not Trackmania Steam app 2225070",
        )
        raw_pid = self.command(["xprop", "-id", str(self.window), "_NET_WM_PID"]).decode()
        match = re.search(r"=\s*(\d+)", raw_pid)
        need(match is not None, "Game window has no process identity")
        pid = int(match.group(1))
        process = Path("/proc") / str(pid)
        # Wine/game rename comm to MainThread; inspect only argv[0], never auth args.
        argv0 = (process / "cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
        need(
            argv0.replace("\\", "/").rsplit("/", 1)[-1].lower() == "trackmania.exe",
            "Window PID is not Trackmania.exe",
        )
        exe = os.readlink(process / "exe")
        need(
            Path(exe).name in {"wine64-preloader", "wine-preloader", "wine64", "wine"},
            "Game PID has unexpected or deleted Wine executable",
        )
        start = (process / "stat").read_text().rpartition(")")[2].split()[19]
        if self.pid is None:
            self.pid = pid
            self.process_start = start
        need(pid == self.pid and start == self.process_start, "Game process changed during sequence")
        geometry = dict(
            line.split("=", 1)
            for line in self.xdo("getwindowgeometry", "--shell", self.window).splitlines()
            if "=" in line
        )
        need((int(geometry["WIDTH"]), int(geometry["HEIGHT"])) == SIZE, "Game dimensions changed")
        need(
            (int(geometry["X"]), int(geometry["Y"])) == (0, 0),
            "Game origin changed; reviewed desktop coordinates no longer match",
        )
        return {
            "window": self.window,
            "pid": self.pid,
            "process_start": self.process_start,
            "size": list(SIZE),
            "origin": [0, 0],
        }

    def capture(self):
        from PIL import Image

        self.identity()
        png = self.command(
            ["import", "-window", str(self.window), "-define", "png:compression-level=1", "png:-"]
        )
        self.identity()
        image = Image.open(io.BytesIO(png)).convert("RGB")
        need(image.size == SIZE, "Captured game-window dimensions changed")
        return png, image

    def act(self, action):
        self.identity()
        kind = action["kind"]
        if kind in {"click", "move"}:
            self.xdo("mousemove", "--sync", action["x"], action["y"])
            if kind == "click":
                self.identity()
                button = action.get("button", 1)
                self.xdo("mousedown", button)
                try:
                    # xdotool's short default click can fall between game frames.
                    # Hold once, never retry a checkbox/toggle automatically.
                    time.sleep(0.10)
                finally:
                    # Always release our pressed button, even if interrupted.
                    self.xdo("mouseup", button)
                self.identity()
        elif kind == "key":
            self.xdo("key", "--clearmodifiers", action["key"])
        elif kind == "type":
            self.xdo(
                "type",
                "--clearmodifiers",
                "--delay",
                "1",
                "--file",
                "-",
                input_bytes=action["text"].encode(),
                timeout=60,
            )
        elif kind == "scroll":
            n = action["steps"]
            self.xdo("click", "--repeat", abs(n), "--delay", "80", "5" if n > 0 else "4")
        # observe intentionally produces no input.


def emit(event, **values):
    print(json.dumps({"time_unix": time.time(), "event": event, **values}), flush=True)


def screenshot(label, png, **values):
    emit("screenshot", label=label, png_b64=base64.b64encode(png).decode(), **values)


def wait_for(desktop, matcher, name, timeout, label):
    deadline = time.monotonic() + timeout
    matched = 0
    last_progress = 0
    while True:
        png, frame = desktop.capture()
        observed_at = time.monotonic()
        ok, metrics = matcher.match(frame, name)
        matched = matched + 1 if ok else 0
        if matched >= 2:
            screenshot(label, png, state=name, metrics=metrics)
            return {"state": name, "observed_at": observed_at}
        if time.monotonic() >= deadline:
            screenshot(label + "-mismatch", png, state=name, metrics=metrics)
            raise ValueError(f"Timed out waiting for reviewed state {name}")
        if time.monotonic() - last_progress >= 5:
            emit("waiting", state=name, metrics=metrics)
            last_progress = time.monotonic()
        time.sleep(GUARD_SAMPLE_GAP_SECONDS)


def recent_guard_age(observation, expected_state, now=None):
    if observation is None or observation.get("state") != expected_state:
        return None
    age = (time.monotonic() if now is None else now) - observation["observed_at"]
    return age if 0 <= age <= GUARD_REUSE_MAX_AGE_SECONDS else None


def image_self_test():
    from PIL import Image, ImageDraw

    im = Image.new("RGB", SIZE, "black")
    draw = ImageDraw.Draw(im)
    for x in range(50, 250, 10):
        draw.rectangle((x, 50, x + 4, 140), fill="white")
    b = io.BytesIO()
    im.save(b, format="PNG")
    states = {
        "test": {
            "regions": [{"png_b64": base64.b64encode(b.getvalue()).decode(), "rect": [50, 50, 200, 100]}]
        }
    }
    matcher = VisualMatcher(states)
    need(matcher.match(im, "test")[0], "Identical image did not match")
    need(not matcher.match(Image.new("RGB", SIZE, "black"), "test")[0], "Wrong image matched")
    draw.rectangle((50, 50, 250, 150), fill="black")
    need(not matcher.match(im, "test")[0], "Removed control matched")
    bad = {
        "version": 1,
        "states": states,
        "steps": [{"before": "test", "after": "test", "action": {"kind": "key", "key": "alt+F4"}}],
    }
    try:
        validate_plan(bad)
    except ValueError:
        pass
    else:
        raise ValueError("Global shortcut was not rejected")
    bad["steps"][0]["action"] = {"kind": "click", "x": 1920, "y": 0}
    try:
        validate_plan(bad)
    except ValueError:
        pass
    else:
        raise ValueError("Out-of-window click was not rejected")
    bad["steps"][0]["action"] = {"kind": "type", "purpose": "path", "text": "access_token=example"}
    try:
        validate_plan(bad)
    except ValueError:
        pass
    else:
        raise ValueError("Credential-like input was not rejected")
    emit("self_test_complete", checks=6)


def remote_main(payload):
    if payload["command"] == "self-test":
        image_self_test()
        return
    desktop = Desktop(payload.get("window"))
    emit("identity_verified", **desktop.identity())
    if payload["command"] == "capture":
        screenshot("capture", desktop.capture()[0])
        return
    plan = validate_plan(payload["plan"])
    matcher = VisualMatcher(plan["states"], plan.get("reference_images"))
    if not payload.get("execute"):
        step = plan["steps"][0]
        wait_for(desktop, matcher, step["before"], step.get("timeout_seconds", 30), "dry-run-before")
        emit("dry_run_complete", inputs_sent=0, steps_validated=len(plan["steps"]))
        return
    prior_after = None
    for index, step in enumerate(plan["steps"]):
        prefix = f"{index:03d}"
        timeout = step.get("timeout_seconds", 30)
        age = recent_guard_age(prior_after, step["before"])
        if age is None:
            wait_for(desktop, matcher, step["before"], timeout, prefix + "-before")
        else:
            emit(
                "guard_reused",
                step=index,
                state=step["before"],
                prior_step=index - 1,
                observation_age_seconds=round(age, 4),
            )
        prior_after = None
        # Check again immediately after state observation, before any input.
        desktop.identity()
        emit("action_started", step=index, kind=step["action"]["kind"])
        desktop.act(step["action"])
        time.sleep(0.25)
        prior_after = wait_for(desktop, matcher, step["after"], timeout, prefix + "-after")
        emit("step_complete", step=index)
    emit("sequence_complete", steps=len(plan["steps"]))


def load_plan(path):
    plan = validate_plan(json.loads(path.read_text()))
    references = dict(plan.get("reference_images", {}))
    files = {}
    decoded = {}
    try:
        from PIL import Image
    except ImportError:
        Image = None  # Standard-library-only hosts still get deduplication.
    for state in plan["states"].values():
        for region in state["regions"]:
            if "png_ref" in region:
                continue
            if "image" in region:
                # Full-size evidence stays unchanged; transport embeds each file once.
                p = (path.parent / region["image"]).resolve()
                if p not in files:
                    files[p] = p.read_bytes()
                data = files[p]
            else:
                data = base64.b64decode(region["png_b64"], validate=True)
            need(
                data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) <= 16 * 1024 * 1024,
                "Reference must be a PNG under 16MiB",
            )
            if Image is not None and not region.get("reference_cropped"):
                full_key = hashlib.sha256(data).hexdigest()
                if full_key not in decoded:
                    decoded[full_key] = Image.open(io.BytesIO(data)).convert("RGB")
                need(decoded[full_key].size == SIZE, "Reference screenshot must be exactly 1920x1080")
                x, y, w, h = region["rect"]
                cropped = io.BytesIO()
                decoded[full_key].crop((x, y, x + w, y + h)).save(cropped, format="PNG")
                data = cropped.getvalue()
                region["reference_cropped"] = True
            key = hashlib.sha256(data).hexdigest()
            references.setdefault(key, base64.b64encode(data).decode())
            region["png_ref"] = key
            region.pop("image", None)
            region.pop("png_b64", None)
    plan["reference_images"] = references
    return validate_plan(plan)


def encode_payload(payload):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    need(len(raw) <= 96 * 1024 * 1024, "Plan payload exceeds 96MiB; use fewer states/steps")
    return base64.b64encode(zlib.compress(raw, 6)).decode(), len(raw)


def decode_payload(encoded):
    return json.loads(zlib.decompress(base64.b64decode(encoded, validate=True)))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["capture", "run", "validate", "self-test"]:
        p = sub.add_parser(name)
        if name in {"run", "validate"}:
            p.add_argument("plan", type=Path)
        if name in {"capture", "run"}:
            p.add_argument("--output", type=Path, required=True, help="New local evidence directory")
            p.add_argument("--window", type=int, help="Pin an already-verified game window ID")
        if name == "run":
            p.add_argument(
                "--execute",
                action="store_true",
                help="Execute guarded input; omitted means read-only preflight",
            )
    args = parser.parse_args()
    if args.command == "self-test":
        image_self_test()
        return
    payload = {"command": args.command, "window": getattr(args, "window", None)}
    if args.command in {"run", "validate"}:
        payload["plan"] = load_plan(args.plan)
    if args.command == "validate":
        print(
            json.dumps(
                {
                    "valid": True,
                    "steps": len(payload["plan"]["steps"]),
                    "note": "Schema/files only; image distinctiveness and current state require remote dry run",
                }
            )
        )
        return
    payload["execute"] = getattr(args, "execute", False)
    out = getattr(args, "output", None)
    if out:
        out.mkdir(parents=True, exist_ok=False)
        os.chmod(out, 0o700)
    # Send code+payload over stdin; no text fields or images in process arguments.
    from runtime import remote_prelude, value

    source = remote_prelude() + Path(__file__).read_text()
    encoded, payload_bytes = encode_payload(payload)
    remote_source = (
        source
        + "\ntry:\n    remote_main(decode_payload("
        + repr(encoded)
        + "))\nexcept Exception as error:\n    emit('stopped', reason=str(error), exception_type=type(error).__name__)\n    sys.exit(1)\n"
    )
    print(
        json.dumps(
            {
                "event": "transport_start",
                "payload_bytes": payload_bytes,
                "wire_bytes": len(remote_source.encode()),
                "reference_images": len(payload.get("plan", {}).get("reference_images", {})),
                "cropped_regions": sum(
                    bool(r.get("reference_cropped"))
                    for s in payload.get("plan", {}).get("states", {}).values()
                    for r in s["regions"]
                ),
            }
        ),
        flush=True,
    )
    command = ["ssh", "-T", *SSH_OPTIONS, value("SSH_HOST"), "python3 - --remote"]
    with subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    ) as proc:
        proc.stdin.write(remote_source)
        proc.stdin.close()
        sequence = 0
        with (out / "events.jsonl").open("w") if out else io.StringIO() as log:
            for line in proc.stdout:
                event = json.loads(line)
                if "png_b64" in event:
                    need(out is not None, "Unexpected screenshot without output directory")
                    label = event.pop("label")
                    need(re.fullmatch(r"[a-zA-Z0-9_-]+", label), "Unsafe screenshot label")
                    filename = f"{sequence:04d}-{label}.png"
                    (out / filename).write_bytes(base64.b64decode(event.pop("png_b64"), validate=True))
                    event["screenshot"] = filename
                sequence += 1
                event["sequence"] = sequence
                log.write(json.dumps(event) + "\n")
                log.flush()
                print(json.dumps(event), flush=True)
            error_output = proc.stderr.read()
            rc = proc.wait()
            if rc:
                # Remote exceptions do not include UI text/payloads.
                if error_output:
                    print("Remote transport error: " + error_output[-2000:], file=sys.stderr)
                raise SystemExit(rc)


if __name__ == "__main__" and sys.argv[1:2] != ["--remote"]:
    main()
