"""Cross-platform global hotkey support for Aura.

macOS: Uses a listen-only Quartz event tap.
Windows: Uses the native RegisterHotKey API.
Other platforms: Gracefully disable global hotkeys.
"""

from __future__ import annotations

import sys
import threading

from PySide6.QtCore import QObject, Signal


# macOS virtual keycodes
_MAC_KEYCODES = {
    "space": 49, "return": 36, "enter": 36, "tab": 48,
    "escape": 53, "esc": 53,
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5,
    "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
    "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37,
    "j": 38, "k": 40, "n": 45, "m": 46,
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23,
    "6": 22, "7": 26, "8": 28, "9": 25, "0": 29,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118,
    "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}

# Windows virtual keycodes
_WIN_KEYCODES = {
    "space": 0x20, "return": 0x0D, "enter": 0x0D,
    "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
    **{chr(c): c for c in range(ord("a"), ord("z") + 1)},
    **{str(c): ord(str(c)) for c in range(10)},
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}

_MODIFIER_ALIASES = {
    "fn": "fn",
    "function": "fn",
    "cmd": "cmd",
    "command": "cmd",
    "meta": "cmd",
    "super": "cmd",
    "win": "win",
    "windows": "win",
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "opt": "alt",
    "option": "alt",
    "shift": "shift",
}


class GlobalHotkey(QObject):
    """Watches for one keyboard shortcut anywhere in the system."""

    triggered = Signal()

    def __init__(self, spec: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.spec = (spec or "").strip()
        self._thread: threading.Thread | None = None
        self._runloop = None
        self._tap = None
        self._stopping = False
        self._thread_id = None

    # -- parsing ------------------------------------------------------------

    def _parse(self) -> tuple[int, set[str]] | None:
        parts = [
            p.strip().lower()
            for p in self.spec.replace("-", "+").split("+")
        ]
        parts = [p for p in parts if p]

        if not parts:
            return None

        if sys.platform == "win32":
            keycodes = _WIN_KEYCODES
        else:
            keycodes = _MAC_KEYCODES

        key = keycodes.get(parts[-1])
        if key is None:
            return None

        modifiers = set()

        for raw in parts[:-1]:
            name = _MODIFIER_ALIASES.get(raw)

            if name is None:
                return None

            if sys.platform == "win32":
                if name == "cmd":
                    name = "win"
                if name == "fn":
                    return None

            elif sys.platform == "darwin":
                if name == "win":
                    return None

            modifiers.add(name)

        return key, modifiers

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> str | None:
        """Begin listening. Returns an error message on failure."""

        if not self.spec:
            return None

        if sys.platform not in ("darwin", "win32"):
            return "Global hotkeys are not supported on this platform."

        parsed = self._parse()

        if parsed is None:
            return f"Could not understand the hotkey {self.spec!r}."

        keycode, modifiers = parsed
        ready = threading.Event()
        outcome: dict = {}

        self._stopping = False

        if sys.platform == "win32":
            target = self._run_windows
        else:
            try:
                import Quartz
            except ImportError:
                return (
                    "Global hotkey needs pyobjc: "
                    "pip install pyobjc-framework-Quartz"
                )

            target = self._run_macos

        self._thread = threading.Thread(
            target=target,
            args=(keycode, modifiers, ready, outcome),
            daemon=True,
        )
        self._thread.start()

        if not ready.wait(timeout=5.0):
            return "Global hotkey initialization timed out."

        return outcome.get("error")

    def stop(self) -> None:
        """Stop the global hotkey listener."""

        self._stopping = True

        if sys.platform == "win32" and self._thread_id is not None:
            try:
                import ctypes
                ctypes.windll.user32.PostThreadMessageW(
                    self._thread_id, 0x0012, 0, 0
                )
            except Exception:
                pass

        elif sys.platform == "darwin" and self._runloop is not None:
            try:
                import Quartz
                Quartz.CFRunLoopStop(self._runloop)
            except Exception:
                pass

    # -- Windows implementation --------------------------------------------

    def _run_windows(self, keycode, modifiers, ready, outcome) -> None:
        import ctypes
        from ctypes import wintypes

        MOD_ALT = 0x0001
        MOD_CONTROL = 0x0002
        MOD_SHIFT = 0x0004
        MOD_WIN = 0x0008
        MOD_NOREPEAT = 0x4000

        WM_HOTKEY = 0x0312
        WM_QUIT = 0x0012
        HOTKEY_ID = 1

        modifier_masks = {
            "alt": MOD_ALT,
            "ctrl": MOD_CONTROL,
            "shift": MOD_SHIFT,
            "win": MOD_WIN,
        }

        flags = MOD_NOREPEAT

        for name in modifiers:
            flags |= modifier_masks[name]

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        self._thread_id = kernel32.GetCurrentThreadId()

        # Create the thread's message queue before registering the shortcut.
        msg = wintypes.MSG()
        user32.PeekMessageW(
            ctypes.byref(msg), None, 0, 0, 0
        )

        registered = user32.RegisterHotKey(
            None, HOTKEY_ID, flags, keycode
        )

        if not registered:
            error_code = ctypes.get_last_error()
            outcome["error"] = (
                f"Could not register global hotkey {self.spec!r}. "
                f"It may already be in use by Windows or another application. "
                f"Windows error: {error_code}."
            )
            ready.set()
            return

        ready.set()

        try:
            while not self._stopping:
                result = user32.GetMessageW(
                    ctypes.byref(msg), None, 0, 0
                )

                if result <= 0 or msg.message == WM_QUIT:
                    break

                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.triggered.emit()

        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)
            self._thread_id = None

    # -- macOS implementation -----------------------------------------------

    def _run_macos(self, keycode, modifiers, ready, outcome) -> None:
        import Quartz

        masks = {
            "fn": Quartz.kCGEventFlagMaskSecondaryFn,
            "cmd": Quartz.kCGEventFlagMaskCommand,
            "ctrl": Quartz.kCGEventFlagMaskControl,
            "alt": Quartz.kCGEventFlagMaskAlternate,
            "shift": Quartz.kCGEventFlagMaskShift,
        }

        required = 0

        for name in modifiers:
            required |= masks[name]

        forbidden = 0

        for name, mask in masks.items():
            if name not in modifiers and name != "shift":
                forbidden |= mask

        def callback(proxy, event_type, event, refcon):
            try:
                if event_type == Quartz.kCGEventKeyDown:
                    code = Quartz.CGEventGetIntegerValueField(
                        event, Quartz.kCGKeyboardEventKeycode
                    )
                    flags = Quartz.CGEventGetFlags(event)

                    if (
                        code == keycode
                        and (flags & required) == required
                        and not (flags & forbidden)
                    ):
                        self.triggered.emit()

                elif event_type in (
                    Quartz.kCGEventTapDisabledByTimeout,
                    Quartz.kCGEventTapDisabledByUserInput,
                ):
                    Quartz.CGEventTapEnable(self._tap, True)

            except Exception:
                pass

            return event

        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown),
            callback,
            None,
        )

        if not tap:
            outcome["error"] = (
                "macOS denied the keyboard tap. Grant Accessibility "
                "permission and restart Aura."
            )
            ready.set()
            return

        self._tap = tap

        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        self._runloop = Quartz.CFRunLoopGetCurrent()

        Quartz.CFRunLoopAddSource(
            self._runloop, source, Quartz.kCFRunLoopCommonModes
        )

        Quartz.CGEventTapEnable(tap, True)
        ready.set()

        while not self._stopping:
            Quartz.CFRunLoopRunInMode(
                Quartz.kCFRunLoopDefaultMode, 0.25, False
            )