"""
buffer.py — Accumulates transcription fragments and flushes coherent chunks.

TranscriptionBuffer is a daemon thread that reads raw text strings from
input_queue, accumulates them, and flushes to output_queue when any of these
conditions are met:

  1. Word count >= max_words
  2. Silence timeout: no new text for max_silence_ms
  3. Last fragment ends with a sentence boundary character (. ! ?)
  4. flush_now() called explicitly (e.g. when toggling listening OFF)

The output_queue receives complete, coherent text strings ready for structuring
and injection.
"""

import logging
import queue
import threading
import time


class TranscriptionBuffer(threading.Thread):
    def __init__(
        self,
        config: dict,
        input_queue: queue.Queue,
        output_queue: queue.Queue,
        streaming: bool = False,
    ):
        super().__init__(name='BufferThread', daemon=True)
        self.input_queue = input_queue
        self.output_queue = output_queue
        self._streaming = streaming
        self._stop_event = threading.Event()
        self._flush_event = threading.Event()

        self._buffer: list[str] = []
        self._buffer_lock = threading.Lock()
        self._last_input_time: float = 0.0

        self.max_words: int = config.get('max_words', 200)
        self.max_silence_s: float = config.get('max_silence_ms', 1500) / 1000.0
        self.sentence_end_chars: frozenset[str] = frozenset(
            config.get('sentence_end_chars', ['.', '!', '?'])
        )

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def run(self):
        while not self._stop_event.is_set():
            # Check for an explicit flush request first
            if self._flush_event.is_set():
                self._flush('manual')
                self._flush_event.clear()

            # Try to get new text (50ms timeout sets the check granularity)
            try:
                text = self.input_queue.get(timeout=0.05)
            except queue.Empty:
                text = None

            if text is not None:
                if isinstance(text, dict):
                    # Pipeline sentinel (e.g. session_end) — flush remaining text, then forward.
                    self._flush('session_end')
                    try:
                        self.output_queue.put(text, timeout=2.0)
                    except queue.Full:
                        logging.warning("Buffer: output_queue full, dropping session_end sentinel")
                    continue

                with self._buffer_lock:
                    self._buffer.append(text)
                    self._last_input_time = time.monotonic()
                    word_count = sum(len(t.split()) for t in self._buffer)

                # Emit preview immediately (outside lock — put() can block)
                if self._streaming:
                    try:
                        self.output_queue.put({'type': 'preview', 'text': text}, timeout=2.0)
                    except queue.Full:
                        logging.warning("Buffer: output_queue full, dropping preview")

                # Flush on word limit
                if word_count >= self.max_words:
                    self._flush('word_limit')
                    continue

                # Flush on sentence boundary
                stripped = text.strip()
                if stripped and stripped[-1] in self.sentence_end_chars:
                    self._flush('sentence_boundary')
                    continue

            # Flush on silence timeout — check under lock, flush outside it
            # (Lock is not reentrant; _flush() also acquires it)
            should_flush = False
            with self._buffer_lock:
                if (
                    self._buffer
                    and self._last_input_time > 0
                    and time.monotonic() - self._last_input_time > self.max_silence_s
                ):
                    should_flush = True
            if should_flush:
                self._flush('silence_timeout')

    def stop(self):
        self._stop_event.set()

    def flush_now(self):
        """Force an immediate flush. Safe to call from any thread (e.g. hotkey)."""
        self._flush_event.set()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _flush(self, reason: str):
        with self._buffer_lock:
            if not self._buffer:
                return
            combined = ' '.join(self._buffer).strip()
            self._buffer.clear()
            self._last_input_time = 0.0

        if not combined:
            return

        word_count = len(combined.split())
        logging.info(f"Buffer flush ({reason}): {word_count} words → sending to output")

        try:
            msg = {'type': 'final', 'text': combined} if self._streaming else combined
            self.output_queue.put(msg, timeout=2.0)
        except queue.Full:
            logging.warning("Buffer: output_queue full, dropping flush")
