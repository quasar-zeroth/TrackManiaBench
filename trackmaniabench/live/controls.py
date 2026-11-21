"""Optional local input devices. Constructors never alter device permissions."""

from __future__ import annotations

import math
import os
import re
import subprocess
import time

from .types import Action


class ControlError(RuntimeError):
    """A control operation failed; its partial effect must not be retried."""


class GamepadController:
    """An Xbox-compatible virtual pad; the game must bind its usual controls.

    Linux requires permission to open /dev/uinput. Return values describe the
    actual quantized device report, not proof the game has consumed that report.
    A newly created pad waits for game discovery before the first report.
    Injected pads are assumed to be connected already and do not wait.
    """

    action_profile = "xbox:steer-int16,triggers-uint8"

    def __init__(self, *, reset_button=0x2000, connect_settle_seconds=2.0, gamepad=None, sleep=time.sleep):
        buttons = {1, 2, 4, 8, 0x10, 0x20, 0x40, 0x80, 0x100, 0x200, 0x1000, 0x2000, 0x4000, 0x8000}
        if isinstance(reset_button, bool) or not isinstance(reset_button, int) or reset_button not in buttons:
            raise ValueError("reset_button must be one Xbox button mask, such as B=0x2000")
        if not math.isfinite(connect_settle_seconds) or connect_settle_seconds < 0:
            raise ValueError("connect_settle_seconds must be finite and non-negative")
        self._reset_button = reset_button
        self.reset_binding = f"xbox-button:0x{reset_button:04x}"
        self._sleep = sleep
        self._closed = False
        created = gamepad is None
        if gamepad is None:
            try:
                import vgamepad

                gamepad = vgamepad.VX360Gamepad()
            except (ImportError, OSError, RuntimeError) as exc:
                raise ControlError(
                    "Cannot create a virtual gamepad. Install TrackManiaBench[live] "
                    "and grant your desktop user access to /dev/uinput on Linux. "
                    "No permissions were changed automatically."
                ) from exc
        self._pad = gamepad
        try:
            if created and connect_settle_seconds:
                self._sleep(connect_settle_seconds)
            self.release()
        except BaseException:
            self._release_best_effort()
            self._closed = True
            raise

    def apply(self, action: Action) -> Action:
        if self._closed:
            raise ControlError("Gamepad is closed")
        steer = int(round(action.steer * 32767))
        throttle = int(round(action.throttle * 255))
        brake = int(round(action.brake * 255))
        try:
            self._pad.left_joystick(x_value=steer, y_value=0)
            self._pad.right_trigger(value=throttle)
            self._pad.left_trigger(value=brake)
            self._pad.update()
        except Exception as exc:
            self._release_best_effort()
            raise ControlError("Gamepad report failed; controls were released where possible") from exc
        return Action(steer / 32767, throttle / 255, brake / 255)

    def release(self):
        self._pad.reset()
        self._pad.update()

    def _release_best_effort(self):
        try:
            self.release()
        except Exception:
            pass

    def reset(self):
        """Pulse the configured Give Up button (B by default).

        Confirm its binding in the game's controls settings. A checkpoint
        respawn can match the starting pose without restarting the race clock.
        """
        self._pulse_button(self._reset_button, "reset")

    def dismiss_finish_popup(self):
        """Confirm the finish popup with A, only after an observed finish."""
        self._pulse_button(0x1000, "finish-popup confirmation")

    def _pulse_button(self, button, operation):
        if self._closed:
            raise ControlError("Gamepad is closed")
        self.release()
        try:
            self._pad.press_button(button=button)
            self._pad.update()
            self._sleep(0.1)
            self._pad.release_button(button=button)
            self._pad.update()
        except Exception as exc:
            self._release_best_effort()
            raise ControlError(f"Gamepad {operation} failed; do not retry uncertain input") from exc
        finally:
            self._release_best_effort()

    def close(self):
        if not self._closed:
            try:
                self.release()
            finally:
                self._closed = True


