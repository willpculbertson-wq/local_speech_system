"""
main.py — Entry point and system orchestrator.

Wires all pipeline stages together and manages their lifecycle.

Pipeline:
  AudioCapture → [audio_queue] → VADProcessor → [speech_queue]
  → TranscriptionWorker → [text_queue] → TranscriptionBuffer
  → [output_queue] → OutputPipeline → TextStructurer → OutputInjector

Usage:
  python src/main.py              # Normal run
  python src/main.py --debug      # Verbose debug logging
  python src/main.py --list-devices  # Print audio devices and exit

Hotkeys (configurable in config/settings.yaml):
  Ctrl+`   Toggle listening ON/OFF
  Escape   Cancel listening (without flushing current buffer)

NOTE: May require an elevated (Administrator) terminal on Windows for the
      global hotkey to register correctly via the keyboard library.
"""

import argparse
import logging
import queue
import signal
import sys
import threading
from pathlib import Path

import pyautogui
import yaml


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config() -> dict:
    config_path = Path(__file__).parent.parent / 'config' / 'settings.yaml'
    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(debug: bool = False):
    level = logging.DEBUG if debug else logging.INFO
    fmt = '%(asctime)s [%(threadName)-16s] %(levelname)-8s %(message)s'
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('dictation.log', encoding='utf-8'),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers)
    # Suppress noisy third-party loggers at debug level
    for name in ('faster_whisper', 'silero_vad', 'urllib3', 'httpx', 'comtypes'):
        logging.getLogger(name).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# StreamingState — thread-safe tracker for preview-mode pending chars
# ---------------------------------------------------------------------------

