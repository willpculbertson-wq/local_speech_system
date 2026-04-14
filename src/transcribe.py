"""
transcribe.py — Speech-to-text via faster-whisper.

TranscriptionWorker is a daemon thread that reads speech segments (float32
numpy arrays at 16kHz) from input_queue, transcribes them with faster-whisper,
and puts the resulting text strings onto output_queue.

Key notes:
  - Model loading happens on thread start (not on main thread) to avoid blocking.
  - faster-whisper's transcribe() returns a lazy generator — inference does NOT
    run until you iterate it. Always consume segments before discarding.
  - Whisper hallucination artifacts are filtered before output.
  - CUDA is auto-detected; falls back gracefully to CPU with int8 quantization.
"""

import logging
import queue
import re
import threading

import numpy as np


# Complete outputs to discard entirely — the whole transcription is an artifact.
_HALLUCINATIONS = frozenset({
    '[blank_audio]',
    '(music)',
    '[music]',
    '(silence)',
    '[silence]',
    '(applause)',
    '[applause]',
    'thank you.',
    'thank you very much.',
    'thank you very much!',
    'have a good one.',
    'have a good one!',
    'thanks for watching.',
    'thanks for watching!',
    'you',
    '.',
    '',
})

# Phrases to strip when they appear INLINE within a real transcription.
# Whisper sometimes injects these mid-sentence rather than as the full output.
# Each entry is compiled as a whole-word, case-insensitive pattern.
_INLINE_HALLUCINATIONS: tuple[re.Pattern, ...] = tuple(
    re.compile(r'\s*\b' + re.escape(p) + r'\b\s*', re.IGNORECASE)
    for p in (
        'thank you very much',
        'have a good one',
        'thanks for watching',
        'please subscribe',
        'like and subscribe',
    )
)


def _resolve_device_and_compute(config: dict) -> tuple[str, str]:
    """Resolve 'auto' device/compute_type based on CUDA availability."""
    device = config.get('device', 'auto')
    compute_type = config.get('compute_type', 'auto')

    if device == 'auto':
        try:
            import torch
            device = 'cuda' if torch.cuda.is_available() else 'cpu'
        except ImportError:
            device = 'cpu'

    if compute_type == 'auto':
        compute_type = 'float16' if device == 'cuda' else 'int8'

    return device, compute_type


class TranscriptionWorker(threading.Thread):
    def __init__(
        self,
        config: dict,
        input_queue: queue.Queue,
        output_queue: queue.Queue,
    ):
        super().__init__(name='TranscribeThread', daemon=True)
        self.config = config
        self.input_queue = input_queue
        self.output_queue = output_queue
        self._stop_event = threading.Event()
        self._model = None

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def run(self):
        self._load_model()

        while not self._stop_event.is_set():
            try:
                audio_segment = self.input_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if audio_segment is None:  # Shutdown sentinel
                break

            self._transcribe(audio_segment)

    def stop(self):
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_model(self):
        from faster_whisper import WhisperModel  # type: ignore

        device, compute_type = _resolve_device_and_compute(self.config)
        model_size = self.config.get('model_size', 'medium')

        logging.info(
            f"Loading Whisper '{model_size}' on {device} ({compute_type})... "
            f"(first run downloads ~1.5 GB, please wait)"
        )

        self._model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            cpu_threads=4,
            num_workers=1,
        )

        logging.info(f"Whisper model ready: {model_size} on {device}")

    def _transcribe(self, audio: np.ndarray):
        try:
            # transcribe() returns (generator, TranscriptionInfo).
            # The generator is lazy — inference runs only when iterated.
            segments, _info = self._model.transcribe(
                audio,
                language=self.config.get('language'),  # None = auto-detect
                beam_size=self.config.get('beam_size', 5),
                vad_filter=self.config.get('vad_filter', False),
                suppress_blank=True,
            )

            # Consume the generator (this triggers actual inference).
            parts = [seg.text.strip() for seg in segments]
            full_text = ' '.join(p for p in parts if p)

        except Exception as e:
            logging.error(f"Transcription error: {e}", exc_info=True)
            return

        # Filter known hallucination artifacts — exact whole-output match
        if full_text.strip().lower() in _HALLUCINATIONS:
            logging.debug(f"Filtered hallucination: {full_text!r}")
            return

        # Strip inline hallucination phrases injected mid-sentence
        original = full_text
        for pattern in _INLINE_HALLUCINATIONS:
            full_text = pattern.sub(' ', full_text)
        full_text = re.sub(r' {2,}', ' ', full_text).strip()
        if full_text != original:
            logging.debug(f"Stripped inline hallucination: {original!r} → {full_text!r}")

        if not full_text.strip():
            return

        logging.debug(f"Transcribed: {full_text!r}")

        try:
            self.output_queue.put(full_text, timeout=2.0)
        except queue.Full:
            logging.warning("Transcribe: text_queue full, dropping result")
