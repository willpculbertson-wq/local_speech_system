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
    for name in ('faster_whisper', 'silero_vad', 'urllib3', 'httpx'):
        logging.getLogger(name).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# OutputPipeline thread
# ---------------------------------------------------------------------------

class OutputPipeline(threading.Thread):
    """Reads flushed text from output_queue, structures it, then injects."""

    def __init__(
        self,
        input_queue: queue.Queue,
        structurer,
        injector,
    ):
        super().__init__(name='OutputThread', daemon=True)
        self.input_queue = input_queue
        self.structurer = structurer
        self.injector = injector
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.is_set():
            try:
                text = self.input_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if text is None:  # Shutdown sentinel
                break

            cleaned = self.structurer.process(text)
            self.injector.inject(cleaned)

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

        # Inter-thread queues (maxsize=50 provides backpressure)
        self._audio_queue: queue.Queue = queue.Queue(maxsize=50)
        self._speech_queue: queue.Queue = queue.Queue(maxsize=20)
        self._text_queue: queue.Queue = queue.Queue(maxsize=20)
        self._output_queue: queue.Queue = queue.Queue(maxsize=20)

        # Workers
        self._audio = AudioCapture(config['audio'], self._audio_queue)
        self._vad = VADProcessor(config['vad'], self._audio_queue, self._speech_queue)
        self._transcriber = TranscriptionWorker(
            config['transcription'], self._speech_queue, self._text_queue
        )
        self._buffer = TranscriptionBuffer(
            config['buffer'], self._text_queue, self._output_queue
        )
        self._structurer = TextStructurer(config['structuring'])
        self._injector = OutputInjector(config['output'])
        self._output_pipeline = OutputPipeline(
            self._output_queue, self._structurer, self._injector
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

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start_listening(self):
        self._listening = True
        self._vad.reset()
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