class StreamingState:
    """Tracks how many characters have been injected as preview text.

    Used by OutputPipeline (OutputThread) and cancel_listening (hotkey thread)
    concurrently. All methods are lock-protected.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._pending_chars: int = 0
        self._cancelled: bool = False
        self._first_preview: bool = True
        # The injector's last-char state immediately before the first preview was
        # injected. Restored before the final inject so context is correct.
        self._pre_preview_last_char: str | None = None

    def add_chars(self, n: int):
        with self._lock:
            self._pending_chars += n

    def consume_if_not_cancelled(self) -> tuple[int, bool]:
        """Atomically consume pending chars if session was not cancelled.

        Returns (chars, was_cancelled). If cancelled, resets the flag and
        returns (0, True). Otherwise returns (pending_chars, False) and resets
        pending_chars to 0.
        """
        with self._lock:
            if self._cancelled:
                self._cancelled = False
                self._pending_chars = 0
                return (0, True)
            n = self._pending_chars
            self._pending_chars = 0
            return (n, False)

    def cancel_and_consume(self) -> int:
        """Called by cancel_listening. Returns chars to delete and sets cancelled flag."""
        with self._lock:
            n = self._pending_chars
            self._pending_chars = 0
            self._cancelled = True
            return n

    def take_first_preview(self) -> bool:
        """Returns True the first time it is called per session, False thereafter."""
        with self._lock:
            if self._first_preview:
                self._first_preview = False
                return True
            return False

    def save_pre_preview_char(self, char: str | None):
        with self._lock:
            self._pre_preview_last_char = char

    def get_pre_preview_char(self) -> str | None:
        with self._lock:
            return self._pre_preview_last_char

    def on_flush_complete(self, last_char: str | None):
        """Called after each final inject to prepare for the next flush cycle.

        Updates _pre_preview_last_char so the next cycle's set_last_char() call
        restores the correct context (the char after the previous clean inject,
        not the char from the start of the session).

        Also resets _first_preview so the next preview batch gets a fresh ### marker.
        """
        with self._lock:
            self._pre_preview_last_char = last_char
            self._first_preview = True

    def reset_cancel(self):
        """Called at the start of a new listening session."""
        with self._lock:
            self._cancelled = False
            self._pending_chars = 0
            self._first_preview = True
            self._pre_preview_last_char = None


# ---------------------------------------------------------------------------
# OutputPipeline thread
# ---------------------------------------------------------------------------

class OutputPipeline(threading.Thread):
    """Reads flushed text from output_queue, structures it, then injects.

    In streaming mode, the queue carries typed dicts:
      {'type': 'preview', 'text': fragment}  — inject raw immediately
      {'type': 'final',   'text': combined}  — replace previews with cleaned text

    In non-streaming mode, the queue carries plain strings (existing behaviour).
    """

    def __init__(
        self,
        input_queue: queue.Queue,
        structurer,
        injector,
        streaming_state: StreamingState | None = None,
    ):
        super().__init__(name='OutputThread', daemon=True)
        self.input_queue = input_queue
        self.structurer = structurer
        self.injector = injector
        self.streaming_state = streaming_state
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.is_set():
            try:
                msg = self.input_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if msg is None:  # Shutdown sentinel
                break

            try:
                if isinstance(msg, dict):
                    msg_type = msg.get('type')
                    if msg_type == 'preview':
                        self._handle_preview(msg['text'])
                    elif msg_type == 'final':
                        self._handle_final(msg['text'])
                    else:
                        logging.warning(f"OutputPipeline: unknown message type {msg_type!r}")
                else:
                    # Non-streaming: plain string
                    logging.info(f"OutputPipeline: received text ({len(msg.split())} words)")
                    cleaned = self.structurer.process(msg)
                    logging.info("OutputPipeline: structured, injecting...")
                    self.injector.inject(cleaned)
                    logging.info("OutputPipeline: injection complete")
            except Exception as e:
                logging.error(f"OutputPipeline crashed: {e}", exc_info=True)

    def _handle_preview(self, text: str):
        logging.debug(f"OutputPipeline: preview {text!r}")
        if self.streaming_state is not None and self.streaming_state.take_first_preview():
            # Open with ### to signal "still processing".
            # Pre-preview context comes from reset_cancel() (None = fresh session start)
            # or on_flush_complete() (correct context after each flush cycle).
            chars = self.injector.inject(text, prefix='### ')
        else:
            chars = self.injector.inject(text)
        if self.streaming_state is not None:
            self.streaming_state.add_chars(chars)

    def _handle_final(self, text: str):
        logging.info(f"OutputPipeline: final flush ({len(text.split())} words), structuring...")
        cleaned = self.structurer.process(text)

        if self.streaming_state is None:
            self.injector.inject(cleaned)
            return

        chars_to_delete, was_cancelled = self.streaming_state.consume_if_not_cancelled()

        if was_cancelled:
            logging.info("OutputPipeline: session cancelled, discarding final flush")
            return

        if chars_to_delete > 0:
            # Close the ### marker so the user sees ###preview### before replacement
            close = self.injector._inject_raw('###')
            chars_to_delete += close
            logging.info(f"OutputPipeline: deleting {chars_to_delete} preview chars (incl. markers)")
            self.injector.delete_chars(chars_to_delete)
            # Restore the injector's context to what it was before the preview started
            self.injector.set_last_char(self.streaming_state.get_pre_preview_char())

        logging.info("OutputPipeline: injecting cleaned text")
        self.injector.inject(cleaned)
        logging.info("OutputPipeline: final injection complete")
        # Prepare context for the next flush cycle within this session
        self.streaming_state.on_flush_complete(self.injector._last_injected_char)

    def stop(self):
        self._stop_event.set()


# ---------------------------------------------------------------------------
# DictationSystem
# ---------------------------------------------------------------------------

class DictationSystem:
    def __init__(self, config: dict):
        self.config = config
        self._listening = False
        self._lock = threading.Lock()
        self._shutdown_event = threading.Event()

        # Import here so errors surface at startup with a clear message
        from audio import AudioCapture
        from buffer import TranscriptionBuffer
        from output import OutputInjector
        from structure import TextStructurer
        from transcribe import TranscriptionWorker
        from vad import VADProcessor

        streaming_enabled: bool = config.get('output', {}).get('streaming_preview', False)

        # Inter-thread queues (maxsize=50 provides backpressure)
        self._audio_queue: queue.Queue = queue.Queue(maxsize=50)
        self._speech_queue: queue.Queue = queue.Queue(maxsize=20)
        self._text_queue: queue.Queue = queue.Queue(maxsize=20)
        self._output_queue: queue.Queue = queue.Queue(maxsize=20)

        # Streaming state (only used when streaming_preview is enabled)
        self._streaming_state: StreamingState | None = (
            StreamingState() if streaming_enabled else None
        )

        # Workers
        self._audio = AudioCapture(config['audio'], self._audio_queue)
        self._vad = VADProcessor(config['vad'], self._audio_queue, self._speech_queue)
        self._transcriber = TranscriptionWorker(
            config['transcription'], self._speech_queue, self._text_queue
        )
        self._buffer = TranscriptionBuffer(
            config['buffer'], self._text_queue, self._output_queue,
            streaming=streaming_enabled,
        )
        self._structurer = TextStructurer(config['structuring'])
        self._injector = OutputInjector(config['output'])
        self._output_pipeline = OutputPipeline(
            self._output_queue, self._structurer, self._injector,
            streaming_state=self._streaming_state,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start background worker threads. Audio capture starts on first toggle."""
        self._vad.start()
        self._transcriber.start()
        self._buffer.start()
        self._output_pipeline.start()
        logging.info("All pipeline workers started")

    def shutdown(self):
        logging.info("Shutting down...")
        if self._listening:
            self._stop_listening()

        # Send sentinels to unblock all blocking queue.get() calls
        self._audio_queue.put(None)
        self._speech_queue.put(None)
        self._text_queue.put(None)
        self._output_queue.put(None)

        self._vad.stop()
        self._transcriber.stop()
        self._buffer.stop()
        self._output_pipeline.stop()

        self._shutdown_event.set()
        logging.info("Shutdown complete")

    def wait_for_shutdown(self):
        self._shutdown_event.wait()

    # ------------------------------------------------------------------
    # Hotkey callbacks
    # ------------------------------------------------------------------

    def toggle_listening(self):
        with self._lock:
            if self._listening:
                self._stop_listening()
            else:
                self._start_listening()

    def cancel_listening(self):
        """Stop listening and discard the current buffer without injecting."""
        with self._lock:
            if self._listening:
                logging.info("Listening cancelled (buffer discarded)")
                self._listening = False
                self._audio.stop()
                self._vad.reset()
                # Clear the buffer without flushing to output
                self._buffer._buffer.clear()
                # Delete any preview chars already injected onto screen
                if self._streaming_state is not None:
                    chars = self._streaming_state.cancel_and_consume()
                    if chars > 0:
                        logging.info(f"cancel_listening: deleting {chars} preview chars")
                        self._injector.delete_chars(chars)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start_listening(self):
        self._listening = True
        self._vad.reset()
        if self._streaming_state is not None:
            self._streaming_state.reset_cancel()
        self._audio.start()
        logging.info("=== LISTENING ON ===  (press Ctrl+` to stop)")
        print("\n[LISTENING]", flush=True)

    def _stop_listening(self):
        self._listening = False
        self._audio.stop()
        self._vad.reset()
        # Flush any accumulated text in the buffer
        self._buffer.flush_now()
        logging.info("=== LISTENING OFF ===")
        print("[STOPPED]", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='Local Speech Dictation System')
    p.add_argument('--debug', action='store_true', help='Enable verbose logging')
    p.add_argument(
        '--list-devices',
        action='store_true',
        help='Print audio input devices and exit',
    )
    return p.parse_args()


