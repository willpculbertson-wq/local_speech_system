"""
output.py — OS-level text injection into the active window.

OutputInjector uses clipboard + Ctrl+V to insert text. This approach works
universally across Windows applications: browsers, editors, terminals, Office,
etc. It correctly handles Unicode (unlike keyboard.write() which is ASCII-only).

Flow:
  1. Save current clipboard contents
  2. Copy new text to clipboard
  3. Sleep paste_delay (ensures clipboard is set before Ctrl+V)
  4. Send Ctrl+V via pyautogui
  5. Sleep briefly, then restore original clipboard

Fallback: direct keyboard typing via keyboard.write() (ASCII-only, slower).
"""

import logging
import threading
import time


class OutputInjector:
    def __init__(self, config: dict):
        self.method: str = config.get('method', 'clipboard')
        self.paste_delay: float = config.get('paste_delay_ms', 100) / 1000.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def inject(self, text: str):
        if not text.strip():
            return

        if self.method == 'clipboard':
            self._inject_via_clipboard(text)
        else:
            self._inject_via_keyboard(text)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _inject_via_clipboard(self, text: str):
        import pyautogui
        import pyperclip

        # Serialise injections so rapid consecutive flushes don't race
        with self._lock:
            original: str | None = None
            try:
                # Save original clipboard so we can restore it
                try:
                    original = pyperclip.paste()
                except Exception:
                    original = None

                pyperclip.copy(text)
                time.sleep(self.paste_delay)

                # Ctrl+V into the currently focused window
                pyautogui.hotkey('ctrl', 'v')

                # Brief pause so the paste completes before we clobber the clipboard
                time.sleep(0.05)

                if original is not None:
                    pyperclip.copy(original)

                logging.info(f"Injected {len(text)} chars via clipboard")

            except Exception as e:
                logging.error(f"Clipboard injection failed: {e}", exc_info=True)
                # Attempt keyboard fallback
                self._inject_via_keyboard(text)

    def _inject_via_keyboard(self, text: str):
        """Direct keystroke fallback. Reliable for ASCII; may mangle Unicode."""
        try:
            import keyboard as kb
            kb.write(text, delay=0.005)
            logging.info(f"Injected {len(text)} chars via keyboard")
        except Exception as e:
            logging.error(f"Keyboard injection failed: {e}", exc_info=True)
