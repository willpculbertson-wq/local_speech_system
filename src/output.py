"""
output.py — OS-level text injection into the active window.

Primary method: Win32 SendInput with KEYEVENTF_UNICODE.
Sends characters directly as Unicode key events — no clipboard involved, no
timing races, no interference with clipboard contents.

Fallback: clipboard + Ctrl+V (legacy, kept for edge-case compatibility).
Final fallback: direct keyboard typing via keyboard.write() (ASCII-only).
"""

import ctypes
import ctypes.wintypes
import logging
import threading


# ---------------------------------------------------------------------------
# Win32 structures — defined once at module level, shared by inject + delete
# ---------------------------------------------------------------------------

_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_VK_BACK = 0x08
_VK_RETURN = 0x0D


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ('wVk',        ctypes.wintypes.WORD),
        ('wScan',      ctypes.wintypes.WORD),
        ('dwFlags',    ctypes.wintypes.DWORD),
        ('time',       ctypes.wintypes.DWORD),
        ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ('type',    ctypes.wintypes.DWORD),
        ('ki',      _KEYBDINPUT),
        ('padding', ctypes.c_ubyte * 8),
    ]


_SendInput = ctypes.windll.user32.SendInput
_INPUT_SIZE = ctypes.sizeof(_INPUT)


class OutputInjector:
    def __init__(self, config: dict):
        self.method: str = config.get('method', 'sendinput')
        self.paste_delay: float = config.get('paste_delay_ms', 100) / 1000.0
        self._lock = threading.Lock()
        self._suppress_next_space: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def suppress_leading_space(self):
        """Suppress the space prefix on the next inject() call.

        Call before each dictation session so the first word doesn't start with
        a space when the cursor is already at the beginning of a line.
        Only affects inject() with no explicit prefix — has no effect on
        inject(text, prefix='...') or _inject_raw().
        """
        self._suppress_next_space = True

    def inject(self, text: str, prefix: str | None = None) -> int:
        """Inject text into the active window.

        prefix: string prepended to the text (default ' ' to separate from prior
                text). Pass an explicit value to override, e.g. prefix='### '.
                When prefix is None the suppress_leading_space flag is consulted.
        Returns the number of characters injected (including the prefix).
        """
        if not text.strip():
            return 0

        if prefix is None:
            if self._suppress_next_space:
                prefix = ''
                self._suppress_next_space = False
            else:
                prefix = ' '

        text = prefix + text.strip()

        if self.method == 'clipboard':
            self._inject_via_clipboard(text)
        elif self.method == 'direct_keyboard':
            self._inject_via_keyboard(text)
        else:
            self._inject_via_sendinput(text)

        return len(text)

    def _inject_raw(self, text: str) -> int:
        """Inject text with no prefix at all. Returns char count."""
        if not text:
            return 0
        if self.method == 'clipboard':
            self._inject_via_clipboard(text)
        elif self.method == 'direct_keyboard':
            self._inject_via_keyboard(text)
        else:
            self._inject_via_sendinput(text)
        return len(text)

    def delete_chars(self, n: int):
        """Delete n characters left of the cursor via a batched Win32 SendInput call."""
        if n <= 0:
            return

        inputs = (_INPUT * (2 * n))()
        for i in range(n):
            inputs[2 * i].type = _INPUT_KEYBOARD
            inputs[2 * i].ki.wVk = _VK_BACK
            inputs[2 * i].ki.dwFlags = 0
            inputs[2 * i + 1].type = _INPUT_KEYBOARD
            inputs[2 * i + 1].ki.wVk = _VK_BACK
            inputs[2 * i + 1].ki.dwFlags = _KEYEVENTF_KEYUP

        sent = _SendInput(2 * n, inputs, _INPUT_SIZE)
        if sent != 2 * n:
            logging.warning(f"delete_chars: SendInput sent {sent}/{2 * n} events")
        else:
            logging.info(f"Deleted {n} chars via SendInput")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _inject_via_sendinput(self, text: str):
        """Inject text as Unicode key events. No clipboard involved."""
        # Build one keydown+keyup pair per character.
        # Newlines are sent as VK_RETURN so they work across apps.
        # Characters outside the Basic Multilingual Plane are skipped
        # (surrogate pairs would need 4 events; rare in dictation output).
        events: list[tuple[str, int]] = []
        for ch in text:
            cp = ord(ch)
            if ch == '\n':
                events.append(('vk', _VK_RETURN))
            elif cp <= 0xFFFF:
                events.append(('uni', cp))
            # else: skip non-BMP character

        n = len(events)
        if n == 0:
            return

        inputs = (_INPUT * (2 * n))()
        for i, (kind, value) in enumerate(events):
            if kind == 'vk':
                inputs[2 * i].type = _INPUT_KEYBOARD
                inputs[2 * i].ki.wVk = value
                inputs[2 * i].ki.dwFlags = 0
                inputs[2 * i + 1].type = _INPUT_KEYBOARD
                inputs[2 * i + 1].ki.wVk = value
                inputs[2 * i + 1].ki.dwFlags = _KEYEVENTF_KEYUP
            else:
                inputs[2 * i].type = _INPUT_KEYBOARD
                inputs[2 * i].ki.wVk = 0
                inputs[2 * i].ki.wScan = value
                inputs[2 * i].ki.dwFlags = _KEYEVENTF_UNICODE
                inputs[2 * i + 1].type = _INPUT_KEYBOARD
                inputs[2 * i + 1].ki.wVk = 0
                inputs[2 * i + 1].ki.wScan = value
                inputs[2 * i + 1].ki.dwFlags = _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP

        with self._lock:
            sent = _SendInput(2 * n, inputs, _INPUT_SIZE)
            if sent != 2 * n:
                logging.warning(f"inject: SendInput sent {sent}/{2 * n} events")
            else:
                logging.info(f"Injected {len(text)} chars via SendInput Unicode")

    def _inject_via_clipboard(self, text: str):
        """Legacy clipboard + Ctrl+V injection. Kept for compatibility."""
        import pyautogui
        import pyperclip
        import time

        with self._lock:
            try:
                try:
                    original = pyperclip.paste()
                except Exception:
                    original = None

                pyperclip.copy(text)
                time.sleep(self.paste_delay)
                pyautogui.hotkey('ctrl', 'v')
                time.sleep(0.05)

                if original is not None:
                    pyperclip.copy(original)

                logging.info(f"Injected {len(text)} chars via clipboard")

            except Exception as e:
                logging.error(f"Clipboard injection failed: {e}", exc_info=True)
                self._inject_via_keyboard(text)

    def _inject_via_keyboard(self, text: str):
        """Direct keystroke fallback. Reliable for ASCII; may mangle Unicode."""
        try:
            import keyboard as kb
            kb.write(text, delay=0.005)
            logging.info(f"Injected {len(text)} chars via keyboard")
        except Exception as e:
            logging.error(f"Keyboard injection failed: {e}", exc_info=True)
