"""
indicator.py — Animated typing indicator injected while dictation is processing.

Injects middle dots (·, U+00B7) at the cursor position to signal that the
system is listening and waiting for transcription. One dot added per second,
up to MAX_DOTS, then holds until real text is injected.

Example progression (at 1 s intervals after a 150 ms startup delay):
    ·   ··   ···   ····   ·····   [holds here]

The indicator is cleared automatically by OutputPipeline before any real text
is injected, and by DictationSystem on cancel or empty-session stop.
"""

import logging
import threading


class TypingIndicator:
    DOT_CHAR      = '\u00B7'   # · middle dot — distinct from sentence-ending period
    MAX_DOTS      = 5
    TICK_INTERVAL = 1.0        # seconds between dots
    STARTUP_DELAY = 0.15       # seconds to wait for focus to return after hotkey

    def __init__(self, injector):
        self._injector = injector
        self._lock = threading.Lock()
        self._active = False
        self._timer: threading.Timer | None = None
        self._injected_chars: int = 0
        self._dot_count: int = 0
        # Injector's last-char state before the first dot — restored on cleanup.
        self._pre_indicator_last_char: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Start injecting dots. Call from _start_listening()."""
        with self._lock:
            self._active = True
            self._injected_chars = 0
            self._dot_count = 0
            self._pre_indicator_last_char = self._injector._last_injected_char
        self._schedule(self.STARTUP_DELAY)

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

    def _schedule(self, delay: float):
        t = threading.Timer(delay, self._tick)
        t.daemon = True
        t.start()
        with self._lock:
            if self._active:
                self._timer = t
            else:
                t.cancel()   # was stopped during the delay — abort silently

    def _tick(self):
        with self._lock:
            if not self._active or self._dot_count >= self.MAX_DOTS:
                return
            self._dot_count += 1
            # Pre-reserve the slot before releasing the lock.
            # If stop() fires between here and _inject_raw, the count is still
            # correct — the caller will delete this char even if it's already
            # been written (deleting past end-of-doc is a silent no-op in Win32).
            self._injected_chars += 1

        n = self._injector._inject_raw(self.DOT_CHAR)
        logging.debug(f"TypingIndicator: dot {self._dot_count}/{self.MAX_DOTS}")

        # _inject_raw should always return 1, but correct if it ever returns 0.
        if n == 0:
            with self._lock:
                self._injected_chars = max(0, self._injected_chars - 1)

        # Decide whether to schedule next tick while briefly holding the lock,
        # but call _schedule() OUTSIDE the lock — _schedule() acquires the same
        # lock internally, so calling it while holding would deadlock.
        with self._lock:
            schedule_next = self._active and self._dot_count < self.MAX_DOTS

        if schedule_next:
            self._schedule(self.TICK_INTERVAL)
