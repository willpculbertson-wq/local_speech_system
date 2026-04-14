"""
output.py — OS-level text injection into the active window.

Primary method: Win32 SendInput with KEYEVENTF_UNICODE.
Sends characters directly as Unicode key events — no clipboard involved, no
timing races, no interference with clipboard contents.

inject() reads up to 2 characters immediately left of the cursor via
UIAutomation right before injecting, then applies grammatical rules:

  Two-char context (preferred — from UIAutomation):
    '. '  '! '  '? '  → no space, capitalise   (new sentence after punct+space)
    '• '  '* '        → no space, capitalise   (after bullet + space)
    '\\n'  '\\n '       → no space, capitalise   (new line / paragraph)
    any letter + ' '  → no space, no cap       (mid-sentence continuation)
    ': '  '; '  '- '  → no space, no cap       (after colon / semicolon / dash)
    '— '  '… '        → no space, no cap       (after em-dash / ellipsis)

  Single-char fallback (from _last_injected_char tracking):
    .!?   → space + capitalise
    \\n/\\r → no space + capitalise
    space → no space, no cap
    other → space, no cap
    None  → no space, capitalise  (start of doc / unknown)

  Body starting with punctuation (.!?,;:)) → always attach directly, no space.

Fallback: clipboard + Ctrl+V (legacy, kept for edge-case compatibility).
Final fallback: direct keyboard typing via keyboard.write() (ASCII-only).
"""

import ctypes
import ctypes.wintypes
import logging
import threading

from cursor_context import get_preceding_chars


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


def _capitalize_first(text: str) -> str:
    return text[0].upper() + text[1:] if text else text


def _get_injection_prefix(chars: str | None, body: str) -> str:
    """Return the full string to inject: leading space/cap + body.

    chars: up to 2 characters immediately before the cursor (UIAutomation),
           or a single tracked character, or None.
    body:  stripped text to inject.

    Grammatical rules applied in priority order:
      1. Body starts with punctuation → attach directly (no space, no cap).
      2. No context (None / empty) → capitalise, no space (start of doc).
      3. Cursor after space:
           - preceded by .!? or newline or bullet (•*) → capitalise
           - preceded by anything else (letter, -:;—…,) → no cap
         Either way: no additional space (one is already there).
      4. Cursor after newline → capitalise, no space.
      5. Cursor right after .!? (no space yet) → space + capitalise.
      6. Cursor after continuation punctuation (,:;-—…) → space, no cap.
      7. Cursor after any other character → space, no cap.
    """
    # Rule 1 — punctuation body
    if body[0] in '.!?,;:)':
        return body

    # Rule 2 — unknown / start of document
    if not chars:
        return _capitalize_first(body)

    last = chars[-1]                              # char immediately left of cursor
    prev = chars[-2] if len(chars) >= 2 else ''  # char before that

    # Rule 3 — cursor is after a space
    if last in ' \t':
        if prev in '.!?' or prev in '\n\r' or prev in '•*':
            return _capitalize_first(body)        # new sentence / after bullet
        return body                               # mid-sentence continuation

    # Rule 4 — cursor after newline (new paragraph / line)
    if last in '\n\r':
        return _capitalize_first(body)

    # Rule 5 — cursor right after sentence-ending punctuation (no space yet)
    if last in '.!?':
        return ' ' + _capitalize_first(body)

    # Rule 6 — continuation punctuation (comma, colon, semicolon, dash, em-dash, ellipsis)
    if last in ',:;-—…':
        return ' ' + body

    # Rule 7 — regular character (letter, digit, quote, paren, etc.)
    return ' ' + body


class OutputInjector:
    def __init__(self, config: dict):
        self.method: str = config.get('method', 'sendinput')
        self.paste_delay: float = config.get('paste_delay_ms', 100) / 1000.0
        self._lock = threading.Lock()
        # Last character we injected — drives context-aware spacing/capitalisation.
        # None means "unknown / start of document".
        self._last_injected_char: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_last_char(self, char: str | None):
        """Override the tracked last-injected character.

        Call this before inject() when the injector's internal state is stale
        — e.g. after deleting preview chars in streaming mode, the cursor has
        moved back and the preceding character is whatever was there before the
        preview started.
        """
        self._last_injected_char = char

    def inject(self, text: str, prefix: str | None = None) -> int:
        """Inject text into the active window with context-aware spacing/capitalisation.

        prefix: when supplied explicitly (e.g. '### ' for streaming previews)
                the caller controls everything and context logic is bypassed.
                When None (default), the preceding-character rules apply:
                  space  → no extra space, no cap   (mid-sentence continuation)
                  .!?    → space + capitalise        (new sentence)
                  \\n/\\r  → no space + capitalise    (new line / paragraph)
                  other  → space, no cap             (mid-word continuation)
                  None   → no space + capitalise     (unknown / start of doc)

        Returns the number of characters injected.
        """
        if not text.strip():
            return 0

        body = text.strip()

        if prefix is not None:
            # Explicit prefix — caller controls spacing and capitalisation entirely
            final = prefix + body
        else:
            # Refresh cursor context from UIAutomation right before injecting.
            # By inject() time the text editor is reliably focused (audio capture
            # and transcription take 1-5 s), so this read is accurate regardless
            # of where the user navigated since the last session.
            # Falls back silently to tracked _last_injected_char on any failure.
            ctx = get_preceding_chars(2)
            if ctx is not None:
                self._last_injected_char = ctx[-1]
            chars_for_logic = ctx if ctx is not None else self._last_injected_char
            final = _get_injection_prefix(chars_for_logic, body)

        self._send(final)
        if final:
            self._last_injected_char = final[-1]
        return len(final)

    def _inject_raw(self, text: str) -> int:
        """Inject text exactly as given, with no prefix or capitalisation changes."""
        if not text:
            return 0
        self._send(text)
        if text:
            self._last_injected_char = text[-1]
        return len(text)

    def _send(self, text: str):
        """Dispatch to the configured injection backend."""
        if self.method == 'clipboard':
            self._inject_via_clipboard(text)
        elif self.method == 'direct_keyboard':
            self._inject_via_keyboard(text)
        else:
            self._inject_via_sendinput(text)

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
