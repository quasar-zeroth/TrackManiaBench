"""Optional local input devices. Constructors never alter device permissions."""

from __future__ import annotations

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
    """

    action_profile = "xbox:steer-int16,triggers-uint8"

    def __init__(self, *, gamepad=None, sleep=time.sleep):
        self._sleep = sleep
        self._closed = False
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
        self.release()

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
        """Pulse the default give-up binding B; the backend verifies the result."""
        if self._closed:
            raise ControlError("Gamepad is closed")
        self.release()
        try:
            self._pad.press_button(button=0x2000)
            self._pad.update()
            self._sleep(0.1)
            self._pad.release_button(button=0x2000)
            self._pad.update()
        except Exception as exc:
            self._release_best_effort()
            raise ControlError("Gamepad reset failed; do not retry an uncertain reset") from exc
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
    _keys = ("Up", "Down", "Left", "Right", "BackSpace")

    def __init__(
        self,
        window_id: int | str,
        *,
        class_pattern="Trackmania",
        display=None,
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
        if self._closed:
            raise ControlError("Keyboard controller is closed")
        self.release()
        try:
            self._guard()
            self._run("keydown", "BackSpace")
            self._sleep(0.1)
            self._run("keyup", "BackSpace")
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