class KeyboardController:
    """Digital arrow controls for a specific, focused Trackmania X11 window.

    This uses XTest through xdotool, not XSendEvent's --window delivery (which
    games can ignore). Focus and visible window class are checked before input;
    releases are unconditional so losing focus cannot leave a held key behind.
    """

    action_profile = "keyboard:steer{-1,0,1},pedals{0,1}"
    _drive_keys = ("Up", "Down", "Left", "Right")

    def __init__(
        self,
        window_id: int | str,
        *,
        class_pattern="Trackmania",
        display=None,
        reset_key="Delete",
        steer_threshold=0.5,
        pedal_threshold=0.5,
        command_timeout=2.0,
        runner=subprocess.run,
        sleep=time.sleep,
        xdotool="xdotool",
    ):
        if isinstance(window_id, bool):
            raise ValueError("window_id must be a positive X11 window ID")
        text = str(window_id)
        if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", text):
            raise ValueError("window_id must be a numeric X11 window ID")
        self.window_id = int(text, 16 if text.lower().startswith("0x") else 10)
        if self.window_id <= 0:
            raise ValueError("window_id must be positive")
        if not 0 < steer_threshold <= 1 or not 0 < pedal_threshold <= 1:
            raise ValueError("Control thresholds must be in (0, 1]")
        if not class_pattern or command_timeout <= 0:
            raise ValueError("A window class guard and positive command timeout are required")
        if not isinstance(reset_key, str) or re.fullmatch(r"[A-Za-z0-9_]+", reset_key) is None:
            raise ValueError("reset_key must be a single X11 keysym, such as Delete")
        self._reset_key = reset_key
        self._keys = tuple(dict.fromkeys((*self._drive_keys, reset_key)))
        self.reset_binding = f"keyboard:{reset_key}"
        self._class_pattern = class_pattern
        self._steer_threshold = steer_threshold
        self._pedal_threshold = pedal_threshold
        self._timeout = command_timeout
        self._runner = runner
        self._sleep = sleep
        self._xdotool = xdotool
        self._env = os.environ.copy()
        if display is not None:
            self._env["DISPLAY"] = str(display)
        self._closed = False
        self._guard()

    def _run(self, *args):
        try:
            return self._runner(
                [self._xdotool, *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=self._env,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise ControlError(
                "xdotool failed; verify X11 DISPLAY, window focus and xdotool installation"
            ) from exc

    def _guard(self):
        ids = self._run("search", "--onlyvisible", "--class", self._class_pattern).split()
        if str(self.window_id) not in ids:
            raise ControlError(
                "Configured window is not a visible window matching the Trackmania class guard"
            )
        if self._run("getwindowfocus") != str(self.window_id):
            raise ControlError("Trackmania must be the focused X11 window before sending controls")

    def apply(self, action: Action) -> Action:
        if self._closed:
            raise ControlError("Keyboard controller is closed")
        steer = (
            1.0
            if action.steer >= self._steer_threshold
            else (-1.0 if action.steer <= -self._steer_threshold else 0.0)
        )
        throttle = float(action.throttle >= self._pedal_threshold)
        brake = float(action.brake >= self._pedal_threshold)
        desired = {"Up": bool(throttle), "Down": bool(brake), "Left": steer < 0, "Right": steer > 0}
        try:
            self._guard()
            # Release old directions before pressing new ones. Each command's
            # completion is checked; a failure neutralizes and never retries.
            for key, down in desired.items():
                if not down:
                    self._run("keyup", key)
            for key, down in desired.items():
                if down:
                    self._run("keydown", key)
            self._guard()
        except Exception as exc:
            self._release_best_effort()
            raise ControlError(
                "Keyboard action failed or focus changed; controls released where possible"
            ) from exc
        return Action(steer, throttle, brake)

    def release(self):
        errors = []
        for key in self._keys:
            try:
                self._run("keyup", key)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ControlError("Could not release all keyboard controls") from errors[0]

    def _release_best_effort(self):
        try:
            self.release()
        except Exception:
            pass

    def reset(self):
        """Pulse Give Up (Delete by default), not checkpoint respawn."""
        if self._closed:
            raise ControlError("Keyboard controller is closed")
        self.release()
        try:
            self._guard()
            self._run("keydown", self._reset_key)
            self._sleep(0.1)
            self._run("keyup", self._reset_key)
        except Exception as exc:
            raise ControlError("Keyboard reset failed; do not retry an uncertain reset") from exc
        finally:
            self._release_best_effort()

    def close(self):
        if not self._closed:
            try:
                self.release()
            finally:
                self._closed = True
