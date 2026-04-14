"""
indicator.py — Visual feedback injected into the active text window.

Two indicator modes, used at different points in the dictation lifecycle:

  start_listening()  →  injects '<listening>' when the user toggles on.
                         Tells the user the system is armed and waiting.

  start_dots()       →  injects stacking middle dots (·) after each text
                         injection to show the system is still listening.
                         One dot per second, up to MAX_DOTS, then holds.

Both modes share the same stop() / cleanup path: stop() cancels any pending
timer, returns (chars_to_delete, pre_indicator_last_char) so the caller can
delete the injected chars and restore the injector's context.

stop() is idempotent — safe to call even if the indicator is not active.
"""

import logging
import threading


class TypingIndicator:
    LISTENING_TEXT = '<listening>'
    DOT_CHAR       = '\u00B7'   # · middle dot
    MAX_DOTS       = 5
    TICK_INTERVAL  = 1.0        # seconds between dots (also delay before first dot)
    STARTUP_DELAY  = 0.15       # seconds to wait for focus to return after hotkey

    def __init__(self, injector):
        self._injector = injector
        self._lock = threading.Lock()
        self._active = False
        self._timer: threading.Timer | None = None
        self._injected_chars: int = 0
        self._dot_count: int = 0
        self._pre_indicator_last_char: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_listening(self):
        """Inject '<listening>' after startup delay. Call from _start_listening().

        The startup delay (150 ms) lets focus return to the text editor after
        the hotkey press before we try to inject anything.
        """
        self._begin(self.STARTUP_DELAY, dots_mode=False)

    def start_dots(self):
        """Start stacking dots. Call after each successful text injection.

        First dot appears after TICK_INTERVAL (1 s) so the user can read
        the injected text before the indicator reappears.
        """
        self._begin(self.TICK_INTERVAL, dots_mode=True)

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

    def _begin(self, initial_delay: float, dots_mode: bool):
        """Shared startup for both modes."""
        with self._lock:
            self._active = True
            self._injected_chars = 0
            self._dot_count = 0
            self._pre_indicator_last_char = self._injector._last_injected_char
        fn = self._tick_dots if dots_mode else self._tick_listening
        self._schedule(initial_delay, fn)

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
            self._injected_chars += len(self.LISTENING_TEXT)

        n = self._injector._inject_raw(self.LISTENING_TEXT)
        logging.debug(f"TypingIndicator: injected '{self.LISTENING_TEXT}'")

        # Correct the count if injection returned fewer chars than expected.
        if n != len(self.LISTENING_TEXT):
            with self._lock:
                self._injected_chars = n

        # No rescheduling — one-shot mode.

    def _tick_dots(self):
        """Repeating: inject one dot per tick until MAX_DOTS is reached."""
        with self._lock:
            if not self._active or self._dot_count >= self.MAX_DOTS:
                return
            self._dot_count += 1
            # Pre-reserve slot so stop() sees the correct count even if it
            # races with the _inject_raw call below.
            self._injected_chars += 1

        n = self._injector._inject_raw(self.DOT_CHAR)
        logging.debug(f"TypingIndicator: dot {self._dot_count}/{self.MAX_DOTS}")

        if n == 0:
            with self._lock:
                self._injected_chars = max(0, self._injected_chars - 1)

        # Decide whether to reschedule — must read flag while holding lock,
        # then call _schedule() OUTSIDE the lock (_schedule also acquires it,
        # so calling it while holding would deadlock on Python's non-reentrant Lock).
        with self._lock:
            schedule_next = self._active and self._dot_count < self.MAX_DOTS

        if schedule_next:
            self._schedule(self.TICK_INTERVAL, self._tick_dots)
