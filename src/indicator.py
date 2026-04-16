"""
indicator.py — Visual feedback injected into the active text window.

  start_listening()       →  injects '<listening>' after a short focus-return
                              delay (150 ms).  Used at session start, called
                              from the hotkey thread where the editor may not
                              have focus yet.

  start_listening_sync()  →  injects '<listening>' synchronously on the
                              calling thread (no timer, no delay).  Used by
                              OutputThread after each text injection; the editor
                              is already focused and we need the injection to
                              complete — and any stop()-race self-clean to run —
                              before the next queue message is processed.

  stop()                  →  cancels any pending timer, returns
                              (chars_to_delete, pre_indicator_last_char) so the
                              caller can delete the injected chars and restore
                              the injector's context.

stop() is idempotent — safe to call even if the indicator is not active.
"""

import logging
import threading


class TypingIndicator:
    LISTENING_TEXT = '<listening>'
    STARTUP_DELAY  = 0.15   # seconds to wait for focus to return after hotkey

    def __init__(self, injector):
        self._injector = injector
        self._lock = threading.Lock()
        self._active = False
        self._timer: threading.Timer | None = None
        self._injected_chars: int = 0
        self._pre_indicator_last_char: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_listening(self):
        """Inject '<listening>' after STARTUP_DELAY (150 ms).

        Use at session start from the hotkey thread where focus may not yet
        have returned to the editor.
        """
        self._begin(self.STARTUP_DELAY)

    def start_listening_sync(self):
        """Inject '<listening>' synchronously on the calling thread.

        Use from OutputThread after each text injection.  Because this runs
        inline (no timer), any stop()-race self-clean completes before the
        thread dequeues the next message — preventing self-clean from firing
        after real text has already been appended to the cursor position.
        """
        with self._lock:
            self._active = True
            self._pre_indicator_last_char = self._injector._last_injected_char
            self._injected_chars = 0

        n = self._injector._inject_raw(self.LISTENING_TEXT)
        logging.debug(f"TypingIndicator: injected '{self.LISTENING_TEXT}' (sync)")

        with self._lock:
            if not self._active:
                # stop() was called while we were injecting.  The text is on
                # screen but stop() returned (0, pre), so the caller won't
                # erase it.  Self-clean now — this runs before the next queue
                # message is processed because we're on the same thread.
                if n > 0:
                    self._injector.delete_chars(n)
                    self._injector._last_injected_char = self._pre_indicator_last_char
                return
            self._injected_chars = n

    def stop(self) -> tuple[int, str | None]:
        """Stop the indicator. Returns (chars_to_delete, pre_indicator_last_char).

        Idempotent — safe to call multiple times. Second and subsequent calls
        return (0, None) so callers can always safely delete the returned count.
        """
        with self._lock:
            if not self._active:
                return 0, None
            self._active = False
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            n   = self._injected_chars
            pre = self._pre_indicator_last_char
            self._injected_chars = 0
        return n, pre

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _begin(self, initial_delay: float):
        with self._lock:
            self._active = True
            self._injected_chars = 0
            self._pre_indicator_last_char = self._injector._last_injected_char
        self._schedule(initial_delay, self._tick_listening)

    def _schedule(self, delay: float, fn):
        t = threading.Timer(delay, fn)
        t.daemon = True
        t.start()
        with self._lock:
            if self._active:
                self._timer = t
            else:
                t.cancel()   # was stopped during the delay — abort silently

    def _tick_listening(self):
        """One-shot: inject the '<listening>' marker."""
        with self._lock:
            if not self._active:
                return

        # Inject outside the lock so stop() is never blocked by a slow send.
        n = self._injector._inject_raw(self.LISTENING_TEXT)
        logging.debug(f"TypingIndicator: injected '{self.LISTENING_TEXT}'")

        with self._lock:
            if not self._active:
                # stop() was called while we were injecting — the text is on
                # screen but stop() already returned (0, pre) so the caller
                # won't erase it.  Clean it up now.
                if n > 0:
                    self._injector.delete_chars(n)
                    self._injector._last_injected_char = self._pre_indicator_last_char
                return
            # Still active — record the count so stop() knows what to erase.
            self._injected_chars = n