def main():
    args = parse_args()
    setup_logging(debug=args.debug)

    if args.list_devices:
        from audio import AudioCapture
        AudioCapture.list_devices()
        return

    config = load_config()

    # Remove pyautogui's built-in delay between key events (we control timing)
    pyautogui.PAUSE = 0
    pyautogui.FAILSAFE = False  # Disable move-to-corner failsafe for headless use

    system = DictationSystem(config)
    system.start()

    # Register global hotkeys
    import keyboard

    hotkey_cfg = config.get('hotkey', {})
    toggle_key: str = hotkey_cfg.get('toggle', 'ctrl+`')
    cancel_key: str = hotkey_cfg.get('cancel', 'escape')

    keyboard.add_hotkey(toggle_key, system.toggle_listening, suppress=True)
    keyboard.add_hotkey(cancel_key, system.cancel_listening, suppress=False)

    logging.info(
        f"Dictation system ready.\n"
        f"  Toggle:  {toggle_key}\n"
        f"  Cancel:  {cancel_key}\n"
        f"  Stop:    Ctrl+C\n"
    )
    print(
        f"\nDictation system ready.\n"
        f"  Press {toggle_key} to start/stop listening.\n"
        f"  Press {cancel_key} to cancel without injecting.\n"
        f"  Press Ctrl+C to exit.\n",
        flush=True,
    )

    # Handle Ctrl+C / SIGTERM cleanly
    def _handle_signal(sig, frame):
        print("\nShutting down...", flush=True)
        system.shutdown()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    system.wait_for_shutdown()


if __name__ == '__main__':
    main()
